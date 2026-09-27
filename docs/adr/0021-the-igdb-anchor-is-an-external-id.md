# ADR-0021 The IGDB anchor is an `external_id` row, and layer 1 anchors only games

Status: accepted, 2026-09-27. Its handling of two stubs naming one game is
amended by ADR-0022: the younger is now merged into the anchored work.

## Context

Matching layer 1 (#47) looks a Steam appid up in IGDB `external_games` and
anchors the work to the game it names. `docs/schema.md` already had the columns
for the result — `work.igdb_id`, `work.is_matched`, and an `external_id` table
described as "the only place an ID from another system is authoritative" — but
no table, and nothing saying who writes the two columns. Rule 9 says only the
resolver writes entity columns, and neither column is in its registry.

Three more questions came with it. Which works are asked about. What happens
when two stubs name one IGDB game, which the unique index on `work.igdb_id`
refuses and `merge_work` (#48) does not yet resolve. And whether the title
changes, and with it the sort order.

## Decision

**The anchor is an `external_id` row: `namespace = 'igdb'`, authoritative,
`source_ref` the provider that asserted it.** `work.igdb_id` and
`work.is_matched` are copies of it, written in the same transaction, the way
`sort_key` is a copy of `sort_title`. They are not resolved fields and are not
in `STRATEGIES`: nothing competes to assert them, so there is no ladder to run.
Rule 9 is about providers asserting values; a match is the matcher's decision,
and the decision's record is the `external_id` row. A later layer that unlinks
a work deletes that row and resets both copies.

**The query and the reading of its answer are `ludamatch`'s**
(`match_by_external_id`). An appid IGDB does not know, or knows under two games,
is not a match. The step runs as the `igdb` provider through the enrichment
pipeline, so answers are cached for 30 days, including the answer "unknown".

**Only works whose `item_kind` is `game` are asked about.** ADR-0020 made null
mean unclassified so that this could be a filter rather than a build order. A
DLC's appid would resolve to the DLC's own IGDB entry, which is a correct
match, but folding DLC under its game is M3 and nothing needs the anchor before
then; asking about games only keeps this layer to the case ADR-0020 argued for.

**Two stubs naming one game: the older is anchored and the younger stays a
stub.** `docs/schema.md` gives the anchor to the older row when neither is
matched, and a work anchored by an earlier run keeps it. The younger is counted
and logged, and it is `merge_work`'s to fold in (#48). Two cards for one game is
the honest state until then; the unique index refuses the alternative anyway.

**IGDB's name is recorded as provenance for `title` and for `sort_title`.** The
title is `single_source` and IGDB is its only source, as rule 5 and
`docs/schema.md` already say. `sort_title` is derived from the same name, as the
stub's was derived from the store's, so a card does not read one name and file
under another. A user's own value for either outranks both (rule 3). A game
IGDB gives no name is anchored all the same and keeps the store's.

**Without an IGDB application the step is skipped, not failed.** The
application is optional (`Settings`), and a red status after every sync for
something the user never set up would be noise. Asking for the step by hand
answers 400 saying it is not configured. After a Steam sync the step runs after
classification, never beside it, because it reads what classification wrote.

## Measured

On a copy of the real 197-game library, against live IGDB, after
classification:

- 186 of the 188 games anchored. The two left are `Conan Exiles - Public Beta
  Client` and `Hunt: Showdown 1896 (Test Server)`, which the store calls games
  (ADR-0020) and IGDB does not map. Neither got a false anchor.
- No two works named the same game.
- 59 works took a new title — trademark signs dropped, `GOTY` spelled out,
  `Baldur's Gate 3` as `Baldur's Gate III`. One is IGDB's own reading of an
  appid rather than a spelling: `Ancestors Legacy Free Peasant Edition` became
  `Ancestors Legacy Multiplayer Open Beta`, the entry IGDB files that appid
  under.
- The first run asked about 188 appids and 186 games in 2.27 s. The second
  asked about the 2 unmatched appids and sent nothing, in 0.01 s.

## Consequences

- `work.igdb_id` and `work.is_matched` are written outside the resolver, and
  every writer of the anchor has to write all three together. There is one
  today, `matching._anchor_one`.
- A work anchored to the wrong game is fixed by removing its `external_id` row,
  not by a provenance row that outranks one. That path, and its audit trail,
  arrive with #48.
- Test clients the store calls games stay unmatched only because IGDB does not
  map them. A test client IGDB did map would be anchored to its game. The
  measured library has none; the gap is ADR-0020's and is recorded there.
