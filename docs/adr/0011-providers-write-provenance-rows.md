# ADR-0011 Providers write provenance rows, never entity columns

Status: accepted, 2026-08-10

## Context

A single entity draws values from several sources at once — a platform API, a
metadata provider, the future local agent, the user. Architecture rule 3 says
user edits are never overwritten by a later sync, and rule 5 fixes a precedence
order between sources with per-field exceptions.

If every provider wrote entity columns directly, both rules would be
conventions. The last sync to finish would win, a user edit would survive only
until the next run, and "which source does this value come from" would be
unanswerable — the previous value having been overwritten, there is nothing left
to compare.

## Decision

Providers write rows in `field_provenance`, one per (entity, field, source).
They never write an entity column. A resolver reads the candidate rows, applies
the field's strategy from the resolution registry, writes the winning value to
the entity column and flags that row `is_effective`. A partial unique index on
`(entity_type, entity_id, field) WHERE is_effective` makes two winners
impossible.

Entity columns are therefore a materialised view of the provenance table, kept
for indexing and querying, not the place values live.

Alternatives considered:

- **Last-write-wins plus an `is_user_edited` boolean per field.** Simplest to
  implement and enough for rule 3. Rejected: it needs a companion column for
  every field, it cannot express rule 5's ordering between three machine
  sources, and it cannot show the user what the alternatives were.
- **An append-only event log of field changes.** Would answer every question
  including historical ones. Rejected in ADR-0014.

## Consequences

- Rules 3 and 5 become mechanical. A sync that misbehaves can at worst add a
  losing row; it has no path to an entity column at all.
- The UI can render "from Steam", list the alternatives, and offer pin or
  override as two ordinary row writes.
- Every field update is two writes plus a resolve, and `field_provenance` will
  be the largest table in the database — entities × fields × sources.
- A field missing from the resolution registry silently never resolves. The
  registry needs a test asserting that every resolved entity column appears in
  it; without that, the failure is a permanently empty column with no error.
- Resolution has to run in the same transaction as the write that triggered it,
  or a reader can observe an entity column that disagrees with its own
  `is_effective` row.
