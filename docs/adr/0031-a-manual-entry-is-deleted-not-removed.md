# ADR-0031 A manual entry is deleted, not removed

Status: accepted, 2026-10-02. Refines ADR-0010 and ADR-0015.

## Context

The schema has carried manual entries since M1: `origin = manual`, a `manual`
provider, `provider_item_id IS NULL` by `CHECK`, and `OwnershipType.physical`.
#97 adds the endpoints and the screen that create them: a disc, an unredeemed
key, an itch.io download, which no platform will ever report.

Three things had to be decided that the earlier ADRs left open: what deleting
one means, where each thing the user types is stored, and which account a
manual copy belongs to.

## Decision

**Deleting a manual entry is a `DELETE`.** Rule 1 protects the user from a
platform's silence, which can be a fault and looks the same as a real loss. A
manual entry has no platform. The user typed it in and the user takes it out,
and nothing about that is ambiguous. A soft removal would fill the removed view
with typos, and restoring a typo helps nobody.

The stub behind it goes too when nothing else reaches it and nothing has
matched it. That takes the user's state on that work with it, which is what
deleting a game you typed in means. A matched work, or one another copy still
reaches, stays: a merge may have put the user's Steam copy on it, and a matched
work is the orphan collector's to judge, as it is for every other work.

**What the user types goes where the same fact goes for a platform:**

- The title is the entry's `provider_title`, recorded as `manual` provenance.
  The stub copies it as ADR-0015 says. It is not a `manual` row on
  `work.title`: that would outrank IGDB's canonical title after a match, and on
  a merged work it would rename the user's Steam copy to whatever they typed for
  the disc. A platform's name for a game is not the work's title (rule 5), and
  neither is the user's name for their disc.
- The release year and the kind are `manual` provenance on the work, so they
  outrank every other source (rules 3 and 5). Clearing the year withdraws the
  user's row rather than recording a null, so a matched work gets IGDB's year
  back. The kind is asked for because the kind filter treats a work with no kind
  as matching no kind, and a manual entry would otherwise vanish from a `game`
  filter.
- The ownership type and the store label are columns on the entitlement,
  written straight. Nothing competes for them on a manual row, which has one
  writer. `store_label` is new; a synced row leaves it null because its platform
  already says where it lives.

**One `manual` account per user**, created with the first entry. A label is
not an account: "PS5 disc" and "itch.io" are not connections with credentials
or a sync history.

Alternatives considered:

- **Soft removal into the removed view.** It keeps the restore button
  consistent across every copy. Rejected for the typo reason above, and because
  the view's rule, "a run stopped seeing it", would not be true of these rows.
- **One manual account per store label.** It reuses `account.label` and needs
  no migration. Rejected: renaming a label would move an entitlement between
  accounts, and the accounts list would fill with things nobody connected.

## Consequences

- A deleted manual entry cannot be brought back. The UI asks before it deletes.
- `field_provenance` rows about the entry go with it. They are polymorphic, so
  no foreign key cascades them. On a work that stays, the year and kind the
  entry gave are withdrawn too, unless another manual copy still reaches the
  work: those rows are keyed by work and field, not by entry, so they are that
  copy's as well.
- An entry the matcher later links to an IGDB work keeps the user's year and
  kind, and shows IGDB's title. That is rule 3 working, not a conflict.
- The `manual` account appears in `GET /api/accounts` like any other. It has no
  credential, and the sync endpoint already refuses `manual`.
