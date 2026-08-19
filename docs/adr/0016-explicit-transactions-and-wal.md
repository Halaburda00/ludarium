# ADR-0016 Explicit transactions, WAL, and the mode chosen by HTTP method

Status: accepted, 2026-08-19

## Context

pysqlite manages transactions itself, and only for DML. A `SELECT` runs with no
transaction open at all:

```
in_transaction after a SELECT: False
in_transaction after an UPDATE: True
```

So an endpoint answering with two queries has no snapshot between them.
`GET /api/works` is the first one with two, and a work whose last live
entitlement was removed between the page query and the entitlement query came
back in the list with nothing listed — a row contradicting the endpoint's own
rule. #30 patched that endpoint by dropping such rows from the page. That is the
right answer for one endpoint and not a fix for the class: every read endpoint
that grows a second query has the same gap, and the next one will not know to
look.

The write path was never affected. DML opens a transaction, so `sync_account`
and everything it calls already ran in one.

## Decision

Emit `BEGIN` from SQLAlchemy's `begin` event, so its own boundaries become real
ones. SQLAlchemy's documented recipe pairs this with `isolation_level = None` on
connect, to stop pysqlite issuing a BEGIN of its own; that half was measured to
change nothing here and left out. pysqlite skips its implicit BEGIN when a
transaction is already open, and with ours emitted first there always is one —
with and without the assignment the driver receives a statement-for-statement
identical sequence, savepoints included. Mutation testing is what surfaced it:
deleting the line failed no test, and a line no test can kill is either
untested or unnecessary.

Two things follow from the explicit BEGIN and are part of the same decision
rather than separate tuning.

**WAL.** A deferred `BEGIN` holds a shared lock for the life of the read. Under
the rollback journal that blocks the writer behind it. Measured on this schema,
one writer against six readers:

| journal | BEGIN | commit behind a 200 ms read |
|---|---|---|
| delete | driver-managed | 1.6 ms (no snapshot at all) |
| delete | explicit | **230.7 ms** |
| WAL | explicit | **2.3 ms** |

Under sustained load with a realistic commit cadence the difference is smaller —
read p99 rises from ~57 ms to ~67 ms under the rollback journal and to ~62 ms
under WAL — but the tail above is the case that matters, and WAL is what makes
holding a snapshot affordable rather than an optimisation on top of it.

**The mode comes from the HTTP method.** A transaction that reads and then
writes under a deferred `BEGIN` has to upgrade a shared lock it already holds.
Two of those deadlock, and SQLite answers the second `database is locked`
immediately without consulting `busy_timeout`, because waiting could not resolve
it. Confirmed under both journal modes. `BEGIN IMMEDIATE` takes the write lock up
front and the two serialise instead — both commit.

So a request whose method is safe (`GET`, `HEAD`, `OPTIONS`) gets
`BEGIN DEFERRED`; anything else gets `BEGIN IMMEDIATE`.

Alternatives considered:

- **`BEGIN IMMEDIATE` for everything.** No way to get it wrong, and no
  per-endpoint marking. Rejected: every read would then hold the write lock for
  its life, which reintroduces the 230 ms stall that WAL was brought in to
  remove.
- **A per-endpoint marker — a `WriteSessionDep` beside `SessionDep`.** Explicit,
  and an endpoint that forgets it gets the deadlock back silently, visible only
  under concurrency. The method is already the declaration and cannot be
  omitted.
- **Retrying on `database is locked`.** Treats the symptom, and a retry loop
  around a transaction that has already read is how a lost update happens.
- **Keeping `isolation_level = None` as insurance.** Rejected on the grounds
  above. The property it was there to guarantee — a `SELECT` inside a
  transaction — has a test; the assignment had none and could not be given one.

## Consequences

- Two queries in one request are one view of the database. `GET /api/works` no
  longer needs its workaround on SQLite — but it keeps it, because PostgreSQL's
  default `READ COMMITTED` gives each statement its own snapshot and ADR-0004
  keeps PostgreSQL a supported target. Raising PostgreSQL to `REPEATABLE READ`
  is a separate decision: it turns the problem into serialisation failures,
  which need a retry policy that does not exist yet.
- A safe method must not write. This is a convention the transaction mode now
  depends on, so it is checked: `tests/test_read_endpoints.py` fails on a `GET`
  handler that commits. It reads the handler, not the helpers below it.
- The database is three files. `ludarium.db-wal` and `ludarium.db-shm` sit
  beside it, and a backup that copies only the first can lose committed
  transactions. Anything backing the database up must checkpoint first or copy
  all three.
- WAL needs the directory to be writable, not just the file, and does not work
  over most network filesystems. On a NAS that means a local volume rather than
  an SMB or NFS mount.
- A `:memory:` database ignores `journal_mode` and answers `memory`. The test
  suite runs file-backed, so the journal is covered; nothing in the application
  depends on it being WAL.
