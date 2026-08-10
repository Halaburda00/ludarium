# ADR-0012 `work.title` is IGDB-anchored; precedence applies only to competing assertions

Status: accepted, 2026-08-10

## Context

Architecture rule 5 orders sources `manual > platform API > local agent >
metadata provider`. The data model calls a `Work` "canonical, IGDB-anchored".
For `work.title` the two statements pull in opposite directions: read literally,
rule 5 makes a store name outrank the canonical one.

Applied to a real library, the literal reading produces a title that depends on
sync order. The Witcher 3 owned on both platforms reads "The Witcher 3: Wild
Hunt" after a Steam run and "The Witcher 3: Wild Hunt - Game of the Year
Edition" after a GOG one, and flips back on the next Steam run. A Work layer
whose whole purpose is to be the one stable identity for a game cannot hold a
name that changes on a schedule.

## Decision

Precedence decides between sources asserting **the same concept**. A store
listing name and a canonical work title are two different fields, not two
candidates for one field. Platforms are not a source for `work.title` at all;
their names live on `entitlement.provider_title`, where nothing overwrites them
and the matcher and the edition line both read them.

`work.title` therefore resolves `single_source` from the IGDB anchor once
`is_matched`. On an unmatched stub it is a copy of the primary entitlement's
`provider_title`, taken at creation — a derived value, not a provenance row.

This is a reading of rule 5, not an exception to it. The same reasoning applies
wherever two sources appear to disagree but are describing different things.

Alternatives considered:

- **The literal reading**, with GOG's store name winning. Rejected for the
  instability above.
- **Two columns, `canonical_title` and `display_title`.** Keeps both values
  first-class. Rejected: the grid still has to pick one, so the choice is only
  moved, and two titles must now be kept in step.

## Consequences

- A card's title is stable. Adding or removing a platform does not rename
  anything.
- A game IGDB does not cover keeps whatever the store called it, edition
  marketing included, until someone edits it. For obscure and itch.io titles
  this is the normal state, not an edge case.
- A user who prefers store names must override per work. `field_pin` and manual
  provenance rows make that possible, but there is no global "prefer store
  names" preference, and adding one later means resolving a preference before
  the registry rather than inside it.
- The edition line ("GOG: GOTY · Steam: Standard") carries the information the
  store name used to smuggle into the title, so nothing is lost from the UI.
