# ADR-0023 Metacritic scores come from RAWG, confirmed by a Steam appid, and never without their link

Status: accepted, 2026-09-27

## Context

`work.metacritic_score` and `metacritic_url` were in the schema from the start,
`single_source` and RAWG's only. Three things were left open. RAWG has no
lookup by a store's id — only a search by name — so how a work is tied to a
RAWG game. What RAWG's terms ask for: free "as long as you attribute RAWG as
the source of the data and add an active hyperlink from every page where the
data of RAWG is used", with "no data redistribution" and 20 000 requests a
month on the free tier. And where the attribution lives, given that
`provider.attribution_html` exists for it.

## Decision

**A search proposes, a Steam appid confirms.** Only works IGDB has matched are
asked about, and by IGDB's name: canonical English, where a stub's title is
the store's and a user's own may be in any language. RAWG's first five
results, additions excluded and fuzziness off, are candidates; one is believed
when RAWG's `stores` for it include a Steam link naming an appid the work is
owned under. The first such candidate wins and the rest are not asked about. A
work with none gets no score — *Prey* (2006) and *Prey* (2017) share a name,
and a missing score costs less than someone else's (rule 6).

**What is cached is what the step needs, not what RAWG said.** Searches keep
candidate ids, stores keep Steam appids, games keep slug, score and Metacritic
link — in `fetch_cache`, for 30 days, under the `rawg` provider, which is
`runtime_only` and so dropped from every export. No RAWG response is recorded
in the repository, test fixtures included: they are invented in RAWG's
published shapes.

**The score is provenance; RAWG's slug is an `external_id`.** The step records
`metacritic_score` and `metacritic_url` as RAWG's rows and the resolver writes
the columns (rule 9). A score outside 0–100 is recorded as none, and so is a
Metacritic link that is not `https`. The slug is an `external_id` in namespace
`rawg`, not authoritative — the matcher inferred it, from a hard id — and a
slug another work holds is left with it and logged.

**The API serves a score only with its attribution.** `WorkSummary.metacritic`
is one object: the value, the source's name, and the game's page on RAWG,
built from the slug and the `rawg` provider's `store_url_template`. A score with
no slug to link to is not served at all, so no client can show one without its
link. The UI links each score to that page and, below the table while any
score is shown, credits RAWG with a link to its home — a translated string, not
`attribution_html`, which stays unused: the UI does not render HTML from the
database.

**The step runs after matching, on every Steam sync**, and is skipped without a
key, as matching is without an IGDB application.

## Consequences

- About three requests per game — search, stores, record — once a month, and
  more for a game whose right candidate is not the first. A library of 6 000
  matched games would spend the free tier; nothing counts requests yet.
- A game RAWG does not sell on Steam, or lists without a Steam link, gets no
  score. Epic-only games get one only once something gives them a Steam appid,
  which is M2b's plan for their review scores too.
- `metacritic_url` is stored and served nowhere yet. It is RAWG's data about
  Metacritic, not the attribution, and the detail view (#54) can decide whether
  it earns a place.
- The attribution requirement is enforced where the score leaves the backend.
  A future client — the grid, the detail view — inherits it by using the same
  object.
