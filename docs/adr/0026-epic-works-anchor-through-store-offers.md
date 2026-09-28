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
without an Epic account connected to the step at all. The token is minted once
per run and reused for every namespace in it; each run mints its own, which is
one request against the hundreds the offers take, and not worth a token kept
across runs.

**A work is anchored only if a base offer is titled as it is.** 17 of 514
namespaces in a measured library held a game beside its beta, test branch,
soundtrack or editor — Killing Floor 2 and KillingFloor2Beta, Death Stranding
and Death Stranding Content — and a beta owned without its game is the only
game-kind item in its namespace. Anchoring by namespace alone would take either
for the game. So the work's title, through `ludamatch.normalise_title`, must
equal a base offer's or begin it: offers carry edition suffixes the library
leaves off ("Watch Dogs 2" / "Watch Dogs 2 Standard Edition"), betas carry
suffixes the offer does not. Exactly one such work in the namespace is
anchored; otherwise none. Against the 423 anchors of a first measurement
without this check, it keeps 419 — the four lost (Assassin's Creed Syndicate,
Shenmue 3, Ghostbusters Remastered, Shadow Tactics) spell the name differently —
and turns the beta away.

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

- 423 Epic works anchored in 84.5 s before the title check, 419 with it; 17
  folded into Steam works owned on both platforms. The 15 least similar title pairs were all right —
  subtitles and editions (`DARQ` → `Darq: Complete Edition`).
- 371 works gained IGDB's Steam appid.
- Of 406 anchored Epic-only works, 192 got a Metacritic score (47%; Steam's own
  are 101 of 186, 54%). The RAWG run took 213 s.

## Consequences

- One catalogue request per namespace, cached a month; about 80 s for a
  500-namespace library on its first run.
- A game IGDB files its Epic offer under as an edition (`Layers of Fear:
  Masterpiece Edition`) takes that entry's title and cover.
- A game whose Epic title is spelled differently from its offer's stays a stub,
  as four did in the measured library. Wrongly unmatched is the cheaper mistake.
