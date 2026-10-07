# ADR-0035 Ingest is a sync of a reported library

Status: accepted, 2026-10-07.

## Context

Rule 8 makes the ingest endpoint a public contract: remote providers, the
local agent and manual uploads all report through one API shape. Until #126
the only way a library came in was a provider class called from
`sync_account`. M4 adds two clients that are not one, the CSV import (#130)
and the Galaxy upload (#131), and the local agent in "Later" is a third.
`docs/schema.md` had already settled who reports what: a Galaxy upload is
provider `galaxy` at the `local_agent` rung, writing into derived accounts on
the platforms it found.

## Decision

**`POST /api/ingest` takes one report: who is reporting, which account it
describes, and what that account owns.** The items have the fields of
`LibraryItem` (`provider_item_id`, `title`, `ownership_type`, `item_kind`,
`playtime_minutes`, `last_played_at`, `acquired_at`, `parent_item_id`, `raw`),
because that is already the normalised shape every provider produces. Unknown
fields are refused, so a client's typo fails loudly instead of being dropped.
Dates must carry a time zone.

**A report is applied by `sync_account`.** `ingest.ReportedLibrary` answers
as a platform would, so a reported library gets the same provenance, stubs,
add-on folding, aggregates and run history as a synced one. The run's
provider is the reporter and its trigger is `ingest`. Entitlements it creates
have origin `import`. There is no second write path to keep in step.

**Only a report that says it is whole may remove.** `complete` defaults to
false, and only `complete: true` lets the run sweep (`removed_at`). A report
of what changed that swept would remove everything it left out (rule 1), and
an inference such as "it has many items, so it must be everything" is the
kind of guess rule 1 exists to prevent. `FetchedLibrary.complete` carries the
flag into `sync_account`, where a platform's answer is always whole.

**Who may report, and about what.** The reporter must be a provider of kind
`agent` or `manual`: a platform with a client of ours reports through its own
sync, and a metadata provider describes works, not accounts. The account must
be on a provider of kind `platform`. That keeps reports away from the manual
entries' account, which ADR-0031 gives to the user alone (rule 2).

**An account a report names is created derived, and a connected one is
refused.** A new `(provider, external_account_id)` becomes an account with
`is_derived = true` and no credentials, which the sync endpoint never syncs.
The label is taken from the first report only, so a name the user gives the
account later is not undone by the next upload. A report about an account the
user connected is answered 409. That account's own sync reports it, and a
second writer at another rung of rule 5 would hold a second opinion about every
game in it.

**The version is in the payload.** `version: 1` today. It moves only for a
change an existing client would break on: a field removed, renamed or made
required, or a meaning changed. A new optional field does not move it. An
unknown version is refused with the versions the server reads, so an old
server tells a new client why rather than half-reading it.

**Bounded.** At most 50 000 items and 32 MB per request. The size is checked
by an ASGI wrapper before FastAPI buffers the body to validate it: from
`Content-Length` when there is one, and by counting otherwise.

**The session cookie, as everywhere else.** A token a daemon can hold belongs
to the local agent's issue, not to this contract.

## Consequences

- Measured on this machine, a 5000-item report is 1.2 MB. Its first ingest
  takes 21 s and a repeat takes 7.6 s, all of it in `sync_account`, in line
  with the 9 s a 2000-game Steam sync takes (#23). The request holds the write
  lock for that long, as a large sync does.
- `installed` is part of version 1 and refused until the local agent exists,
  since rule 5 gives it to the agent only. Allowing it then is not a breaking
  change.
- A report needs a reporter row. Only `manual` is seeded today. The Galaxy
  upload seeds `galaxy` and the platforms it reports for.
- A derived account appears in `GET /api/accounts`. The accounts screen (#129)
  is where it should be told apart from a connected one.
- Nothing runs enrichment after an ingest yet. `steps.FOLLOWS` is keyed by the
  syncing provider, and none of the reporters is in it.
