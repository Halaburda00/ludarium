# Ludarium

Self-hosted, web-based game library aggregator. It connects to a user's accounts
on multiple gaming platforms, pulls their owned games into one catalogue, tags
each entry with its source platform, and lets the user filter, sort and track
what they own.

**It is a catalogue, not a launcher. We never run games.**

---

## Why this exists

Playnite and GOG Galaxy already aggregate libraries, but both are Windows
desktop applications that depend on locally installed launchers. Nothing fills
the gap of a **self-hosted, cross-platform, account-based catalogue** reachable
from a phone, a Mac, or any browser.

The secondary goal is a reusable, openly licensed dataset of cross-platform
title mappings (`ludamatch-data`), which does not currently exist in public.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI, SQLAlchemy 2.0 (typed), Alembic, Pydantic v2 |
| Database | SQLite by default, abstracted so PostgreSQL stays possible |
| HTTP | httpx (async) + tenacity for retry/backoff |
| Scheduling | APScheduler |
| Crypto | cryptography (Fernet) for platform tokens, argon2-cffi for the login password |
| Frontend | React 19 + TypeScript, Vite, Tailwind v4, shadcn/ui (Base UI) |
| Frontend data | TanStack Query, TanStack Virtual, React Router |
| i18n | i18next / react-i18next |
| Matching | RapidFuzz, sqlite-vec, fastembed (ONNX) |
| Testing | pytest, pytest-asyncio, respx |
| Tooling | uv, ruff, mypy --strict, pre-commit, gitleaks |
| Deploy | Docker multi-arch (linux/amd64 + linux/arm64), GHCR |

**Never add `torch` or `sentence-transformers`.** The container must stay small
enough to run comfortably on a NAS; `fastembed` provides the same models via
ONNX Runtime at a fraction of the size.

---

## Repository layout

```
ludarium/
├── backend/              # FastAPI service
│   ├── src/ludarium/
│   ├── tests/
│   └── pyproject.toml
├── frontend/             # React + TypeScript
├── docs/                 # mkdocs-material, ADRs, schema, roadmap, openapi.json
├── docker/               # Dockerfile, compose
├── data/                 # local SQLite database (gitignored)
├── .github/workflows/    # CI
├── CLAUDE.md
├── LICENSE               # AGPL-3.0-or-later
└── NOTICE                # name and logo are not covered by the code license
```

`ludamatch` (the matching library, MIT) and `ludamatch-data` (the alias
dataset, CC0) live in **separate repositories**, so that other projects can
depend on them without pulling in Ludarium.

---

## Language policy

- Code, comments, docstrings, README, docs, commit messages: **English**
- UI strings: **English by default**, always via i18next keys — never hardcoded
- Game titles are requested from platform APIs in English (`l=english` on Steam)
  for matcher consistency, independent of the UI language

---

## Code conventions

- PEP 8, enforced by ruff; line length 100
- `mypy --strict` — full annotations including return types and generics
- **Comments are short and substantive.** Explain *why*, not *what*. No
  decorative separators, no banner headers, no restating the code in prose.
- Docstrings only where behaviour is non-obvious. No boilerplate docstrings on
  self-explanatory functions.
- Conventional Commits, short and specific: `feat: add Steam owned-games client`
- **No signatures, emoji, or generated-by footers in commit messages.**
- One logical change per commit; a pull request per feature, CI green before merge
- Tests use `respx` with recorded fixtures — never call real platform APIs in CI

---

## Architecture rules

These are decisions already made. Do not relitigate them without being asked.

1. **Sync never deletes records.** It sets `removed_at`. Removed entries stay
   visible in a dedicated view and can be restored in one click. Only a run
   that finished with status `success` may set `removed_at` — a failed or
   partial run marks nothing as removed, or an Epic outage would empty the
   Epic library.
2. **Records with `source = manual` are immutable to sync.** A user may add a
   game they own on a disc or an unredeemed key; sync must never touch it.
3. **User edits win.** Any field a user overrides is flagged and never
   overwritten by a later sync.
4. **Providers are isolated.** An Epic outage must not affect a Steam sync.
   Each provider reports its own status and last successful run.
5. **Multiple sources per entity** with per-field precedence:
   `manual > platform API > local agent > metadata provider`.

   Precedence applies only where several sources assert the same concept. A
   platform's store name and a canonical work title are different fields, not
   competing values for one — platforms are not a source for `work.title` at
   all, and their names live on `entitlement.provider_title`.

   Field-level exceptions: `work.title` is anchored to IGDB once matched;
   `installed` comes from the local agent only; `playtime` takes the maximum
   within one entitlement and the sum across entitlements of the same work.
6. **A false positive is worse than a false negative** in matching. When
   confidence is low, send the pair to the manual review queue rather than
   merging. Every automatic merge is reversible and leaves an audit trail.
7. **Secrets only via environment variables.** Platform tokens are Fernet
   encrypted at rest, masked in the UI, never returned to the frontend, and
   excluded from logs and exports.
8. **The ingest endpoint is a public contract.** Remote providers, the future
   local agent and manual uploads all report through the same API shape.
9. **Providers never write entity columns.** They write `field_provenance`
   rows; only the resolver writes the resolved value onto the entity. Entity
   columns are a denormalised result kept so that the M3 filters can be
   indexed. This is what makes rules 3 and 5 a mechanism rather than a
   convention: a sync that goes wrong can at worst add a losing provenance row.

---

## Data model (summary)

Three levels, because two are not enough:

```
Work         The Witcher 3: Wild Hunt          canonical, IGDB-anchored
 └─ Edition  GOTY / Complete / Standard        differs in bundled content
     └─ Entitlement  steam:292030, gog:1495134320   what the user actually owns
```

`Entitlement` ↔ `Work` is many-to-many: a bundle grants several works at once.

Key enums, all present from day one even if unused:

- `OwnershipType`: `owned | subscription | free | family_shared | trial | physical`
- `ItemKind`: `game | dlc | demo | soundtrack | video | tool | mod`
- `PlayStatus`: `not_started | playing | completed | mastered | dropped | on_hold | wishlist`

`Provider` → `Account` (many) → `Entitlement`. A user may connect several
accounts on the same platform.

---

## Matching cascade

| Layer | Method | Expected coverage |
|---|---|---|
| 1 | Hard IDs (IGDB `external_games`) | ~70% |
| 2 | Curated alias dataset (`ludamatch-data`) | ~20% |
| 3 | Normalisation + RapidFuzz / TF-IDF | ~7% |
| 4 | LLM adjudication, batched, structured output | ~3% |
| 5 | Manual review queue in the UI | remainder |

Embeddings are a **retriever, not a decider**: they generate candidates, a
feature-based classifier decides (year delta, publisher, platform overlap,
cosine, fuzzy ratio). Always embed a composite document — title, year,
publisher, platforms — never a bare title, or *Prey* (2006) and *Prey* (2017)
collapse into one record.

No string-similarity threshold can separate Prey (2006) from Prey (2017): the
titles are identical, and the distinguishing features — year and publisher —
are invisible to any measure over strings. This is why candidate retrieval and
adjudication are separate steps.

Store the embedding model name and version alongside the vectors. Changing the
model invalidates the index.

---

## External data: legal constraints

| Source | Use | Constraint |
|---|---|---|
| Wikidata | alias dataset backbone | CC0 — redistributable |
| Steam `GetAppList` | public catalogue | public |
| Steam Web API | owned games, playtime | user supplies their own key |
| IGDB | metadata, canonical IDs | runtime only, non-commercial, **no redistribution** |
| RAWG | Metacritic scores | runtime only, **no redistribution**, attribution + active link required wherever displayed |

**Never scrape Metacritic.** Never ship IGDB or RAWG data inside the
repository or the Docker image.

---

## Out of scope (deliberate decisions)

- Launching or installing games
- Multi-tenant / multi-user UI — the schema carries `user_id`, the UI does not
- Subscription catalogues such as Game Pass — `OwnershipType` exists for later
- A hosted SaaS version — we would have to store other people's platform keys

---

## Common commands

```bash
# backend
cd backend && uv run uvicorn ludarium.main:app --reload --host 0.0.0.0
cd backend && uv run pytest
cd backend && uv run alembic upgrade head

# frontend
cd frontend && pnpm dev
cd frontend && pnpm run build

# api contract, after changing a request or response model
cd backend && uv run ludarium-openapi > ../docs/openapi.json
cd frontend && pnpm run api:types

# quality
pre-commit run --all-files
```
