# ADR-0014 No per-field history; provenance rows are updated in place

Status: accepted, 2026-08-10

## Context

`field_provenance` holds one row per (entity, field, source). When a source
changes what it reports — Steam renames a product, IGDB corrects a release year
— that row has to be either updated or superseded.

Keeping every version is tempting: it would answer "when did this change", "what
did Steam call it before", and it would make a bad enrichment run traceable
after the fact.

## Decision

The provenance table is a snapshot, not a log. A row is updated in place, and
the previous value is gone.

An audit trail is kept only where a decision has to be **reversible**:
`match_audit` records links, unlinks, relinks and merges, because rule 6
requires every automatic merge to be undoable. Values are not decisions in that
sense — a wrong value is corrected by a manual override, not by rolling back to
an earlier one.

Alternative considered: **append-only provenance with `valid_from` /
`valid_to`.** Full history, standard pattern. Rejected: the table would grow
with every sync of every field forever on a machine whose whole design
constraint is running comfortably on a NAS, every read would need a "currently
valid" predicate, and the unique constraints that keep one row per source would
have to be rewritten as partial indexes over the open interval. Nothing in the
product reads history; the cost is paid continuously for a feature nobody
requested.

## Consequences

- `field_provenance` stays proportional to entities × fields × sources, and
  independent of how often syncs run. On a 5000-title library it stays small
  enough to keep fully indexed.
- "Steam used to call it X" is unanswerable. A title that changes upstream
  changes here with no trace.
- Debugging a bad match loses the earlier values. `entitlement.raw_payload`
  keeps the last provider record, so the most recent state is recoverable, but
  nothing before it.
- If history is ever needed, it arrives as a separate append-only table fed by
  the resolver, not as a change to this one. That keeps the decision reversible
  at the cost of one migration rather than a rewrite.
