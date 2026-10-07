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

- [x] SQLAlchemy models + first Alembic migration
- [x] Settings via pydantic-settings, Fernet encryption for the API key
- [x] `LibraryProvider` protocol + `SteamProvider`
      (`GetOwnedGames`, playtime stored even though it is not displayed yet)
- [x] Sync service: upsert, `first_seen_at`, `removed_at`, per-provider status,
      and a work stub per new entitlement (ADR-0015) so the grid is
      work-centric from the first run
- [x] `GET /api/works`, `POST /api/sync/{provider}`, `GET /api/health`
- [x] Login (single account, argon2), session cookie
- [x] Onboarding: paste the Steam key + SteamID, validate immediately, show a count
- [x] Frontend: a plain table of titles with the platform column
- [x] Tests with respx fixtures

**Done when:** `docker compose up` is not needed — running the backend and the
frontend locally shows your real Steam library in the browser.

---

## M2a — Metadata and matching · ~3 days

The data half of what was one milestone. Split because the two halves are
different work with different failure modes, and because the view half cannot
start until the ordering underneath it is correct.

M2b is not a third half: it is Epic and review scores, pulled forward from M4
into the gap between the two. References elsewhere in the docs to "M2" mean
M2a and M2c and stay true.

The list is in build order, and the order is load-bearing twice over.

- [x] Fix the works ordering and the cursor that depends on it. First, because
      the grid in M2c is built on both, and fixing them afterwards means
      migrating the column twice
- [x] IGDB client (Twitch OAuth, token cache, rate limiting)
- [x] Enrichment pipeline with local caching — never re-fetch what we have.
      Ahead of everything that fetches, because it is the cache, the batching
      and the rate limiting all of them share; written after its callers it
      becomes three private ones
- [x] `ItemKind` classification, moved forward from M3, and ahead of the
      matcher rather than merely inside the same milestone. A real library
      carries playtests, public beta clients and test servers, and
      `GetOwnedGames` carries no field that separates them from games — so
      layer 1 would be handed titles that resolve in IGDB to the game they are
      a test of. Rule 6 makes that worse than leaving them unmatched, which is
      the whole reason this moved; running the matcher first would keep the
      milestone and lose the point of it
- [x] Create `ludamatch` as a separate MIT repository, seeded with what layer 1
      needs: title normalisation, the `external_games` lookup, and the mapping
      types. Ludarium depends on it from this milestone onward and keeps no
      matcher logic of its own
- [x] Matching layer 1: hard IDs from IGDB `external_games`. It belongs here,
      not in M4 — a stub has to acquire its IGDB anchor before there is
      anything to enrich, and the IGDB client is already in this milestone
- [x] `merge_work(source, target)` and the orphan-stub cleanup job, both
      specified in `docs/schema.md`. Layer 1 is the first thing that merges
      stubs, so the operation ships with it, tests and undo included
- [x] RAWG client for Metacritic + required attribution link in the UI
- [x] Cover art: fetching and storage

**On the timing of `ludamatch`:** it is created here rather than at M6 for two
reasons, neither of them preference. Licence hygiene — every line of matcher
code is ours only until the first external contribution touches it, and after
v0.1.0 (M5) that stops being a safe assumption; relicensing later would need
every contributor's agreement. And cost — extracting three functions now takes
an hour, extracting a grown matcher takes a week, and a library written as a
library ends up with a better API than one carved out of an application.

**Done when:** the library knows what its games are — matched, enriched and
classified — even though it still looks like a table.

---

## M2b — Epic and review scores · ~3 days

Pulled forward from M4, because it is the outcome most wanted from the whole
project and nothing in it waits on presentation: the provider protocol and the
sync already exist, and what a score on an Epic game depends on — matching
layer 1 and RAWG — finishes in M2a. Leaving it in M4 would hold it behind two
milestones of grid and filters. The scores show in the M1 table until M2c
replaces it.

- [x] `EpicProvider` (auth flow modelled on legendary). Epic has no public
      library API; the flow is the launcher's own and can change without
      notice, so the provider must fail as itself (rule 4). What it holds is a
      refresh token that is replaced on every refresh — encrypted like the
      Steam key, and written back each time, or the second sync is logged out
- [x] Steam review scores for every work with a Steam appid, Epic-only works
      included: layer 1 finds their appid in IGDB `external_games`, and the
      appid is all the score needs

**Done when:** an Epic game shows its Metacritic and Steam review scores, and
an Epic outage leaves the Steam sync untouched.

---

## M2c — A real grid · ~3 days

The view half, and it follows M2a because three of its four items are useless
until there is metadata to show. It is not purely a consumer, and the estimate
says so: search needs a backend of its own.

- [x] Virtualised grid with covers and lazy loading
- [x] Search. The only item here with a backend half: the query matches
      `work.title` and `entitlement.provider_title` and still pages on the
      cursor the first item of M2a fixes. A substring over the sort key's fold
      rather than the FTS5 first planned (ADR-0028)
- [x] Work detail view
- [x] Dark mode

**Done when:** the library looks like something you would actually want to browse.

---

## M3 — Filters, statuses, backlog · ~5 days

- [x] Filter registry: one entry per filter, declarative, maps to SQL
- [x] Filters: platform, Metacritic, genre, year, playtime, `ItemKind`, status
- [x] Sort by title, Metacritic, Steam review score, playtime, last played and
      release date, either way, each on its own keyset. Agreed after this
      roadmap was written: "what should I play tonight" is mostly a question of
      order
- [x] DLC folded under its parent game (`ItemKind` classification moved to M2a); the parent from Epic's `mainGameItem` (ADR-0032)
- [x] Filter state in the URL; saved views
- [x] `PlayStatus`, personal rating, notes
- [x] A play queue the user orders by hand: `queued` with a dense position (ADR-0033), and an "Up next" view to reorder it
- [x] Manual entry — physical copies, unredeemed keys, itch.io; deleting one is a `DELETE` (ADR-0031)
- [x] Removed-from-account view with one-click restore; a restored copy is kept (ADR-0030)
- [x] Demo mode on a made-up library, read-only, with no scores or store links to invent (ADR-0034)

**Done when:** the tool answers "what should I play tonight" better than any
platform's own UI.

---

## M4 — GOG, local import · ~5 days

Epic moved to M2b.

- [ ] `GogProvider`
- ~~Steam Family Sharing~~ — dropped (#62): the family endpoints refuse the
  Web API key and want a user access token, and a lent game is not the
  user's library. `family_shared` stays in the enum; a lent game can be a
  manual entry
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
- [ ] Multiple accounts per platform, with labels: the backend has allowed it
      since M1, and an accounts screen makes it usable
- [x] Scheduled sync on APScheduler, writing runs with
      `SyncTrigger.scheduled`; per-provider interval, skipped while a run for
      that provider is already in flight (ADR-0036)
- Matching layer 2 moved to M6: it consumes `ludamatch-data`, which M6 builds

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
- [ ] Publish `ludamatch` to PyPI and replace the git dependency on it. A
      compose file a stranger runs cannot be made to resolve a git URL
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
- [ ] Matching layer 2: the curated alias dataset in the cascade (moved from M4)
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
