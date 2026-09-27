# ADR-0026 Epic works are anchored through their store offers, with the launcher's app token

Status: accepted, 2026-09-28

## Context

Layer 1 (ADR-0021) anchors a Steam work by its appid. #64 measured that no
identifier in an Epic library record is IGDB's `uid` for Epic: IGDB holds the
store's **offer id**, and the library lists catalogue items. #74 asks for a
route from one to the other, and for Metacritic on Epic-only games, which the
RAWG step (ADR-0023) could not confirm: it believed a candidate only by a Steam
appid the user owns the game under.

## Decision

**Namespace → `BASE_GAME` offers → IGDB.** The catalogue lists a namespace's
offers (`catalog/api/shared/namespace/{ns}/offers`), paged by 100. Its
`BASE_GAME` offers are looked up in IGDB `external_games` through `ludamatch`
(`Store.EPIC`), and a work is anchored when they name exactly one IGDB game.
Two games → nothing (rule 6).

**With the launcher's app token, not anyone's sign-in.** Measured: a
`client_credentials` token for the launcher's client is enough for `offers`.
So matching never spends a user's rotating refresh token (ADR-0025), and runs
without an Epic account connected to the step at all. The token is held in
memory for its four hours.

**A namespace holding several of the library's games anchors only the one
titled as its base offer.** 17 of 514 namespaces in a measured library held a
game beside its beta, test branch, soundtrack or editor — Killing Floor 2 and
KillingFloor2Beta, Death Stranding and Death Stranding Content. Anchoring each
to the namespace's one game would fold the test client into the game. The work
whose title equals the base offer's, through `ludamatch.normalise_title`, is
anchored; the rest stay stubs.

**After Steam, in the same `igdb` run.** An Epic copy of a game a Steam work
already holds is folded into it by `merge_work`, as a second Steam stub is
(ADR-0022), so a game owned on both platforms is one card. An Epic sync now
triggers `igdb` and `rawg`; Epic states item kinds itself, so there is no
classification step to wait for.

**IGDB's Steam appid, for works with no Steam copy.** After anchoring, every
anchored work owned on no Steam account gets the appids IGDB files its game
under (`external_game_source = 1`) as authoritative `steam` rows in
`external_id`. The RAWG step reads those beside the entitlements' appids. The
appid is then IGDB's word rather than the user's ownership, but it is a hard id
all the same, and RAWG must still list it.

## Measured

On a copy of a real library (533 Epic games, 197 Steam), live IGDB and Epic:

- 423 Epic works anchored in 84.5 s, 17 of them folded into Steam works owned
  on both platforms. The 15 least similar title pairs were all right —
  subtitles and editions (`DARQ` → `Darq: Complete Edition`).
- 371 works gained IGDB's Steam appid.
- Of 406 anchored Epic-only works, 192 got a Metacritic score (47%; Steam's own
  are 101 of 186, 54%). The RAWG run took 213 s.

## Consequences

- One catalogue request per namespace, cached a month; about 80 s for a
  500-namespace library on its first run.
- A game IGDB files its Epic offer under as an edition (`Layers of Fear:
  Masterpiece Edition`) takes that entry's title and cover.
- Games in a multi-game namespace whose title differs from the offer's stay
  stubs. Wrongly unmatched is the cheaper mistake.
