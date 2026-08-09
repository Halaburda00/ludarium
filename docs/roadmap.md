# Roadmap

Vertical slices, not horizontal layers. Every milestone ends with something
that runs end to end, is tagged, and has a CHANGELOG entry.

Estimates assume roughly 5 hours per day.

---

## M0 — Documentation and decisions · ~1 day

Write down what has already been decided, so neither a future contributor nor
an AI agent proposes a launcher or multi-user support three weeks from now.

- [ ] `docs/schema.md` — full data model: entities, fields, relations,
      indexes, enums, per-field source precedence
- [ ] `docs/adr/` — one file per decision, in the format
      *Context / Decision / Consequences*:
  - ADR-0001 Catalogue, not launcher
  - ADR-0002 Self-hosted web app instead of a desktop application
  - ADR-0003 Single-tenant with a `user_id` column reserved
  - ADR-0004 SQLite by default, PostgreSQL kept possible
  - ADR-0005 Three-level model: Work / Edition / Entitlement
  - ADR-0006 Wikidata as the redistributable alias backbone
  - ADR-0007 Layered matching cascade, manual queue over silent merges
  - ADR-0008 AGPL for the app, MIT for the library, CC0 for the data
  - ADR-0009 fastembed instead of sentence-transformers
  - ADR-0010 Sync never deletes; `manual` records are immutable
- [ ] `README.md` — Motivation, Prior art (Playnite, GOG Galaxy, Heroic,
      Lutris, Backloggd), Status, Roadmap, Licence
- [ ] `CONTRIBUTING.md` — inbound = outbound, commit format, how to run tests
- [ ] `.github/workflows/ci.yml` — ruff, mypy, pytest on a 3.11–3.13 matrix,
      plus a frontend build

**Done when:** a stranger reading `docs/` understands what the project is,
what it deliberately is not, and why.

---

## M1 — Steam → database → an ugly list · ~4 days

No cover art, no filters, no matching. It must work end to end and it is
allowed to look bad.

- [ ] SQLAlchemy models + first Alembic migration
- [ ] Settings via pydantic-settings, Fernet encryption for the API key
- [ ] `LibraryProvider` protocol + `SteamProvider`
      (`GetOwnedGames`, playtime stored even though it is not displayed yet)
- [ ] Sync service: upsert, `first_seen_at`, `removed_at`, per-provider status
- [ ] `GET /api/games`, `POST /api/sync/{provider}`, `GET /api/health`
- [ ] Login (single account, argon2), session cookie
- [ ] Onboarding: paste the Steam key + SteamID, validate immediately, show a count
- [ ] Frontend: a plain table of titles with the platform column
- [ ] Tests with respx fixtures

**Done when:** `docker compose up` is not needed — running the backend and the
frontend locally shows your real Steam library in the browser.

---

## M2 — Metadata and a real grid · ~5 days

- [ ] IGDB client (Twitch OAuth, token cache, rate limiting)
- [ ] RAWG client for Metacritic + required attribution link in the UI
- [ ] Enrichment pipeline with local caching — never re-fetch what we have
- [ ] Cover art, storage and lazy loading
- [ ] Frontend: virtualised grid, search, detail view
- [ ] Dark mode

**Done when:** the library looks like something you would actually want to browse.

---

## M3 — Filters, statuses, backlog · ~5 days

- [ ] Filter registry: one entry per filter, declarative, maps to SQL
- [ ] Filters: platform, Metacritic, genre, year, playtime, `ItemKind`, status
- [ ] `ItemKind` classification; DLC folded under its parent game
- [ ] Filter state in the URL; saved views
- [ ] `PlayStatus`, personal rating, notes
- [ ] Manual entry — physical copies, unredeemed keys, itch.io
- [ ] Removed-from-account view with one-click restore
- [ ] Demo mode with a fixed seed dataset

**Done when:** the tool answers "what should I play tonight" better than any
platform's own UI.

---

## M4 — GOG, Epic, local import · ~5 days

- [ ] `GogProvider`
- [ ] `EpicProvider` (auth flow modelled on legendary)
- [ ] Upload and parse `galaxy-2.0.db` — reaches EA, Ubisoft, Battle.net in one
      step without reverse-engineering three APIs
- [ ] CSV/JSON import
- [ ] Multiple accounts per platform, with labels
- [ ] Matching layers 1–2: IGDB hard IDs + alias dataset

**Done when:** the library covers every platform you actually use.

---

## M5 — Docker, docs, first public release · ~3 days

- [ ] Multi-stage Dockerfile, target under 500 MB
- [ ] GitHub Actions: buildx, `linux/amd64` + `linux/arm64`, push to GHCR
- [ ] `PUID` / `PGID` / `TZ`, healthcheck, reverse proxy and subpath support
- [ ] `docker-compose.yml` ready to copy from the README
- [ ] mkdocs-material on GitHub Pages, `ludarium.dev`
- [ ] Screenshots — the single biggest factor in whether anyone tries it
- [ ] release-please, CHANGELOG, tag **v0.1.0**

**Done when:** a stranger can run Ludarium from one compose file.

---

## M6 — `ludamatch` · ~1 week

Extracted into its own repository under MIT.

- [ ] Title normalisation (editions, trademarks, roman numerals, punctuation)
- [ ] Candidate retrieval: trigram / BM25 + optional ANN over embeddings
- [ ] Feature-based classifier for adjudication
- [ ] Optional LLM layer, batched, structured output, cached
- [ ] Golden set of ~300 pairs, precision/recall measured **per layer**
- [ ] `ludamatch-data`: alias dataset generated offline from Wikidata, CC0
- [ ] Manual review queue in the Ludarium UI

**Done when:** the first user's library matches at ~95% with no manual work,
and the README shows measured numbers rather than claims.

---

## Later

- Wishlist with manual ordering, then Steam wishlist import
- Achievements
- Local agent (installed state, accurate playtime, EA/Ubisoft/Battle.net)
- Statistics and backlog reports
- Export and backup
- Xbox, PlayStation
- Subscription catalogues (Game Pass)
- Paid mobile client
