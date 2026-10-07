# ADR-0037 GOG is read through GOG Galaxy's own endpoints

Status: accepted, 2026-10-07.

## Context

GOG is the third platform the owner uses, with about 200 games. It has no
documented library API (#128), which puts it where Epic was (ADR-0025). Every
endpoint and field below was measured against the owner's library on
2026-10-07 before code relied on it. The fixtures in
`backend/tests/fixtures/gog/` are those answers, trimmed, with the tokens, the
user id and the profile name replaced.

## Decision

**GOG Galaxy's own sign-in, as Heroic and lgogdownloader use it.** The user
signs in on gog.com through Galaxy's public client (`46899977096215655`).
GOG redirects to a nearly empty page whose address carries a code, and the
user pastes back the address or the code. The code is exchanged at
`auth.gog.com/token` for a refresh token, which is what the account keeps.
The exchange is a `POST` with a form body (measured: accepted), so no token
travels in a URL. Ludarium never sees the password.

**The refresh token is kept as it is.** Measured on every refresh made
during #128, by `GET` and by `POST`: GOG returns the same refresh token, unlike Epic, so `renewed_secret`
stays None. An access token lasts an hour, and every sync asks for a new one.

**The library is three answers.**

- `embed.gog.com/account/getFilteredProducts?mediaType=1` names the account's
  games: 190 over two pages of 100, in English, with id, slug and category.
  This is the list a library is made of, and it is read whole or not at all.
- `embed.gog.com/user/data/games` lists every owned id: 362 measured. Of the
  ids the product list does not name, the public catalogue
  (`api.gog.com/products?ids=`, 50 at a time) types 41 as packs, whose games
  are already in the list, and 6 as add-ons. 125 are known to neither the v1
  nor the v2 catalogue (404). They are not counted as unreadable: a short
  count would make every run partial, and a partial run never sweeps (rule 1).
- An add-on's game is the `requiresGames` link of `api.gog.com/v2/games/{id}`.
  All six measured add-ons named a game the account owns. An add-on v1 lists
  and v2 answers 404 for keeps its place without a game, rather than failing
  the library.
- A catalogue that names none of the ids it was asked about fails the run.
  Measured, it named 47 of 172, so an empty answer is an outage, and read as
  "no add-ons" the sweep would remove every add-on (rule 1). A partial answer
  cannot be told from ids it never knew; its cost is an add-on marked removed
  until the next run lists it again, which restores it.

**Playtime comes from the profile's statistics.**
`www.gog.com/u/{username}/games/stats` answers with the same bearer token:
191 games over four pages of 50, each with `playtime` and `lastSession` under
the user id, or an empty list (41 of 191) where there is nothing to say. The
endpoint is keyed by the profile's name, not the user id (measured: the id
answers 404). The name is read from `embed.gog.com/userData.json` on every
sync and kept nowhere, since the user can rename it. `playtime` is read as
minutes. If the statistics fail, the library fails with them. A library whose
playtime silently went missing would lower every total until the next run.

**Every GOG copy is `owned`.** Nothing in the answers tells a giveaway from a
purchase. `acquired_at` is left empty: Galaxy's own library service carries an
`owned_since`, but only for 123 of 337 entries.

**No store link.** A product page is keyed by its slug, and `/game/{id}`
redirects to the catalogue's front page (measured). The template needs the id,
so GOG gets none, as Epic does.

**Layer 1 matches by product id.** IGDB's `external_games` holds GOG product
ids under source 5, which `ludamatch` already reads. Measured on the owner's
library, IGDB named 179 of 190 games. The anchoring that Steam used is now
keyed by store, and GOG runs it after Steam and Epic, so a GOG copy of a game
already anchored elsewhere folds into that work. A GOG-only game then gets
IGDB's Steam appids recorded, as an Epic-only game does, so the store's
reviews and RAWG can find it.

**A refused token is redirected, not answered 401.** Measured: the account
endpoints on `embed.gog.com` send an unknown bearer to the sign-in page with a
302. A token minted a moment before cannot be the problem, so a redirect there
is read as the sign-in having ended, and the UI offers to sign in again. On
the profile's host it was not measured, and a redirect there is an answer the
code cannot read, not a reason to sign in again.

## Consequences

- GOG can change any of this without notice. When it does, the GOG run fails
  with its own status and the other platforms' syncs are untouched (rule 4).
- 125 owned ids are invisible to Ludarium. If any of them turns out to be a
  game, it is because the catalogue started knowing it, and the next sync picks
  it up.
- The client id and secret are Galaxy's, public in every install, and in the
  code with a `gitleaks:allow`. They identify the program asking, not the user.
