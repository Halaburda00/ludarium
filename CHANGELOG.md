# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions below 1.0.0 make no compatibility promise; the database is migrated by
Alembic on every start, so an upgrade is expected to work even when the API
shape moves.

## [Unreleased]

### Added

- `docs/openapi.json`, the API contract as the app publishes it, printed by
  `uv run ludarium-openapi`. The frontend's request and response types are
  generated from that file rather than transcribed from it, so a renamed or
  dropped field is a compile error at every place that reads it instead of an
  empty column in the browser. Two tests keep the chain honest: the committed
  document is what the command prints, and the committed types are what the
  document generates.

### Changed

- A sync no longer costs a fixed set of round-trips per game. The account's
  existing rows are read once instead of once per item, a provider states all
  of an entitlement's fields in one statement instead of three, the resolver
  decides every field before it flushes instead of flushing twice per field,
  and the work totals are recomputed for the whole library in two queries
  instead of two per work. A 2000-game library measures 25.7 s → 9.1 s on a
  first run and 15.0 s → 3.1 s on a second, with the statements per game
  falling from 22 to 9 and from 12 to 2. What is left per game is the inserts
  SQLite will not batch. A test pins the slope, so the next per-item query
  fails there rather than on somebody's NAS.
- Lists of ids are named to the database a bind-limit at a time. Past the
  driver's ceiling an `IN (...)` does not run slowly, it raises inside the run's
  own transaction — so the sync rolls back and reports `failed` identically on
  every retry until the library shrinks.

### Fixed

- Read endpoints run their queries in a transaction. pysqlite opens none for a
  `SELECT`, so an endpoint answering with two queries had no snapshot between
  them and a concurrent write landed in the gap. SQLAlchemy now emits `BEGIN`
  itself, the journal is WAL, and the mode follows the HTTP method — safe
  methods get `BEGIN DEFERRED`, everything else `BEGIN IMMEDIATE`, which is what
  stops two read-then-write transactions deadlocking. A deferred request
  transaction is held to reading by `PRAGMA query_only`, so a handler that
  writes is refused by the database rather than deadlocking with the next
  request. See ADR-0016.
- Start-up no longer answers every `OperationalError` with "run alembic upgrade
  head". A locked database says so instead.
- A field the registry marks `single_source` can no longer end up asserted by
  two providers at once. The rule was enforced in code alone, so two syncs
  running side by side could both read the field as unclaimed; it is now a
  partial unique index, written from the registry rather than named in the
  migration. See ADR-0017.

### Note for anyone backing up the database

The database is now three files: `ludarium.db` plus `-wal` and `-shm`. Copying
only the first can lose committed transactions.

## [0.1.0] — 2026-08-19

**M1 — Steam to database to an ugly list.** One platform, no metadata, no
matching and no cover art: a self-hosted catalogue that reads a real Steam
library into a real database and shows it in a browser.

### Added

- Data model for the three levels — work, edition, entitlement — with the
  ownership, provenance and sync-run tables, and the first Alembic migration.
  The enums are complete from day one even where nothing sets them yet.
- Field resolver and the provenance write path: providers write
  `field_provenance` rows and only the resolver writes a value onto an entity,
  which is what makes "user edits win" a mechanism rather than a convention.
- `LibraryProvider` protocol and the Steam client, reading `GetOwnedGames` with
  playtime, retried with backoff and tested against recorded fixtures.
- Sync service: entitlement upsert, `first_seen_at`, per-provider status, and a
  work stub for every new entitlement so the library is work-centric from the
  first run.
- Removal marking. A sync sets `removed_at` and never deletes, and only a run
  that finished with status `success` may set it — a failed or partial run
  marks nothing, so a platform outage cannot empty a library.
- One open sync run per account, enforced by a partial unique index rather than
  by a check in the service.
- `GET /api/health`, `POST /api/accounts`, `POST /api/sync/{provider}` and
  `GET /api/works`, the last one paginated on a keyset cursor over
  `(sort_title, id)`.
- Login for the single configured user with argon2id and an httpOnly session
  cookie, reconciled with the environment on every start.
- Frontend shell: login, Steam onboarding that validates the key against Steam
  before storing it, and the library screen.
- The library table — title and platform, the platform cell linking to the
  store page — with loading, empty, error and paged states, all through
  i18next keys.

### Security

- Platform credentials are Fernet encrypted at rest, masked in the UI, never
  returned to the frontend, and excluded from logs. `httpx`'s request logging
  is silenced because a Steam key travels in the query string.
- The published OpenAPI document is walked by a test that fails if any response
  schema grows a credential-shaped field.

### Known gaps

- Steam is the only provider. `work.title` is the platform's own name until the
  M2 matcher anchors it to IGDB, so `is_matched` is `false` on every row.
- The library listing runs outside a transaction on SQLite (#32), and the
  read path works around it rather than fixing it.
