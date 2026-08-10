# ADR-0013 `entitlement_work` `role='primary'` is the single source of truth for work membership

Status: accepted, 2026-08-10

## Context

Two paths lead from an entitlement to a work: `entitlement.edition_id →
edition.work_id`, and the `entitlement_work` row with `role = 'primary'`. Both
were introduced for good reasons — the first records which edition was bought,
the second carries the many-to-many that bundles require.

Two paths to the same fact drift. A rematch that repoints the link but leaves
`edition_id` alone, or the reverse, produces an entitlement that belongs to one
work by one route and another by the other. Nothing in the schema notices, and
the two routes are used by different queries, so the symptom appears far from
the cause.

## Decision

The `primary` link is authoritative. `edition_id` records only which edition was
bought; its `edition.work_id` must agree with the `primary` link, and both are
written in one transaction on match, rematch and merge.

The agreement is enforced by a test and by a consistency check the health
endpoint can run — not by a database constraint. The condition spans three
tables (`entitlement`, `edition`, `entitlement_work`), which SQLite cannot
express as a `CHECK`.

Alternatives considered:

- **Drop `edition_id` and derive the edition from the link.** One fact, one
  place. Rejected: it loses which edition was bought when a work has several,
  which is exactly the question "GOG: GOTY · Steam: Standard" answers.
- **Denormalise `work_id` onto `entitlement`.** Fastest to read. Rejected: a
  third copy of the same fact, with the same drift and one more writer.

## Consequences

- Rematching updates one link. Everything downstream — the grid, the
  aggregates, the merge — reads the same route.
- The invariant is unenforced at the storage layer. A bug can commit a row where
  the edition belongs to a different work than the primary link, and only the
  consistency check will catch it, possibly long after. On PostgreSQL this could
  later become a trigger; it is deliberately not written twice.
- Reading entitlement → work always costs a join through `entitlement_work`,
  where a plain FK would not. The `(work_id, entitlement_id)` index exists for
  exactly this.
- `role = 'primary'` must be unique per entitlement, which the merge has to
  respect: a link that would collide with an existing primary is demoted to
  `granted` rather than dropped.
