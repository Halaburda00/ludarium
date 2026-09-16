# ADR-0019 Enrichment caches what it fetches in the database and runs outside the sync

Status: accepted, 2026-09-17

## Context

Four M2a items fetch data about a library that has already been synced: IGDB
metadata and external ids (#47), RAWG scores (#49), cover art (#51), and store
data for `ItemKind` classification (#41). #50 builds what they share before any
of them exists. Written after them, it would be four private caches.

Three constraints shape it.

- **IGDB and RAWG data may not be redistributed.** A cache is fetched provider
  data at rest, so where it lives decides whether it can reach the repository,
  a Docker image, or an export. #56 gave this decision to #50 and to no one
  else.
- **A library is enriched over and over.** A 197-game library enriched after
  every sync, with no memory of the last run, would ask IGDB the same 197
  questions each time. Nothing about most of them has changed.
- **A sync is one transaction** that commits with its `success` status, because
  a partial library must not mark anything removed (rule 1, ADR-0010). A sync
  that also enriched would hold that write transaction across dozens of network
  calls, and an IGDB outage would fail a run that had already stored a correct
  library.

## Decision

**Fetched responses are cached in the database**, in `fetch_cache`, one row per
`(provider, resource, key)`: `igdb`, `games`, `1942`. The payload is the
provider's answer as JSON. SQL null means the provider was asked and has
nothing under that key, which is kept: a playtest IGDB has never heard of is
otherwise asked about again on every run. `fetched_at` says when, and a caller
that knows its provider revises records passes a `max_age`.

**Binary files are the exception.** Cover art (#51) goes on disk under the data
directory, beside the database, with `image_asset.local_path` relative to it.
Image files in the database would grow the one file every backup copies whole.

**The data directory is what keeps both private.** `.gitignore` already
excludes `data/`. `.dockerignore` excludes `**/data/`, because Docker anchors a
bare `data/` at the context root and the default database lives in
`backend/data/`. A test checks both against the path `Settings` defaults to, so
moving the default somewhere neither file covers fails in CI. Exports (M5) leave
out `fetch_cache` whole, whatever each provider's `licence_class` says: it is a
cache, and an export carries resolved values.

**A step asks through `EnrichmentRun.fetch`.** It hands over the keys it wants
and a function that asks the provider about a batch of them. Fresh cached keys
are answered from the table. The rest go to the provider `batch_size` at a time
(500 for IGDB, its documented `limit`), and each batch commits before the next
is asked for. No transaction is open while the provider is being asked.

**A failed run keeps what it fetched.** A sync rolls back on failure. Enrichment
does not, because it removes nothing: nothing it can leave half-done is a
hazard. A run that fails on its tenth batch keeps nine, and the next run asks
only for the tenth.

**Rate limits stay in the clients.** #45 put IGDB's limits in `IgdbClient` so
that no caller could exceed them by existing, and the providers' limits differ
in kind: IGDB documents one per second, Steam's store API documents none. A
limiter in the pipeline would duplicate the first and guess at the second.

**A run is a `sync_run` with no account.** `account_id` was already nullable
"for metadata providers". The run reports the provider's health on its row, as
a sync does (rule 4). A `ProviderError` becomes a failed status rather than an
exception, so whatever triggered the run is not failed by it. A partial unique
index allows one open run per provider, so two triggers cannot ask IGDB the same
questions twice. An IGDB run and a RAWG run never wait on each other. An
orphaned run is reclaimed after `sync.ORPHAN_AFTER`, as a sync's is. This
replaces the earlier reading, pinned in a sync test, that metadata runs are open
several at a time by design.

**Enrichment is triggered after a successful sync, and by hand.** After a sync,
because that is when the library has something new to enrich, and a user who
has just connected Steam should not have to know a second step exists. By hand,
because an IGDB outage ends a run that someone will want to retry without
syncing again. A failed sync triggers nothing, since it stored nothing. A
schedule comes with M4, where APScheduler arrives for syncs. Enrichment would
otherwise be the first and only user of a dependency added for it.

The wiring lands with the first step, #41, not here. With no steps, an endpoint
and a post-sync hook would run nothing and could be tested for nothing but
their own existence.

Alternatives considered:

- **Files on disk, one JSON document per key.** Needs its own atomic-write
  story, its own staleness scan, and a second location to keep out of backups.
  The database already has all three, and the lookups are by key.
- **An HTTP-level cache under httpx.** Keyed by request, and a batched request
  names 500 ids: one new game in the library changes the request and misses the
  cache for the other 499. The unit that stays the same between runs is the
  key, not the request.
- **Enrichment inside the sync transaction.** The failure the issue names: a
  write lock held across network calls, and an outage failing a stored library.
- **A schedule now.** See above.

## Consequences

- A run against an unchanged library asks the provider nothing, and
  `sync_run` shows it: `items_seen` counts the keys asked about, `items_updated`
  the ones that went to the provider. A second run over the same library reads
  as many seen and none updated.
- The table grows with the library and the providers enriching it. Nothing
  shrinks it yet. Emptying it costs only the requests to fill it again, so it
  can be cleared by hand until something needs more.
- A step's keys must be complete. A key the fetch function leaves out is
  cached as absent, so a function that truncates its answer (IGDB's `limit`
  caps rows, not keys) caches a false absence. The function has to page rather
  than return short. The same rule already applies to `SteamProvider`, which
  refuses a body shorter than the count Steam sent.
- A post-sync trigger that finds a run already open is refused. The items that
  sync added wait for the next trigger. Before M4 that is the next sync or a
  click. #41 decides whether that is good enough or whether the hook queues a
  follow-up run.
- A step that runs under a platform provider's key (#41's store data would
  run under `steam`) shares that provider's health columns with its library
  syncs, and the later run's status overwrites the earlier one's. `sync_run`
  still tells them apart by `account_id`. Per-account health arrives in M4. #41
  decides whether store data is a provider of its own.
- The data directory is instance state, all of it. Anything meant to ship,
  such as the CC0 alias dataset, needs a path outside it.
