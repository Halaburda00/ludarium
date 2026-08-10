# ADR-0008 AGPL for the app, MIT for the library, CC0 for the data

Status: accepted, 2026-08-10

## Context

Three artefacts with three different jobs. Ludarium is a self-hosted web
application someone could trivially run as a service for other people.
`ludamatch` is a matching library whose value depends on other projects
depending on it. `ludamatch-data` is a dataset whose entire purpose is to be
reused.

A single licence cannot serve all three, because the protection the application
wants is exactly the friction the library and dataset must not have.

## Decision

| Artefact | Licence | Reason |
|---|---|---|
| Ludarium (the app) | AGPL-3.0-or-later | Network use triggers the obligation, so a hosted fork must publish its changes |
| `ludamatch` | MIT | A matcher nobody can depend on is a matcher nobody uses |
| `ludamatch-data` | CC0 | The dataset exists to be taken |

A `NOTICE` file keeps the name and logo outside the code licence.

Alternatives considered:

- **MIT everywhere.** Maximum adoption, no friction anywhere. Rejected for the
  application: a closed hosted fork of a tool whose whole premise is
  self-hosting would take the work and give nothing back, and we have already
  refused to run that service ourselves.
- **AGPL everywhere.** Consistent and maximally protective. Rejected for the
  library: AGPL on a dependency is a non-starter for most consumers, so
  `ludamatch` would be published and never adopted, defeating the reason to
  extract it.

## Consequences

- The library and the dataset can be adopted by anyone, including commercially,
  without touching the application's copyleft.
- Three licences is a real burden: inbound = outbound has to be stated per
  repository in `CONTRIBUTING.md`, headers have to be right, and code cannot be
  moved from the app into the library without checking who wrote it.
- AGPL excludes some corporate users of the application itself. That is
  accepted — they are not the audience.
- The split fixes when `ludamatch` has to exist, and the constraint is
  **contributor copyright, not repository visibility**. While every line is
  written by us, matcher code sitting in the AGPL application can be released
  under MIT at any time — publishing the repository changes nothing about that.
  What changes it is the first external contribution touching matcher code:
  from then on, relicensing needs that contributor's agreement, and the cost
  grows with every one after. External PRs realistically begin after v0.1.0
  (M5), so the repository is created in M2, well before the boundary can be
  crossed by accident.
- Extraction cost points the same way. Three functions move in an hour; a grown
  matcher takes a week, and comes out with the API shape it acquired as part of
  an application rather than the one a library would have been given.
