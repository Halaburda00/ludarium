# ADR-0038 The GOG Galaxy database is not imported

Status: accepted, 2026-10-09.

## Context

#131 planned an upload of `galaxy-2.0.db`, on the premise that Galaxy's
integrations had already gathered EA, Ubisoft and Battle.net libraries into one
file, so that reading it reached three platforms without three clients. The
issue asked for a measurement before any parser. It was taken on 2026-10-09,
on a copy of the owner's file, opened read-only, with Galaxy 2.1.10.56 closed.

## What was measured

**The three platforms are not there to read.** Galaxy 2.1.10.56 lists three
official integrations: GOG, Epic and Xbox Live. EA, Ubisoft and Battle.net were
only ever community plugins, from FriendsOfGalaxy, and checked on 2026-10-09
they are dead:

| Plugin | Last push | State |
|---|---|---|
| `galaxy-integration-origin` | 2023-10-24 | "Origin is discontinued, needs an update to support new EA App" is open |
| `galaxy-integration-uplay` | 2023-10-09 | an open issue titled "UNMAINTAINED"; a June 2026 forum report says the known fix no longer works |
| `galaxy-integration-battlenet` | 2020-01-10 | archived |
| `galaxy-integration-blizzard` | 2021-11-09 | its successor, untouched since |

A library reaches the file only through a plugin someone patches by hand, and
those patches break whenever the platform's sign-in changes.

**Xbox, the one live integration Ludarium has no client for, reports play
history, not ownership.** With Xbox Live connected, the file carried 87
`xboxone_*` entries in `LibraryReleases`, every one marked `isOwned = 1`.

- 85 of them have playtime. By the owner's account they are games played
  through a Game Pass subscription that has since lapsed.
- None of the 13 items the Xbox app shows in the same account's library
  appears among them, by title.
- `ProductPurchaseDates.purchaseDate` is empty for all 87, and `addedDate` is
  the moment the integration was connected, identical for all of them. There is
  no acquisition date.

So the file has no field that separates a purchase from a subscription play,
and it misses what was actually bought. Importing it as `owned` would invent a
library; importing it as `subscription` would mislabel every real purchase.
Subscription catalogues are out of scope in any case.

What would have worked, for the record: `ExternalAccounts` holds the platform's
user id (a 16-character XUID for Xbox), a stable `external_account_id`;
`GameTimes.minutesInGame` and `LastPlayedDates` are filled; and the
`allGameReleases` game piece maps each release to its keys on other platforms
(`steam_…`, `epic_…`, `psn_…`). That mapping is GOG's data, not ours to
redistribute in `ludamatch-data`.

## Decision

**#131 is closed, and no Galaxy import is built.** What remains for platforms
without a client is the CSV and JSON import (#130). A tool with live plugins
for EA, Ubisoft and Battle.net, such as Playnite, can export a library that
import reads.

PSN, the other live community plugin, was not measured: there was no account
to measure it with, and PlayStation Plus would likely raise the same question
of ownership that Xbox did.

## Consequences

- The `galaxy` provider is not seeded. The mechanism `docs/schema.md`
  describes for it stays: a reporter whose provider differs from the account's,
  writing derived accounts at the `local_agent` rung, is what the local agent
  in "Later" needs.
- ADR-0002 counted a Galaxy upload among the costs of being a web app. That
  cost is gone, because there is nothing worth uploading.
- If Galaxy's integrations ever recover, this is reopened by measuring a new
  file, not by reviving the old plan.
