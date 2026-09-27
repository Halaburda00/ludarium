# ADR-0022 Layer 1 merges on a shared IGDB id, and an undo restores under a new id

Status: accepted, 2026-09-27

## Context

ADR-0021 left a second work naming an already-held IGDB game as a stub, counted
and logged, for `merge_work` (#48). `docs/schema.md` specified the merge table
by table, but not four things the implementation had to decide: whether layer 1
calls it, what the undo gives the source back as its id, what the payload has
to carry beyond "the ids of everything moved", and where the orphan-stub job
runs while there is no scheduler.

## Decision

**Layer 1 merges without asking.** A shared IGDB id reached through
`external_games` is a hard id, the one match rule 6 trusts outright. The work
already holding the game is the target, whether an earlier run anchored it or
this one did a moment ago, and the older stub is anchored first, so the target
is the older row where neither was matched. The merge is audited
(`match_audit`, `merged`, layer `hard_id`, actor `auto`) and undoable. A merge
that meets a `single_source` conflict is refused before any write and both
cards stay; with no review queue yet, the refusal is logged.

**The source of a merge is never anchored.** Two anchored works name two
different IGDB games, which is not a duplicate, and an anchored source would
carry an `external_id` the target's `igdb_id` copy contradicts.

**An undo restores the source under a new id.** SQLite hands out the highest
rowid plus one, so a freed highest id is reused by the next insert — and the
source of a merge is usually the youngest work, the one holding the highest
id. Restoring under the old id would work until it did not. The `unmerged`
audit row carries the new id, and older audit rows the merge repointed are
pointed at it.

**The payload holds whole rows for everything the merge deleted**, not just
their ids: the source work, the editions that collapsed, the links and
provenance rows that lost a collision, and the source's user state. It also
holds the target's previous value for every field whose provenance the fold
brought in, because a field the source alone asserted has no row left on the
target after an undo and would otherwise keep the source's value. The undo
moves back only rows still where the merge put them; one that changed hands
since is left alone rather than pulled out from under whoever moved it.

**The orphan job runs at the end of the `igdb` step**, in its own transaction,
until M4 brings a scheduler. A removed entitlement keeps its link (rule 1), so
the stub behind it is never an orphan; the job deletes works, never
entitlements. An unreached work that is matched, a parent, or
carrying user state or a `manual` row is kept and counted in the log.

## Consequences

- Two stubs for one game become one card on the first run that anchors either,
  with no user action.
- An undone merge restores the card with a different id. Nothing outside the
  database holds work ids yet; a bookmarked URL, once there is one, would not
  survive an undo.
- Every table added later that references a work has to be added to
  `ludarium.merging`, in both directions, or a merge will leave its rows
  pointing at a deleted work and an undo will not bring them back.
- The payload has a `version`, and an undo refuses one it cannot read rather
  than half-restoring it.
