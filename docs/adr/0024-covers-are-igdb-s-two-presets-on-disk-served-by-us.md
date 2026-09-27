# ADR-0024 Covers are IGDB's two presets on disk, served by us under ids never reused

Status: accepted, 2026-09-27

## Context

#51 asks for cover art fetched through the enrichment pipeline, stored on disk
under the data directory, served by the backend rather than hotlinked, at
"sensible sizes" for a NAS. ADR-0019 already put binary files outside the
database. Left open: which sizes, where the data directory is when the
database is not SQLite, how a cover reaches the browser, and what happens to
the files when a cover changes or a work goes away.

## Decision

**Two of IGDB's own presets, as IGDB sends them.** `cover_big` (264×374) and
`cover_big_2x` (528×748), measured at 21–24 KB and 68–80 KB on three covers:
about 96 KB a game, 19 MB for a 197-game library. The grid gets both as a
`srcset`, so a high-density phone screen takes the sharp one and a desktop the
small one. Nothing decodes or resizes an image here, so no imaging library joins
the container. A cover missing at one size is skipped at both, rather than
leaving a `srcset` with half a pair.

**One `image_asset` row per file**, told apart by `width`, each with its own
path and SHA-256. The files live at `covers/igdb/<image_id>.jpg` and
`<image_id>_2x.jpg` under `LUDARIUM_DATA_DIR` (default `./data`, beside the
default database), a setting of its own so PostgreSQL deployments have one too.
Files are written to a `.partial` name and renamed, so a reader never sees half
of one.

**Fetched in the `igdb` run, after anchoring.** Covers read what anchoring has
just written, and are IGDB's data; a second provider row would report one
outage as two. `enrichment.chained` runs the two parts as one run. Which image
IGDB gives each game is cached in `fetch_cache` for a month like any answer;
whether the file is on disk is checked every run, so a deleted file is fetched
again.

**Served at `/api/images/{id}`, cached for good.** A changed cover is new rows,
never rewritten ones, so a URL always names the same bytes and the response
says `immutable`. That holds only if an id is never reused, and SQLite reuses a
freed highest rowid — which replacing a work's cover frees. So `image_asset` is
`AUTOINCREMENT` on SQLite; the test that replaces a cover found the reuse. The
endpoint is behind the session and refuses a path that resolves outside the
data directory.

**Files follow rows.** After each run, files in `covers/igdb/` that no row
points at are deleted — a replaced cover's, an orphan stub's, a cut-short
download. Only that directory, only files. `merge_work` moves image rows with
the rest of a work and drops a file the target already has by checksum; the
undo puts both back; the orphan job deletes the rows and leaves the files to
the sweep.

## Consequences

- A library of 5 000 games holds about 480 MB of covers.
- The data directory is now two kinds of thing, a database and files, and a
  backup has to take both. Exports (M5) leave IGDB's files out.
- Nothing shows the covers yet. The grid (M2c) reads `cover.url`, `url_2x`,
  `width` and `height` from `GET /api/works`.
- A cover is fetched only for a matched work. Unmatched stubs stay coverless
  until a later layer matches them.
