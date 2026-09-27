# ADR-0025 Epic is read through the launcher's own endpoints, and its token rotates

Status: accepted, 2026-09-28

## Context

Epic has no public library API. #64 asks for Epic's library as the second
platform, and says the choice of an unofficial flow gets an ADR of its own
because it can break without notice and nobody owes us a fix.

## Decision

**The Epic Games Launcher's own flow, as legendary uses it.** The user signs in
on epicgames.com through the launcher's public client
(`34a02cf8f4414e29b15921876da36f9a`) and pastes back the authorization code the
redirect page shows. The code is exchanged for a refresh token, which is what
the account keeps. Ludarium never sees the password. The client id and secret
are the launcher's, public in every install; they identify the program asking,
and the user's sign-in is what grants access.

Every endpoint and field was measured against a real library before code relied
on it (#64, 2026-09-28): the token endpoint, the library service
(`library/api/public/items`, 709 records in 7 pages, 1.6 s) and the catalogue
(`catalog/api/shared/namespace/{ns}/bulk/items`, one request per namespace,
English titles). The fixtures in `backend/tests/fixtures/epic/` are those
answers, trimmed, with tokens and account replaced.

**The refresh token is replaced on every use.** Measured: each refresh returns
a new one, valid for a year. The provider holds the new token in
`renewed_secret`, and the sync writes it back in the transaction that closes
the run — after the rollback a failed run makes, so a failure keeps it too. The
old token answered once more when reused at once, but nothing relies on that.

**What the library holds is filtered by the catalogue's categories.** `addons`
(or a main game) → DLC; `games` → game; `applications` or `engines` → tool;
anything else — Unreal Marketplace and Fab assets, plugins, sample projects —
is not a library item and is left out. The library lists an item once per
build, so records are made unique by catalogue item. An item the catalogue does
not answer for keeps its `appName` as a title and no kind rather than failing
the library. `provider_item_id` is the catalogue item id; the namespace is in
`raw`.

**Every Epic game is `owned`.** The catalogue's `freegames` category marks
claimed weekly giveaways, not free-to-play games, and it carries no marker for
free-to-play at all (Fortnite, Genshin Impact and Fall Guys have none), so
nothing can honestly be called `free`.

**A failure says who can fix it.** `sync_run.error_kind` records the kind of
provider error that ended a run. An ended sign-in is `credentials`, and the UI
offers to sign in again; an outage is `unavailable`, and it does not. Connecting
an account that is already connected replaces its credential, which is how the
sign-in is renewed without losing the account's history.

## Consequences

- Epic can change any of this without notice. When it does, the Epic run fails
  with its own status and Steam's sync is untouched (rule 4); the fix is ours
  and nobody else's.
- If a sync's process dies between the refresh and the run closing, the new
  token is lost and the next sync asks the user to sign in again. One click,
  and the price of not writing a credential outside the run's own transaction.
- None of the library's identifiers is IGDB's `uid` for Epic, which is the
  store's offer id. Epic works stay stubs until #74 anchors them through the
  namespace's offers — measured at 464 of 562 games.
- Playtime and installed state are not read (#64 keeps them out of scope).
