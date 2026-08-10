# ADR-0010 Sync never deletes; `manual` records are immutable

Status: accepted, 2026-08-10

## Context

Platforms lie by omission. A game disappears from an API response because of a
transient error, a partial outage, a region change, an expired family share, or
a delisting — and the response looks exactly like the one you get for a game you
genuinely no longer own.

If absence meant deletion, one bad response would take the user's playtime,
status, rating and notes with it. That data is not recoverable from the platform
because the platform never had it.

Separately, a user may add a game they own on a disc or as an unredeemed key.
Nothing on any platform corresponds to it, so any sync that reconciles against a
provider's list would treat it as gone.

## Decision

Absence sets `entitlement.removed_at` and records the run that did it. Rows are
never deleted. Removed entries stay visible in a dedicated view and are restored
in one click, keeping their `first_seen_at`, playtime and user state.

Only a run that finishes `success` may mark anything removed — a `failed` or
`partial` run marks nothing, or an Epic outage would empty the Epic library.

Records with `origin = 'manual'` are excluded from sync by predicate, not by
convention. They carry `provider_item_id IS NULL`, so they cannot collide with
the `UNIQUE (account_id, provider_item_id)` upsert key sync works through.

Alternatives considered:

- **Hard delete on absence.** The table matches the platform exactly and never
  grows. Rejected: it converts a transient API failure into permanent data loss,
  and the data lost is the only data the user actually created.
- **Soft delete with an automatic purge after N days.** Bounded growth, still
  recoverable for a while. Rejected: it destroys the same data, just later, and
  requires the user to notice within a window they were never told about.

## Consequences

- A provider outage becomes cosmetic. Nothing is lost, and the removed view
  explains what happened.
- The table only grows. On a personal library that is measured in kilobytes, and
  it is the right trade.
- Every library query needs `removed_at IS NULL`. Forgetting it is the single
  most likely bug in any new query, which is why the default grid predicate is
  documented in the schema rather than left to each caller.
- Restore has to be genuinely one click. A removed view that is tedious to act
  on makes the promise of this ADR theatre.
- Work-level aggregates must be computed from non-removed entitlements only, so
  a removal changes `playtime_minutes` and `platform_count` without touching the
  entitlement's own stored values.
