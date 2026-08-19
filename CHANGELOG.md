# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions below 1.0.0 make no compatibility promise; the database is migrated by
Alembic on every start, so an upgrade is expected to work even when the API
shape moves.

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
