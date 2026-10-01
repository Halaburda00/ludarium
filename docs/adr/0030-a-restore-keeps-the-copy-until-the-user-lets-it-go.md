# ADR-0030 A restore keeps the copy until the user lets it go

Status: accepted, 2026-10-01. Refines ADR-0010.

## Context

ADR-0010 promises a removed view with a one-click restore. Sync already undoes
a removal by itself when the platform lists the game again (`_refresh`), so the
outage case needs no click at all. A restore by hand is therefore only ever
used for a game the platform still does not list.

If a restore only clears `removed_at`, the next successful sync marks the game
removed again. The click lasts until the next run, which can be minutes away.
For a game the user knows they own, that click does nothing useful: a key
redeemed on a second account, a family share they have since bought, a
delisted game the platform has dropped from the response.

## Decision

**A restore is the user's word against the platform's silence, and rule 3
applies to it.** Restoring clears `removed_at` and `removed_by_run_id`, and
sets `entitlement.kept_at`. The sweep skips a kept copy. A copy the platform
still lists is unaffected: `kept_at` changes nothing about what a run writes
for it.

`kept_at` stays until the user clears it from the work's page ("stop keeping").
After that, the next successful run that does not list the game marks it
removed, as it would any other copy. A platform listing the game again does not
clear the flag either: rule 3 says no sync overwrites a user's decision, and
later reappearance is no reason for the flag to stop holding.

A column rather than a provenance row: removal is not a resolved field. Nothing
competes for it, and it has no value for the resolver to choose between. The
flag is the user's decision about one row, stored on that row, as `removed_at`
is the sync's.

Alternatives considered:

- **Restore lasts until the next sync.** Honest about what the platform says,
  and needs no migration. Rejected: the only case a restore by hand is used for
  is the one it would immediately undo, so the click would be theatre, which
  ADR-0010 warns against.
- **Restore marks the entitlement `manual`.** The sweep already ignores manual
  rows (rule 2). Rejected: a manual row carries no `provider_item_id`, so it
  would lose its upsert key and its playtime from the platform. A kept copy is
  still a platform copy and should still be described by each run that sees it.

## Consequences

- A game that really did leave the account (a refund, an expired share) stays
  in the library once kept, until the user stops keeping it. That is the cost
  of the user's word winning, and the work's page states it.
- The removed view lists only rows with `removed_at` set. A kept copy is live,
  appears in the library, and is marked as kept on its work's page.
- Manual copies never appear in the removed view, because they are never
  swept (rule 2).
