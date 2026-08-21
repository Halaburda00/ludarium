# ADR-0017 Check-then-act is safe inside one transaction; the database says so, not a re-read

Status: accepted, 2026-08-21

## Context

Two places read, decide, and then write:

- `seed_providers()` selects the provider table and inserts what is missing.
- `_check_sole_source()` inside `record()` refuses a second non-manual source
  for a `single_source` field.

Rule 4 makes contention on the second one legal by design: providers sync
independently and concurrently, so two runs can both read a field as unclaimed.
Nothing in the schema covered it — `single_source` is a property of the registry
in `resolver.STRATEGIES`, not a column — and the database accepted both rows.

Reported as issue #20, before ADR-0016 landed. Half of it is no longer true.

## Decision

**The transaction is the mechanism.** Both sites read and write inside one, and
what makes that safe is the isolation the transaction gives them, not a re-read
bolted on after the insert.

On SQLite, ADR-0016 already closed both windows and neither site needed a
change. Every write goes through a session that announces itself with `BEGIN
IMMEDIATE`, so a second writer blocks at `BEGIN` rather than reading a database
it is about to invalidate. Measured, with a 300 ms window forced between the
read and the write:

| pattern | winner | loser |
|---|---|---|
| seed, `BEGIN IMMEDIATE` | read `[]` at +0.017 s, committed | read `['steam']` at +0.383 s, inserted nothing |
| `record()`, `BEGIN IMMEDIATE` | wrote | refused: *work.title is single_source; igdb already asserts it* |
| either, `BEGIN DEFERRED` | committed | `database is locked` |

The loser under `IMMEDIATE` waits and then sees the winner's work, which is the
whole of what a re-read would have told it. The deferred row is the deadlock
ADR-0016 exists to prevent, and no request can take that path any more: a
deferred transaction is held to reading by `PRAGMA query_only`.

**On an engine where two writers can read the same empty field, a constraint
does it.** PostgreSQL's default `READ COMMITTED` leaves both windows open, and
ADR-0004 keeps PostgreSQL a supported target.

- `seed_providers()` is covered by the unique constraint on `provider.key`. The
  loser gets an `IntegrityError` and fails to start, which is the right
  outcome — seeding is the first thing an instance does, so refusing costs
  nothing and a duplicate provider row would cost a great deal.
- `single_source` gets a constraint it was thought not to be able to have.
  `record()` writes `field_provenance.sole_source` — true where the registry
  calls the field `single_source` and the row is not the user's override — and
  a partial unique index over `(entity_type, entity_id, field)` refuses the
  second one. The flag is computed from `STRATEGIES` at the write, so the
  migration names no fields and the two cannot drift.

The losing writer's `IntegrityError` is not caught and turned into something
softer. A raced pair is not a state to recover from in place: the run fails,
rule 1 keeps a failed run from removing anything, and the write it lost is one
a correctly-behaved provider would never have attempted.

Alternatives considered:

- **Re-read after the insert, inside the same transaction, and roll back on
  finding a rival.** Redundant on SQLite, where the transaction has already
  done it, and no answer on PostgreSQL, where the rival's uncommitted insert is
  invisible to exactly the query meant to find it.
- **A partial unique index naming the fields in its predicate.** The reason the
  issue concluded no constraint could express this. It is also true: the
  predicate would list `title`, `metacritic_score`, … and drift from
  `STRATEGIES` the first time a field is added. Writing the flag instead of
  naming the fields is what avoids it.
- **Serialising runs across providers.** Contradicts rule 4, which exists so
  that an Epic outage cannot affect a Steam sync.
- **Catching the `IntegrityError` under a savepoint and continuing.** Costs a
  `SAVEPOINT` on every first write of every field — thousands on a first sync —
  to recover from a pair that requires a provider to have written outside its
  lane. The failure is worth more than the recovery.

## Consequences

- The one case issue #20 found unreportable — a raced pair on a field the user
  has also overridden, where the override wins before `_only` runs — no longer
  exists, because the pair no longer does.
- `_check_sole_source()` stays. It names the offending provider while it is
  still in hand; the index only says that two rows may not both be there.
- A field whose strategy changes in a later release keeps stale flags on rows
  written under the old one. Changing `STRATEGIES` for an existing field is a
  migration, and the flag is part of what it has to bring across.
- The backfill leaves an already-raced group entirely unflagged rather than
  picking one of its rows as the real one. `_only` goes on refusing that group
  at every resolve, so the misclassification stays visible instead of being
  quietly blessed by the migration.
