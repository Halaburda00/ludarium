# ADR-0018 Works are ordered by a key folded in Python and stored beside the title

Status: accepted, 2026-09-11

## Context

`GET /api/works` ordered by `work.sort_title`, and SQLite compares text with the
`BINARY` collation — byte by byte. Run against a real 197-game Steam library
(#40), the first page opened like this:

```
0 ARC Raiders
1 Amnesia: A Machine for Pigs
```

`R` (0x52) is a smaller byte than `m` (0x6D). The same rule filed one series as
two blocks, because `:` (0x3A) is smaller than `™` (U+2122):

```
Batman: Arkham Asylum GOTY Edition
Batman: Arkham City GOTY
Batman™: Arkham Knight
Batman™: Arkham Origins
```

The order is not cosmetic. The listing pages on a keyset over `(sort_title, id)`,
so the comparison that orders the page is also the one that finds where the
next page starts. Whatever changes one has to change both, and the index they
seek on, or a page boundary skips or repeats rows.

`sort_title` itself cannot be folded. It is a resolved field under `precedence`,
and `docs/schema.md` shows its reason for being one: a user sets
`"Witcher 3, The: Wild Hunt"` by hand, and rule 3 keeps that exactly as written.
Folding the column in place would overwrite the user's spelling with the
database's comparison form.

## Decision

A second column, `work.sort_key`, holds `sort_title` folded by
`ludarium.titles.sort_key`: trademark signs removed, NFKD-decomposed, casefolded,
combining marks dropped, whitespace runs collapsed. The listing orders by
`(sort_key, id)`, seeks on `ix_work_sort_key_id`, and the cursor carries the key.
`ix_work_sort_title_id` is dropped, since nothing orders by `sort_title` any
more.

The column is declared `COLLATE "C"` on PostgreSQL. Folding in Python settles
what the key contains, not how the database compares two keys: PostgreSQL's
default collation is the database's locale, and a locale collation reorders the
punctuation, digits and spaces the key deliberately keeps. `C` compares bytes,
which on UTF-8 is code point order — the order SQLite's `BINARY` and Python's
`sorted` already use. The first version of this ADR claimed the engines agreed
without the declaration; review caught it. There was no PostgreSQL to measure
against when this was written, so this rests on PostgreSQL's documented `C`
behaviour, and a test pins the declaration in the compiled DDL.

The fold also depends on the interpreter. `unicodedata` carries the Unicode
database of the Python running it, and a newer one folds characters an older
one did not know. Measured over every code point between Python 3.13.14 (UCD
15.1.0) and 3.14.4 (UCD 16.0.0):

```
code points whose sort_key differs: 95
  of which were already assigned in the old UCD: 0
  of which were unassigned (Cn) in the old UCD:   95
```

That is Unicode's normalization and case-folding stability policies, observed
on this function: a key computed from characters that existed at the time does
not change. A title is exposed only if it holds a character newer than the
interpreter that keyed it — rare, and silent when it happens, since the old row
and a new one would file apart with no error.

So startup rewrites every stored key the running code would compute
differently, and logs how many (`seed.reconcile_sort_keys`). That also repairs
the two other ways a key can go stale — a bulk `UPDATE` past the validator, and
a change to the fold shipped without a recomputing migration. It reads every
row on every start: 64 ms at 20 000 works and 329 ms at 100 000, measured.

The key is derived by a SQLAlchemy validator on `Work.sort_title`, and is never
assigned directly. Every writer already assigns `sort_title` through the ORM —
the resolver with `setattr`, the sync stub through the constructor, tests the
same way — so every writer keeps the key in step without knowing it exists. It
is not a provenance field: nothing asserts it, so there is nothing for the
resolver to decide.

The cursor gains a version, `[2, key, id]`. Cursors issued before this were
`[sort_title, id]`, which is exactly the shape of `[key, id]` and a position in
a different order; accepted, one would silently start a page somewhere nobody
chose. Refused, it is a 400 once, for a library left open across the upgrade.

The migration backfills in Python with its own copy of the function rather than
an import, as earlier revisions spell out their values: a migration describes
the database at its revision. A test runs the backfill and compares it with the
live function, which is the only thing that can say the two agree.

Alternatives considered, with what SQLite 3.53.1 actually does where it was
measured:

- **`COLLATE NOCASE`.** Folds ASCII letters only: `BRÜTAL` and `brütal` stay two
  different strings, and an accented letter still sorts after every unaccented
  one in its position. PostgreSQL has no such collation at all, and ADR-0004
  keeps PostgreSQL a supported target.
- **A generated column over `lower(sort_title)`.** Portable in syntax, not in
  result: `SELECT lower('BRÜTAL')` returns `brÜtal`. The fold that matters is
  precisely the one SQL's `lower` does not do on SQLite without the ICU
  extension, which the standard library build does not carry.
- **A custom collation registered with `sqlite3.create_collation`.** Gives the
  right order on the connections that register it. An index declared with it is
  unusable from every connection that does not — the `sqlite3` shell, a backup
  tool, a migration run from anywhere but this codebase — and there is no way to
  give PostgreSQL the same function.
- **PostgreSQL `citext` or a nondeterministic ICU collation, with something else
  on SQLite.** Each engine would fold by its own rules, so the same library
  would list in a different order depending on the database behind it. A key
  computed before either engine sees it cannot disagree with itself.
- **Folding `sort_title` in place.** Rejected above: it destroys the value rule 3
  protects.
- **Storing the Unicode version beside the keys**, as CLAUDE.md asks for
  embeddings, and recomputing when it changes. Notices one of the three ways a
  key goes stale and not the other two. The convention exists for embeddings
  because recomputing them takes minutes, so knowing *when* is worth
  bookkeeping; a key costs microseconds, and checking every row is cheaper than
  deciding whether to.

## Consequences

- Both engines order identically because neither folds anything and both compare
  bytes — SQLite by default, PostgreSQL because the column says `COLLATE "C"`.
  Remove the declaration and PostgreSQL silently orders by locale instead.
- A bulk `UPDATE` against `work.sort_title` would bypass the validator and leave
  the key stale until the next start. Nothing issues one; a resolver test fails
  if the resolver ever stops assigning through the ORM.
- Changing the fold, or the Python that runs it, is repaired at the next start
  rather than needing a migration of its own. The warning in the log is the only
  notice an operator gets that it happened.
- Startup reads `work.sort_key`, so a database the upgrade was never run on is
  refused at start with the command to run, rather than failing on the first
  request.
- Punctuation is not folded. It is ordering — it is what files "Company of
  Heroes: Opposing Fronts" after "Company of Heroes 2" — and stripping it is
  matcher normalisation, which belongs to `ludamatch` (ADR-0008).
- Digits compare as text: "Game 10" files before "Game 2". Natural-number
  ordering is a separate decision with its own edge cases, and nothing asked for
  it.
- Dropping combining marks is right for accented Latin letters and loses the
  distinction between a kana and its voiced form, so the order among such titles
  is approximate. Titles are requested in English (`l=english`), which makes
  this the rare case rather than the common one.
- `sort_title` only moves a capitalised article, so "the last of us" still files
  under T. The key inherits that; it folds what `sort_title` produced and does
  not second-guess it.
