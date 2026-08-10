# ADR-0006 Wikidata as the redistributable alias backbone

Status: accepted, 2026-08-10

## Context

Layer 2 of the matching cascade is a curated dataset of cross-platform title
aliases, expected to cover roughly 20% of a library — the part hard IDs miss and
fuzzy matching gets wrong.

That dataset is also the project's stated secondary goal: an openly licensed set
of cross-platform title mappings, which does not currently exist in public. A
dataset that cannot be published fails that goal completely, whatever its
quality.

The obvious sources are ruled out by their terms. IGDB and RAWG are runtime-only
with no redistribution; store pages are hostile to scraping and unstable.

## Decision

`ludamatch-data` is generated offline from Wikidata, which is CC0 and therefore
redistributable. It lives in its own repository, is versioned, and ships inside
the Docker image as the `title_alias` table — the only bundled catalogue data.
Each row records the `dataset_version` it came from, and imports rebuild the
table wholesale rather than editing it.

Alternatives considered:

- **Build the alias set from IGDB.** Far better coverage of games as such, and
  the anchor is already IGDB. Rejected outright: IGDB forbids redistribution, so
  the result could never be published, which is the entire point of the dataset.
- **Scrape store pages.** Best possible coverage of store-specific names.
  Rejected: fragile, adversarial, and legally worse than either of the above.

## Consequences

- The dataset can be published, reused by other projects, and improved by people
  who will never run Ludarium.
- Wikidata's coverage of games is uneven — thin for small releases, itch.io
  titles and non-English names. Layer 2 will miss its ~20% target on some
  libraries, and the honest response is the measured per-layer numbers in M6,
  not a better claim.
- Generation is an offline pipeline that has to be maintained and re-run, and
  `dataset_version` discipline is what makes a stale import diagnosable.
- Because the data is CC0, it can ship in the image, which keeps layer 2 working
  on an instance with no internet access at all.
