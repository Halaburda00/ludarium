# ADR-0028 Search matches a substring of titles folded as the sort key is, not FTS5

Status: accepted, 2026-09-28

## Context

#53 asks for search over the library. It has to run on the server, match both
the work's title and the store's own name for a copy (rule 5 keeps them as
separate fields), and page on the keyset cursor. The issue also warns that the
normalisation search uses has to agree with the ordering from #40. Otherwise a
result set is sorted by one rule and matched by another.

`docs/schema.md` and ADR-0004 had planned an FTS5 table `work_fts(title,
normalised_title, summary)` on SQLite, `pg_trgm` on PostgreSQL, and a separate
query over `entitlement.provider_title` unioned in. Checked against the issue,
that plan has three problems:

- FTS5's `unicode61` tokenizer folds on its own terms. It does not drop `™`,
  and its casefolding and diacritic removal are not `titles.sort_key`'s. That is
  precisely the second rule the issue warns about.
- `normalised_title` is `ludamatch`'s output, which nothing writes. Search over
  it would be search over nulls, and it is the matcher's normalisation, which
  the issue keeps apart from search.
- FTS5 matches word prefixes. "itcher" does not find The Witcher, which a user
  typing "a few letters" will expect.

## Decision

**A search matches a substring of titles folded by `titles.search_key`, which
is `titles.sort_key` on purpose.** `work.title_key` and
`entitlement.provider_title_key` hold the folds. Model validators keep them in
step as `sort_key` is kept in step, the migration backfills them with a copy of
the fold that a test holds equal to the function, and startup rewrites any
stored key the running code would fold differently (`reconcile_folded_keys`,
formerly `reconcile_sort_keys`).

**`GET /api/works?q=` filters and changes nothing else.** A work matches when
its folded title contains the folded query, or when the store name of one of
its live copies does. A removed copy's name does not match, since that copy
does not put the work in the list either. The listing keeps its `(sort_key,
id)` order and cursor, so a search pages exactly as the library does. `%` and
`_` are escaped and treated as characters to find. A blank query is no search.
Queries are capped at 200 characters.

**No index.** A substring match with a leading wildcard cannot use a B-tree
anyway. Measured on SQLite over 20 000 works and 26 000 entitlements, a search
that matches nothing scans everything in 14 ms, and one that fills a page
stops early at under 5 ms. On PostgreSQL the same `LIKE` works unchanged. A
`pg_trgm` GIN index on the two key columns is what would speed it up, if a
library ever makes that necessary.

**The client asks once typing pauses (250 ms) and keeps the query in the
address**, so a search survives a reload and the back button. While the next
answer is on its way, the last one stays on screen.

Alternatives considered:

- **FTS5 and `pg_trgm`, as planned.** They bring a second fold, word-prefix
  matching only, a virtual table kept in step by triggers, and two dialects to
  test. They buy speed that 20 000 works do not need.
- **Matching on `sort_key`.** It needs no new column, but `sort_key` folds
  `sort_title`, where the article has moved to the end, so "the witcher" would
  not find "Witcher 3: Wild Hunt, The".
- **Filtering in the browser.** The browser holds one page, not the library.

## Consequences

- Two new `NOT NULL` columns, derived and never assigned. A snapshot restored
  by an undone merge leaves them out, as it already left out `sort_key`.
- ADR-0004's "FTS5 and `pg_trgm` for search" no longer describes the design;
  its status line points here.
- Search finds substrings only. A misspelling finds nothing. Typo tolerance
  would be a different feature, and the fold would still have to be this one.
