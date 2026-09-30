# ADR-0029 A Steam score is kept without a verdict, with its review count

Status: accepted, 2026-09-30. Amends ADR-0027.

## Context

ADR-0027 recorded a Steam review summary only when the store gave a verdict
(`review_score` 1–9). Without one, all four columns were written as null. That
rule was meant for playtests, which have no reviews at all: their
`percent_positive` of 0 is not "0% positive". But the same rule also threw
away small games. The store reports their percentage and their count, and
names no verdict only because the reviews are few.

Measured against the 584 review summaries cached by a real instance:

| `review_count` | with a verdict | without |
|---|---|---|
| 0 | 0 | 25 |
| 1, 2, 5 | 0 | 3 |
| 16 and up | 553 | 0 |

The store gives a verdict from somewhere between 6 and 16 reviews. That is
consistent with the 10 the store states, but the data does not pin the
threshold down any closer. Three games lost their score to the rule. On a
library with more small games, more would.

## Decision

**A score is a percentage over at least one review, and the verdict is
optional.** `reviews.values_of` records `steam_review_percent`,
`steam_review_count` and `steam_review_appid` whenever the count is at least
one and the percentage is within 0–100. `steam_review_rating` is recorded when
the store names a verdict this code knows, and null otherwise. A summary with
no reviews is still recorded as four nulls, so the playtests are unchanged.

**The count always stands beside the percentage.** It is what tells 100% of
one review from 96% of a million. The API serves `SteamReviews` with a
nullable `rating`, and never without `percent`, `count` and the link. The
card shows a score without a verdict as "X% · N reviews", so its weight is
visible without hovering.

**Everything that ranks by the score still requires a verdict**, until #108
replaces that with a review count the user chooses. The `steam_min` /
`steam_max` filter (#103) already did. The `steam_reviews` order (#94) now
compares `CASE WHEN steam_review_rating IS NOT NULL THEN steam_review_percent
END` rather than the bare percentage. Its two indexes are rebuilt over that
expression, so the order still seeks rather than sorts.

**Nothing to migrate.** The cache holds the store's summary, not the columns
(ADR-0019). The first run after the upgrade therefore records the kept scores
from answers it already has, without asking the store again.

## Consequences

- A small game shows its Steam reviews on its card and page. It is not
  counted by a Steam range and it sorts with the unscored, as before.
- A verdict code the store adds later is read as no verdict, and the
  percentage survives it. Before, the whole score was lost.
- The sort's expression is written twice: in `ludarium.sorting` and in the
  index declaration in `models.catalogue`. SQLite uses an expression index
  only for the same expression. The test that reads the query plan for every
  indexed order fails if the two drift apart.
