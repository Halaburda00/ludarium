# Roadmap

Vertical slices, not horizontal layers. Every milestone ends with something
that runs end to end, is tagged, and has a CHANGELOG entry.

Estimates assume roughly 5 hours per day.

---

## M0 — Documentation and decisions · ~1 day

Write down what has already been decided, so neither a future contributor nor
an AI agent proposes a launcher or multi-user support three weeks from now.

- [x] `docs/schema.md` — full data model: entities, fields, relations,
      indexes, enums, per-field source precedence
- [x] `docs/adr/` — one file per decision, in the format
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
  - ADR-0011 Providers write provenance rows, never entity columns
  - ADR-0012 `work.title` is IGDB-anchored; precedence applies only to
    competing assertions about the same concept
  - ADR-0013 `entitlement_work` `role='primary'` is the single source of truth
    for work membership
  - ADR-0014 No per-field history; provenance rows are updated in place
  - ADR-0015 Sync creates work stubs rather than deferring work creation to the
    matcher
- [x] `README.md` — Motivation, Prior art (Playnite, GOG Galaxy, Heroic,
      Lutris, Backloggd), Status, Roadmap, Licence
- [x] `CONTRIBUTING.md` — inbound = outbound, commit format, how to run tests,
      and the rule that matcher logic belongs in `ludamatch` under MIT
- [x] `.github/workflows/ci.yml` — ruff, mypy, pytest on Python 3.13 only, plus
      a frontend build. The application ships as a `python:3.13-slim` image, so
      there is a single supported interpreter; a version matrix belongs to
      `ludamatch` (created in M2), which is a library others depend on

**Done when:** a stranger reading `docs/` understands what the project is,
what it deliberately is not, and why.

---

## M1 — Steam → database → an ugly list · ~12 days

No cover art, no filters, no matching. It must work end to end and it is
allowed to look bad.

The original estimate of ~4 days assumed the provenance layer was free; it is
not, and the resolver has to exist before the sync service rather than after it.

- [ ] SQLAlchemy models + first Alembic migration
- [ ] Settings via pydantic-settings, Fernet encryption for the API key
- [ ] `LibraryProvider` protocol + `SteamProvider`
      (`GetOwnedGames`, playtime stored even though it is not displayed yet)
- [ ] Sync service: upsert, `first_seen_at`, `removed_at`, per-provider status,
      and a work stub per new entitlement (ADR-0015) so the grid is
      work-centric from the first run
- [ ] `GET /api/works`, `POST /api/sync/{provider}`, `GET /api/health`
- [ ] Login (single account, argon2), session cookie
- [ ] Onboarding: paste the Steam key + SteamID, validate immediately, show a count
- [ ] Frontend: a plain table of titles with the platform column
- [ ] Tests with respx fixtures

**Done when:** `docker compose up` is not needed — running the backend and the
frontend locally shows your real Steam library in the browser.

---

## M2 — Metadata and a real grid · ~5 days

- [ ] IGDB client (Twitch OAuth, token cache, rate limiting)
- [ ] Matching layer 1: hard IDs from IGDB `external_games`. It belongs here,
      not in M4 — a stub has to acquire its IGDB anchor before there is
      anything to enrich, and the IGDB client is already in this milestone
- [ ] Create `ludamatch` as a separate MIT repository, seeded with what layer 1
      needs: title normalisation, the `external_games` lookup, and the mapping
      types. Ludarium depends on it from this milestone onward and keeps no
      matcher logic of its own
- [ ] `merge_work(source, target)` and the orphan-stub cleanup job, both
      specified in `docs/schema.md`. Layer 1 is the first thing that merges
      stubs, so the operation ships with it, tests and undo included
- [ ] RAWG client for Metacritic + required attribution link in the UI
- [ ] Enrichment pipeline with local caching — never re-fetch what we have
- [ ] Cover art, storage and lazy loading
- [ ] Frontend: virtualised grid, search, detail view
- [ ] Dark mode

**On the timing of `ludamatch`:** it is created here rather than at M6 for two
reasons, neither of them preference. Licence hygiene — every line of matcher
code is ours only until the first external contribution touches it, and after
v0.1.0 (M5) that stops being a safe assumption; relicensing later would need
every contributor's agreement. And cost — extracting three functions now takes
an hour, extracting a grown matcher takes a week, and a library written as a
library ends up with a better API than one carved out of an application.

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
- [ ] `POST /api/ingest` as a public contract: one payload shape carrying the
      reporting provider, the account it describes, and a list of items
      (`provider_item_id`, `title`, `ownership_type`, `playtime_minutes`,
      `installed`, `acquired_at`, plus an opaque `raw` object). Versioned,
      documented, and validated the same way whoever posts it — the Galaxy
      upload is its first consumer, the local agent in "Later" is the second
- [ ] Upload and parse `galaxy-2.0.db` — reaches EA, Ubisoft, Battle.net in one
      step without reverse-engineering three APIs. Posts through `/api/ingest`;
      the accounts it discovers are created derived, with no credentials
- [ ] CSV/JSON import
- [ ] Multiple accounts per platform, with labels
- [ ] Scheduled sync on APScheduler, writing runs with
      `SyncTrigger.scheduled`; per-provider interval, skipped while a run for
      that provider is already in flight
- [ ] Matching layer 2: the curated alias dataset (layer 1 shipped in M2)

**Done when:** the library covers every platform you actually use, and it
refreshes itself without being asked.

---

## M5 — Docker, docs, first public release · ~3 days

- [ ] Multi-stage Dockerfile, target under 500 MB
- [ ] GitHub Actions: buildx, `linux/amd64` + `linux/arm64`, push to GHCR
- [ ] `PUID` / `PGID` / `TZ`, healthcheck, reverse proxy and subpath support
- [ ] `docker-compose.yml` ready to copy from the README
- [ ] Export and backup, implementing the `licence_class` filtering in
      `docs/schema.md` — IGDB and RAWG values are dropped on the way out. A
      self-hosted tool people cannot get their data out of is a trap, and the
      first release is the moment that promise has to hold
- [ ] mkdocs-material on GitHub Pages, `ludarium.dev`
- [ ] Screenshots — the single biggest factor in whether anyone tries it
- [ ] release-please, CHANGELOG, tag **v0.1.0**

**Done when:** a stranger can run Ludarium from one compose file.

---

## M6 — Matching layers 3–5 · ~1 week

`ludamatch` already exists as its own MIT repository (M2). This milestone fills
it out and finishes the cascade.

- [ ] Title normalisation extended: editions, trademarks, roman numerals,
      punctuation — beyond the minimum layer 1 needed
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
- Local agent (installed state, accurate playtime, EA/Ubisoft/Battle.net),
  reporting through the `/api/ingest` contract defined in M4
- Statistics and backlog reports
- Xbox, PlayStation
- Subscription catalogues (Game Pass)
- Paid mobile client
