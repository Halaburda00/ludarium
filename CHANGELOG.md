# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions below 1.0.0 make no compatibility promise; the database is migrated by
Alembic on every start, so an upgrade is expected to work even when the API
shape moves.

## [Unreleased]

### Fixed

- A Steam sync no longer skips games Steam considers unvetted. `GetOwnedGames`
  omits them by default and lowers its own `game_count` to match, so the
  response was self-consistent and the loss was invisible from this side:
  measured against a real library, one owned game of 197 was in the Steam
  client and never in Ludarium.
- The library lists titles in the order a person expects. It was ordered by
  the database's byte comparison, so "ARC Raiders" came before "Amnesia", an
  accented letter sorted after every unaccented one in its place, and a
  trademark sign split a series in two — "Batman: Arkham City" in one block,
  "Batman™: Arkham Knight" in the next. Works now carry a folded copy of their
  sort title that ignores case, accents, trademark signs and doubled spaces,
  and the listing and its cursor both order by it. The title a user sets by
  hand is kept exactly as written. A cursor handed out before the upgrade is
  refused rather than read under the new order, so a library left open across
  the upgrade needs a reload to carry on scrolling. On PostgreSQL the key is
  compared byte for byte, as on SQLite, rather than by the database's locale.
  Every start rewrites any stored key the running code would compute
  differently — after an upgrade to a Python with a newer Unicode database, for
  instance — and logs how many; a database that was never upgraded is refused at
  start with the command to run.

### Added

- Every game IGDB has matched now carries its summary, its release date and
  year, and the companies behind it: developer, publisher, porting and support
  studios. They are asked for in the same run as matching and covers, and kept
  for a month. Measured on a real library: of 587 matched games, 587 got a
  summary, 584 a release date and 575 a publisher. A year or summary set by hand
  still wins. When two cards are folded into one, their companies move with
  them, and an undo puts them back. Nothing shows them yet: the work detail view
  does (#54).
- Search. Type in the field above the grid, and once you pause the library
  shows only the games whose title contains what you typed, anywhere in it,
  ignoring case, accents and trademark signs as the ordering does. The name a
  store gives a game counts too, so a game IGDB renamed is still found under
  the name you saw on Steam or Epic. Results page like the library. The search
  is kept in the address, so a reload or the back button keeps it.
  `GET /api/works` takes it as `q`.
- The library is a grid of covers instead of a table. Only the cards on
  screen, and a few rows either side, are rendered, so a library of thousands
  scrolls like one of ten. The next page is asked for before the end of what
  is loaded comes into view, and scrolling faster than it arrives reaches a
  loading row rather than blank space. A cover's space is kept before the image
  loads, and nothing moves when it does. The grid is a feed to a screen reader,
  each card with its place in the library. Tab walks the links in reading order,
  and the arrow keys, Home and End move from card to card. If a later page
  fails to load, the cards already loaded stay, with a retry at the end, rather
  than giving way to an error that hid them.
- The enrichment pipeline that M2a's metadata, covers and classification will
  fetch through. What a provider answers is cached in the database, including
  "nothing under this key", so a second run over an unchanged library asks the
  provider nothing. Keys are asked about in batches, and each batch is committed
  as it arrives: a run that fails halfway keeps what it fetched, and the next
  run asks only for the rest. Enrichment never runs inside a sync's
  transaction. Each run is recorded like a sync, with the provider's own health,
  one open run per provider, and an outage reported as a failed run rather than
  an error. IGDB is now a seeded provider, marked as data that may not leave the
  instance. The data directory is excluded from the Docker build context as
  well as from git, and a test checks both. Nothing triggers enrichment yet:
  that arrives with the first step to run in it.
- Steam items are classified by what Steam's store says they are: game, demo,
  playtest, mod, DLC, tool or soundtrack. `GetOwnedGames` says nothing about it,
  and the store endpoint usually used for it calls every playtest a game. The
  store is asked after every successful Steam sync, 200 apps per request, and
  can be asked again by hand with `POST /api/enrichment/steam_store`. A store
  outage is reported as its own failed run and never fails the sync. A label
  set by hand still wins. `playtest` is a new kind. A work nothing has classified
  now reads as unclassified rather than as a game, and an upgrade clears the old
  `game` default wherever no source asserted it. Two kinds of entry are still
  called games, because the store has no word for them: test servers and public
  beta clients.
- Epic Games libraries. Connect an Epic account from the onboarding screen:
  sign in on Epic's own page, copy the code it shows, paste it back. Ludarium
  keeps the sign-in, not your password, encrypted like the Steam key, and
  renews it on every sync. Games, DLC and tools come in with their English
  titles; Unreal Engine marketplace assets are left out. Claimed free games
  are owned games. "Sync now" now syncs every platform you have connected, and
  a platform that fails says so on its own — with a "Sign in again" link when
  Epic has ended the sign-in, and without one when Epic is simply down. This
  uses the Epic Games Launcher's own endpoints, which Epic does not document
  and can change; if it does, the Epic sync fails and Steam carries on.
- Epic games are matched to IGDB, so they get IGDB's title, cover art and —
  for games owned only on Epic — a Metacritic score. A game owned on both
  Steam and Epic becomes one card. Measured on a real library: 419 of 533
  Epic games matched, 17 folded into their Steam cards, and 192 of 406
  Epic-only games scored. A game is matched only where its title agrees with
  the store's, so a beta, test branch or soundtrack is never taken for the game
  it belongs to.
- Cover art for every game IGDB has matched, fetched in the same run as the
  match and kept on this server under the data directory
  (`LUDARIUM_DATA_DIR`, `./data` by default). Two sizes of each, 264×374 and
  528×748, so a sharp screen gets a sharp cover and an ordinary one downloads a
  third as much: about 96 KB a game, some 19 MB for a library of two hundred.
  Covers are served by Ludarium itself, so the library does not go blank when
  IGDB is down and IGDB does not see what you browse. A cover IGDB changes is
  fetched again within a month and the old files are removed. The files are
  IGDB's and are never committed, baked into an image, or — once exports exist
  — exported. Nothing shows them yet: that is the grid's job.
- The library says when it is still being updated after a sync. Classification,
  IGDB matching and RAWG scores run after the sync has answered and can take a
  few minutes; until now the button came back within seconds and nothing said
  more was on its way, so the new titles and scores appeared only on a reload.
  The library now names the step that is running, holds the sync button back
  meanwhile — another sync would have skipped the steps already underway — and
  reloads itself when the last one ends. A step that fails is reported with its
  reason. `POST /api/sync/{provider}` now answers `{runs, enriching}` rather than
  a bare list of runs, and `GET /api/sync/runs` gains `enriching`.
- Steam's user-review score, in a column beside Metacritic: the share of
  positive reviews, linked to the reviews on the store page, with Steam's
  verdict and the number of reviews in the link's name. It counts every
  language and only copies bought on Steam, and leaves out the off-topic
  review bombs the store leaves out by default. A game owned only on Epic gets
  its score too, once IGDB has matched it and given it a Steam appid. Where a
  game has several Steam apps, the most-reviewed one is used. Scores are asked
  for with the store's other data after every Steam sync, and after matching
  on every Epic sync, 200 apps per request, and kept for a week.
- Metacritic scores, from RAWG, in a column of the library. Set
  `LUDARIUM_RAWG_API_KEY` to a key registered at rawg.io; without one nothing
  is fetched. Only games IGDB has matched are looked up, by IGDB's name, and a
  result is believed only when RAWG sells it under the game's own Steam appid —
  so *Prey* (2017) never gets *Prey* (2006)'s score, and a game RAWG cannot
  confirm gets none. Each score links to the game's page on RAWG, and the
  library credits RAWG wherever scores are shown, as RAWG's terms require.
  Scores are asked for after matching, on every Steam sync, and kept for a
  month; RAWG's free tier allows 20 000 requests a month, about three per game.
  They are RAWG's data and will be left out of exports.
- Two cards for one game become one. When IGDB files two owned entries under
  the same game — a standard and a GOTY release, say — the newer card is folded
  into the one already matched: its copies, editions, sources and your status,
  rating and notes move over, where the surviving card had not set its own, and
  playtime is summed again. Every merge is recorded and can be undone, which
  brings the folded card back with everything it took along. Cards that no
  copy points at any more are cleared away after each IGDB run, unless they
  are matched or hold something you set.
- An IGDB client, the first piece of M2a. It authenticates through a Twitch
  application, enforces both of IGDB's documented limits — four requests a
  second and eight open at once — once per application however many clients
  there are, and retries with backoff on an outage and on IGDB's own 429, though
  not on Twitch's, whose limit is undocumented. The app access token is kept in
  the database, encrypted
  like platform credentials, so a restart does not mint another.
  `LUDARIUM_IGDB_CLIENT_ID` and `LUDARIUM_IGDB_CLIENT_SECRET` are optional — an
  instance without them syncs libraries exactly as before — but half an
  application is refused at start. Nothing calls the client yet.
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
- A provider that lists one item twice with two different titles now names the
  work stub by the title the entitlement ends the run with, rather than by the
  one the first mention happened to seed. The stub is a copy of the resolved
  `provider_title`, and it used to be a copy of a value that had since moved.
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
