# ADR-0015 Sync creates work stubs rather than deferring work creation to the matcher

Status: accepted, 2026-08-10

## Context

The three-level model (ADR-0005) puts `Work` above `Edition` above
`Entitlement`. The obvious reading is that a work appears once something
establishes what the game *is* — that is, once the matcher runs.

That reading breaks the milestone order. M1 ships a Steam sync and a grid with
no matcher at all, so under deferred creation the grid would list entitlements.
M2 adds the IGDB client and the enrichment pipeline, which enrich works — but
the cascade layers that create works were scheduled for M4, leaving M2 with
nothing to attach metadata to. `user_work_state` hangs off `work_id`, so a user
could not mark anything as *playing* until the matcher shipped. And the grid
would need two query shapes, one for matched games and one for loose
entitlements, until M4 and then permanently, since unmatched entitlements never
go away entirely.

## Decision

Sync creates a work stub for every new entitlement, in the same transaction:
a `work` row with `is_matched = false` and `title` copied from `provider_title`,
a default `Standard` edition, and a `primary` link. There is no state in which
an entitlement has no work.

Enrichment attaches the IGDB anchor later and flips `is_matched`. Matching
merges stubs that turn out to be the same game, through
`merge_work(source, target)`.

Cascade layer 1 moves from M4 to M2 as a direct consequence: a stub must be able
to acquire its anchor in the milestone where the IGDB client lives.

Alternative considered: **defer work creation to the matcher**, with the grid
unioning works and orphan entitlements. Rejected for the four problems above,
of which the M2 sequencing one is fatal rather than merely awkward.

## Consequences

- One query shape for the grid from M1, and `user_work_state` works from the
  first sync — a status can be set on a game the matcher has never seen.
- Deduplication becomes a merge of two existing rows rather than a creation.
  Rematching needs a merge operation regardless, so this is one code path
  instead of two.
- The database carries near-duplicate work rows until matching runs, and the
  user sees them: the same game bought on two platforms is two cards until a
  cascade layer merges the stubs. This is visible in M1–M2 and is the honest
  state, but it will be reported as a bug at least once.
- `merge_work` has to move every kind of referencing row — links, editions, user
  state, provenance, pins, external ids, tags, images, embeddings, audit rows —
  and stay reversible under rule 6. It is the most intricate operation in the
  system and needs the heaviest tests.
- Orphaned stubs accumulate when entitlements are removed or relinked, so a
  periodic cleanup job is required. Deleting the wrong stub destroys user state,
  so the job has to check for user-authored data before removing anything.
