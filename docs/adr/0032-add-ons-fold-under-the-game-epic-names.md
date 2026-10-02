# ADR-0032 Add-ons fold under the game Epic's catalogue names

Status: accepted, 2026-10-03.

## Context

`work.parent_work_id` has existed since M1 "for DLC folded under its parent
game in the grid", and nothing set it. With `ItemKind` classified since M2a,
the grid listed every add-on as a card beside its game. #98 asks where the
parent should come from, measured on a real library before choosing.

Measured on the reference library on 2026-10-03 (806 copies: 609 Epic, 197
Steam; 63 works classified `dlc`):

| Source | Add-ons it could place | Why |
|---|---|---|
| Steam store `GetItems`, `related_items.parent_appid` | 0 of 0 | `GetOwnedGames` never lists DLC, so no Steam add-on reaches the library to ask about |
| IGDB `games.parent_game` | 0 of 63 | The matcher anchors works known to be games; no add-on is matched |
| Epic, the namespace an add-on shares with its game | 42 of 63 | 15 share it with no owned game, and 6 with two "games": a soundtrack or a second build sits beside the real one |
| Epic catalogue, `mainGameItem` | 63 of 63 | Names the game for every add-on: 48 owned games, and 15 games not owned (Train Sim World add-ons from a giveaway) |

## Decision

**The parent comes from Epic's `mainGameItem`.** The catalogue request already
asks for it (`includeMainGameDetails`) and the provider already used it to
classify. It names the game's catalogue item, which is that game's
`provider_item_id` on the same account. The sync states the parent as the
game copy's work, as `platform_api` provenance on `work.parent_work_id`
(rule 9). An add-on whose game is not owned is stated as a null, and stays a
card of its own.

**Folding is decided when the library is listed, against what is owned then.**
An add-on is folded only while its game has a live copy. A game removed by a
sync therefore lets its add-ons back into the grid, and a restore folds them
again without waiting for a run.

**An add-on is folded only under a game the same `hidden` choice lists.**
Hiding a game does not hide its add-ons, which keep their own cards. A hidden
add-on of a shown game is in the hidden view, the one place a hidden work is
un-hidden from. `hidden` is the exception to "every other filter describes the
game", because by default it excludes: a fold under a hidden game would leave
an add-on the user never hid with no card in the default view.

**`addons=fold` is the default and `addons=separate` lists them as cards
again.** It is a filter field like the others, so it is in the address and in
saved views. A folded game's card counts its owned add-ons, and its page lists
them. An add-on's page names its game.

**Search and kind look through to folded add-ons. Every other filter describes
the game itself.** A folded add-on has no card, so a search for its name, or
`kind=dlc`, finds its game instead; without that, a folded add-on could not be
found at all. Year, scores, genre, status and playtime are facts about the game:
an add-on released in 2018 does not make Civilization VI a 2018 game. An add-on
that only such a filter would match is reached with `addons=separate`.

**A merge moves the claims with the column.** `merge_work` already repointed
children. Their `parent_work_id` provenance rows now move too, and so does a
target's claim on the source, which is cleared. Without that, the next resolve
would write the deleted source's id back onto the column. The undo puts back
every claim it moved.

Alternatives considered:

- **The shared namespace.** It needs no new field from the catalogue.
  Rejected: it placed 42 of 63 against 63, and the six ambiguous cases are
  exactly the ones a guess would get wrong.
- **IGDB `parent_game`.** Canonical, and it would cover every platform once
  add-ons are matched. Deferred rather than rejected: no add-on is matched
  today. When the matcher takes add-ons, IGDB's claim joins Epic's on the
  ladder, below it.
- **Fold by a filter on `kind`.** Hiding every `dlc` work would lose the 15
  add-ons whose game is not owned, which the issue requires to stay visible.

## Consequences

- `ix_work_parent_work_id`, a partial index, makes the look-through
  affordable. Measured at 20 000 works with 2 000 add-ons: a search matching
  nothing took 30 ms with it and 21 969 ms without, and `kind=dlc` 14 ms against
  810 ms. The first page, folded or not, took 12–14 ms either way.
- A search that matches nothing now asks the add-ons too: 30 ms at 20 000 works.
- Steam add-ons, should the local agent ever report them, have no parent until
  `GetItems`' `parent_appid` is read. It answers for playtests too, so it is
  the obvious second source.
- `parent_work_id` holds a work id, so anything that renumbers works has to
  move the claims as well as the column. `merge_work` does. A future operation
  that renumbers works has to do the same.
