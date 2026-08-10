# ADR-0004 SQLite by default, PostgreSQL kept possible

Status: accepted, 2026-08-10

## Context

The target deployment is one container on a NAS, serving one person with a
library in the thousands, not millions. Requiring a database server would double
the deployment footprint and turn a one-file backup into a `pg_dump` procedure.

Against that, the two features the matcher depends on — vector search and text
search — are exactly the ones where SQLite and PostgreSQL diverge most.

## Decision

SQLite is the default: one file under the mounted data volume, no second
container. The schema stays portable — enums are `TEXT` with `CHECK`
constraints rather than native types, no SQLite-only tricks in the core tables —
so PostgreSQL remains a supported target for anyone who wants it.

Where the two genuinely differ, both are named: `sqlite-vec` and `pgvector` for
vectors, FTS5 and `pg_trgm` for search.

Alternatives considered:

- **PostgreSQL only.** One dialect to write and test, real cross-table
  constraints (the invariant in ADR-0013 could be a trigger), better concurrent
  writes. Rejected: it makes the simplest deployment a two-container one, for a
  workload a single file handles comfortably.
- **SQLite only.** Simplest of all, and honest about the target. Rejected
  because portability costs little if it is respected from the first migration
  and is impossible to add afterwards.

## Consequences

- `docker compose up` needs one service, and a backup is one file.
- Constraints Postgres could enforce are enforced by tests instead. ADR-0013 is
  the concrete case, and it will not be the last.
- Two implementations of search and two of vector storage have to be built and
  kept in step, or "PostgreSQL possible" quietly becomes false. Whichever is not
  exercised in CI is the one that is broken.
- `PRAGMA foreign_keys = ON` must be set on every SQLite connection, or every
  `ON DELETE` policy in the schema is decorative. It is set in a pool listener
  and asserted by a test.
- Concurrent writes are serialised. With one user and a scheduler that skips
  overlapping runs this is not a constraint today; it would become one the
  moment multi-user arrived.
