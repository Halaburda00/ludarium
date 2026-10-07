# ADR-0034 The demo is a made-up library, shown read-only

Status: accepted, 2026-10-07.

## Context

#99 asks for a way to see Ludarium without connecting an account: for the
screenshots M5 needs, for a public demo, and for anyone deciding whether to
install it. A demo has to show every M3 feature, and it has to stay within
the legal constraints in `CLAUDE.md`: no IGDB or RAWG data in the repository
or the image.

## Decision

**The games are invented.** Titles, release dates, genres and summaries are
written for the demo (`ludarium.demo`), so none of them comes from IGDB or
RAWG, and the dataset needs no network. Real games from Wikidata (CC0) were
considered and turned down. Their scores and covers would arrive live from
enrichment, so the demo would differ from day to day and need the keys a
visitor does not have. Screenshots of real cover art in the repository are
also a question this avoids.

**A made-up game has no score and no store link.** The API serves a score only
with the page it links to (RAWG's terms, and ADR-0027 for Steam), and a game
that does not exist has no page. A link built from a made-up Steam appid is
also a real store page about some other game. So a demo seeds its providers
without store URL templates, and no work in it has a Metacritic or Steam
score. Sorting and filtering by score have nothing to show in the demo. That
is the price of not inventing attributions.

**The library is built through the paths a user's library takes.** The
platform copies arrive through `sync_account`, from a provider that always
answers with the same list, and a second Steam run without one game marks it
removed, as a real platform would. The manual copies go through the manual
entry endpoint, and the statuses, the queue and the saved views go through
their own endpoints, called in process. What IGDB would say about a game
(year, date, summary, genres) is recorded as provenance under the source
`demo`. A demo assembled from direct row writes could hold a state the API
refuses, and would show a library no user can have.

**Read-only, and signed in as one visitor.** With `LUDARIUM_DEMO` set, a
middleware refuses every method but `GET`, `HEAD` and `OPTIONS` with 403, by
method rather than by route, so an endpoint added later is covered without
being listed. `current_session` answers every request with a session that is
never stored, for the single user. A public demo anyone can change is one
someone will deface. The client learns it is a demo from `/api/health`, and
hides the actions it would refuse.

**Only on an empty database, and only the demo on it.** The seed runs on a
database with no accounts and no works. On a later start it accepts a database
whose accounts are exactly the demo's: Steam and Epic with the external id
`demo`, which no real account has, and the manual account. It refuses
anything else, so turning the flag on over a real library fails at start
instead of showing that library to anyone who opens it. Saved views are made
last, so a demo without them was left half-made by a failed start, and that is
refused too.

## Consequences

- The username and password are still required, and in a demo nobody uses
  them. Making them optional would mean a second shape of configuration for
  one flag.
- Turning the flag off over a demo database gives an ordinary instance with
  made-up games, whose accounts hold no credential and fail to sync. The store
  links come back, since providers are reconciled on every start.
- A visitor who tries a write the client still offers (a status, a saved view)
  sees the 403's message rather than a change.
