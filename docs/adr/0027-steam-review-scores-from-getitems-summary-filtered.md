# ADR-0027 Steam review scores come from `GetItems`' filtered summary, of a work's most-reviewed app

Status: accepted, 2026-09-28. Amended by ADR-0029: a score is kept without a verdict.

## Context

#61 asks for the Steam store's user-review score beside Metacritic, for every
work with a Steam appid. Epic-only works are the reason for it: layer 1 keeps
the appids IGDB gives a matched work (ADR-0021), and an appid is all the store
needs. Three things were left open: which endpoint, which reviews count, and
which appid when a work has several.

The store filters reviews by language and by how the game was bought, and its
endpoints do not share defaults. So the question of which reviews count has no
neutral answer. Leaving it to an endpoint's default would still be a choice,
just not one anyone made on purpose.

## Decision

**`IStoreBrowseService/GetItems` with `data_request.include_reviews`.** It is
the endpoint classification already uses (ADR-0020). Measured, it answers for
the whole 197-app library in one request, 5 922 bytes of URL, and a full
batch of 200 eight-digit appids still fits under the 8 KB limit. The
alternative, `store.steampowered.com/appreviews/{appid}?json=1`, answers the
same numbers one app per request.

**Which reviews count: `summary_filtered`.** Each item carries up to three
summaries. Measured for The Witcher 3 (292030):

| Summary | Reviews | Matches |
|---|---|---|
| `summary_filtered` | 824 691 | `appreviews?purchase_type=steam&language=all` (824 277) |
| `summary_unfiltered` | 834 481 | the same, with off-topic review periods counted |
| `summary_language_specific` | 242 156 | the store page's "All Reviews" row for an English visitor |

The "All Reviews" row on the store page is filtered to the visitor's
language. It reads 242 156 in English, 45 765 in Polish and 21 134 in German,
so the number a user checks it against depends on who is checking.
`summary_filtered` is the one summary that is the same for everyone: every
language, bought on Steam (key activations excluded, as the store's score has
excluded them since 2016), and with the off-topic review periods the store
excludes by default. It matched `appreviews` with those parameters to within
0.1% on each of three apps. The gap is caching, not definition. It does not
follow the UI language, because a score that moved with the language setting
would not be one number to sort or filter by.

**The verdict is stored as a value, not as Steam's label.** The store's
`review_score_label` is translated into whatever language the request names.
`review_score` is the stable part: 1 to 9, from Overwhelmingly Negative to
Overwhelmingly Positive, and 0 for no verdict. It is stored as `SteamRating`,
and the UI names it through i18n. Measured: 0, 1, and 4 to 9. Codes 2 and 3
were not found in any sample. They are the two labels left (Very Negative,
Negative), mapped in the order the scale runs.

**Four columns on `work`, written together.** `steam_review_rating`,
`steam_review_percent`, `steam_review_count` and `steam_review_appid`, all
`single_source` under `steam_store`. The step records provenance and the
resolver writes the columns (rule 9). The step always records all four at
once, so they describe one app. A summary with no verdict — code 0, a count of
0, a percentage outside 0–100 — is recorded as four nulls. That is the store's
answer, not a gap: the playtests in the library have no reviews, and their
`percent_positive` of 0 is not "0% positive". A work none of whose apps the
store answers for, such as a delisted one, is left as it was.

**Of several appids, the most-reviewed wins, and a tie goes to the lower
appid.** A work gathers the appids of its primary Steam entitlements, removed
ones included, as classification does, and the appids IGDB gives it once
matched. That can be an old GOTY appid beside the current one, or a
re-release. The most-reviewed app is the page most people read, and a delisted
app has no summary and so can never win. The choice depends only on the
store's answer, never on row order.

**It runs under `steam_store`, chained after classification**, cached apart
under the `reviews` resource for a week, where an app's kind is trusted for a
month. One provider, because both parts ask the same endpoint and one outage
fails both. A Steam sync runs the store before IGDB, as before. An Epic sync
now runs it too, after IGDB, since the appids an Epic game's score needs are
the ones matching has just given it. Because the order is no longer the same
for every sync, `Scheduled` now puts the running step first. Before, it
assumed the steps ran in the order `STEPS` names them.

**Served as one object, with its link.** `WorkSummary.steam_reviews` holds the
verdict, percentage, count and a link to the reviews on the app's store page
(`#app_reviews_hash`, the anchor the store uses itself). A verdict with no
appid to link to is not served. The table shows the percentage and puts the
verdict and count in the link's name.

Alternatives considered:

- **`appreviews`.** One request per app. The batching the enrichment pipeline
  exists for would be wasted.
- **`summary_language_specific`, requested in the UI language.** It would match
  what the user's store page shows. But it would change whenever the setting
  changed, needs a cache per language, and makes a score over a few dozen
  Polish reviews look like one over a million.
- **The first appid found, or the lowest.** Deterministic, but the lowest can
  be a delisted edition with no reviews, which would hide the work's score.

## Consequences

- One request per 200 apps per week, whatever the library's size in works.
- The score can disagree with the store page a user reads, which shows reviews
  in their own language by default. The disagreement is stated here and in the
  link's name ("of N reviews"), not hidden.
- An unmatched Epic game has no appid and gets no score. The same holds for a
  game IGDB lists without a Steam `external_game`.
- Codes 2 and 3 are inferred from the scale's order. If the store ever labels
  them differently, the fix is one line in `RATINGS`.
- A user override of one of the four columns is served only while the other
  three still hold values. Nothing writes such an override yet (`field_pin`
  arrives with its UI).
