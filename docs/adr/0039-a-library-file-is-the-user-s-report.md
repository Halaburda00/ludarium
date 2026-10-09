# ADR-0039 A library file is the user's report, one account per platform it names

Status: accepted, 2026-10-09.

## Context

Some libraries have no API worth integrating: a spreadsheet of discs and keys,
another tool's export, EA, Ubisoft and Battle.net now that Galaxy no longer
reaches them (ADR-0038). #130 brings them in from a CSV or JSON file, through
the ingest contract (ADR-0035), so a file is applied like every other report.

## Decision

**Read in the backend, shown before it is written.** `POST /api/import/preview`
parses the file and answers with what it read: rows, the columns it used and
those it ignored, the rows it could not read and why, and the account each row
lands in. `POST /api/import` takes the same file again and applies it. Nothing
is kept between the two, so nothing the user did not confirm is ever stored,
and there is no half-made import to expire.

**The user's rung.** The reporter is `manual`, at the `manual` rung of rule 5,
as `docs/schema.md` settled: the user is asserting what they own, with no
machine behind it. Copies have origin `import`, not `manual`, so rule 2 does
not freeze them: a re-import updates them. They are not edited in the manual
entry form, which belongs to `origin = manual` (ADR-0031). A user's override on
the work still wins over any import (rule 3).

**One account per platform the file names.** A platform name a person would
write — "Origin", "EA app", "PS5", "itch.io" — is folded and looked up, and
lands under that platform's provider. Ten clientless platforms are seeded for
it (`ea`, `ubisoft`, `battlenet`, `xbox`, `playstation`, `nintendo`, `itch`,
`humble`, `amazon`, `other`), so the platform filter and badges work on them.
Anything else lands under `other`, labelled with what the file said. The
account is identified by the folded name (`import:origin`), not the provider:
"EA" and "Origin" in one file are two lists the user kept apart, and stay two
accounts. A row with no platform goes to an account labelled "Imported".

**Steam, Epic and GOG rows are skipped when that platform is connected.** Its
own sync is the better source, and a second copy of every game would be noise.
With none connected the rows land in a derived account on that platform, as a
file is then the only word on it. Connecting the platform later does not take
that account over, because its id is `import:steam`, not a SteamID: its copies
stay until the user switches it off.

**A derived account's ids are its report's.** Classification, review scores,
layer 1 matching and store links all read an entitlement's `provider_item_id`
as the platform's id. On a derived account it is whatever the report said:
for a file, the user's own `id` or a key made from the title. Sent to the
Steam store it failed the whole classification step on the first non-numeric
one; sent to IGDB as a hard id it would anchor a work to whatever game had
that number, a false positive rule 6 exists to prevent. All of them now skip
derived accounts. Imported copies are matched by title in M6, or not at all.

**The key a re-import finds.** A row's `id`, when it has one, prefixed `id:`.
Without one, a hash of the title folded with NFC and casefold, plus how many
rows before it in the same account had that title: two copies of one game stay
two, and the same file imported twice lands on the same rows. The fold is not
`titles.search_key`, which moves with the Unicode database and is healed on
start; a key here lives in rows a later import has to find again. An `id`
repeated in one account is a problem with the later row.

**Formats are named, and the rest refused whole.** CSV in UTF-8 with or
without a BOM, UTF-16 with a BOM, or Windows-1250, which is what Excel saves on
a Polish Windows; separated by commas, semicolons or tabs, counted on the
header line; quoted newlines allowed. JSON is an array of objects in UTF-8.
A file whose encoding, structure or header cannot be read is refused with a
message, never imported in part. A row with a bad value costs that row and is
listed with its number. Dates are ISO 8601 or `DD.MM.YYYY`, and a bare date
is midnight UTC.

**Removal is the user's choice, off by default.** The preview says how many
copies each account holds that the file does not list, and only a request
with `sweep` sends `complete: true`. A file with unread rows cannot sweep:
the copies behind those rows would be removed for being missing (rule 1).

**Bounded, and nothing else kept.** 16 MB and 50 000 rows per file. `raw`
holds the row number only; the file's other columns may hold anything, and
none of it is stored (rule 7).

## Consequences

- The ingest contract gained an optional `release_year`, asserted about the
  work at the reporter's rung, as `item_kind` is. Version 1 still holds.
- An imported copy links to no store, even on Steam.
- Rows that change their title and carry no `id` are a new copy on the next
  import, and the old one is swept only if the user asks.
- Nothing runs enrichment after an import, as after any ingest (ADR-0035).
