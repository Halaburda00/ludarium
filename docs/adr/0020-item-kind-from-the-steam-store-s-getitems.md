# ADR-0020 `ItemKind` comes from the Steam store's `GetItems`, and unclassified is not a game

Status: accepted, 2026-09-17

## Context

Running M1 against a real 197-game Steam library found six entries that are not
games: two test clients (`Hunt: Showdown 1896 (Test Server)`, `Conan Exiles -
Public Beta Client`) and four playtests (BattleBit Remastered, New World,
Vaultbreakers, Frostrail). Each has its own work. Matching layer 1 (#47)
resolves an appid to a game, and a playtest's appid resolves to the game it
tests. Rule 6 ranks that below leaving the playtest unmatched. `GetOwnedGames`
carries no field that separates any of them from a game (#41).

#41 proposed `store.steampowered.com/api/appdetails`, whose `type` field reads
`game`, `dlc`, `demo`, `music`, and so on. Measured against those six appids it
answers `game` for all six. It also takes one appid per request.

Two questions were already open. `work.item_kind` defaulted to `game`, so a
stub nobody had classified read as a game. And ADR-0019 left #41 to decide
whether store data runs under `steam` or as a provider of its own, and whether a
post-sync trigger that finds a run already open is good enough.

## Decision

**The kind comes from `IStoreBrowseService/GetItems`.** It is public and needs
no key. For the four playtests it answers type 12 and names the parent game.
All 197 appids went in one request. The endpoint is undocumented, so the
mapping is what was measured:

| Code | `ItemKind` | Measured on |
|---|---|---|
| 0 | `game` | 188 of the library |
| 1 | `demo` | seven demos the library's games link to |
| 2 | `mod` | Defence Alliance 2, NEOTOKYO, Archolos |
| 4 | `dlc` | Hearts of Stone, Blood and Wine, Columbia's Finest |
| 6 | `tool` | RPG Maker XP |
| 11 | `soundtrack` | Portal 2 Soundtrack |
| 12 | `playtest` | the four playtests above |

A code not in the table asserts nothing. `video` is an `ItemKind`, but no sample
has shown its number.

Four more facts were measured and are held by the client:

- It accepts GET only (POST gets 405). The ids go in the query string, and a
  URL past about 8 KB gets 400: 250 seven-digit appids passed and 300 failed.
  A request carries at most 200.
- Without `country_code` the answer is `200 {"response": {}}` whatever was
  asked. Cached, that would mark every app as unknown, so an answer that does
  not account for every app asked about is refused.
- A delisted app (Firefall) comes back with `success` 15 and `appid` 0. Only
  `id` still names it, so items are keyed by `id`.
- `parent_appid` alone does not mark a playtest. Shogun 2 carries one, and
  Medieval II names itself.

**`playtest` is a new `ItemKind`.** A demo is a slice of a released game, and a
playtest tests one before release. A filter for either should not return the
other.

**`work.item_kind` is nullable, and null means unclassified.** The `game`
default stated a fact nobody had checked. With null, the matcher can take only
works known to be games, so this issue running before #47 becomes a mechanism
rather than a build order. The migration nulls `game` only where no provenance
row asserts a kind.

**Store data runs as its own provider, `steam_store`.** It is seeded with kind
`metadata` and source kind `platform_api`. It is Steam describing its own apps,
so it ranks with Steam, above IGDB. Having its own row means a store outage is
the store's failed run and does not overwrite the library sync's health.

**The step records provenance on the primary work, and only when the kind is
known.** An unknown app records nothing rather than null: under `precedence` a
null row still outranks IGDB's answer. A kind the store stops answering for
keeps its row, because the store forgetting a delisted app is not evidence the
app changed. Answers are cached for 30 days.

**A successful sync triggers the step after the response has gone, and
`POST /api/enrichment/{provider}` triggers it by hand.** When a run is already
open, the post-sync trigger is skipped and logged, not queued. That run, or the
next sync, asks about what this sync added. Before M4 a queue would guard
against a window of seconds, since one store request covers 200 apps.

Alternatives considered:

- **`appdetails`.** Measured wrong on every entry the issue is about.
- **IGDB `category`.** It only answers for apps that matched, and a playtest is
  exactly what should not match.
- **Filtering on the name.** #41 records a substring check calling Lost Ark a
  soundtrack because it contains `ost`.
- **Mapping playtest to `demo`.** No enum change, but M3's filters could not
  tell the two apart.
- **Running under `steam`.** No new provider row, but a store outage would
  overwrite the library's health, and the two would share one open-run slot.

## Consequences

- Two of the six are still classified `game`. The test server and the public
  beta client carry type 0 and no parent in the store, and no field there
  separates them from the games they belong to. A heuristic would be the
  name-based guess rule 6 forbids. They stay a false `game` until a user
  overrides them (rule 3), or until another source knows better.
- `WorkSummary.item_kind` can be null. The frontend has nothing that displays it
  yet.
- Mods are classified `mod`, including total conversions a user thinks of as
  games (Archolos). That is Steam's label, and an override wins over it.
- Classification depends on an undocumented endpoint. A change in its shape
  shows up as failed `steam_store` runs rather than wrong kinds, because the
  client refuses what it does not recognise.
- The requests say `country_code=US`. An app the store hides in the US would be
  cached as unknown. No such app was found in the library, and nothing was
  measured from another region.
