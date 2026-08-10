# ADR-0001 Catalogue, not launcher

Status: accepted, 2026-08-10

## Context

The tools Ludarium is measured against — Playnite, GOG Galaxy, Heroic, Lutris —
all aggregate a library *and* launch it. Launching is what makes them desktop
applications: it requires running on the machine where the games are installed,
speaking each platform's launch protocol, tracking install state, and often
handling updates.

Ludarium is meant to run on a NAS and be answered from a phone. A server in
another room cannot start a game on a desktop, and pretending otherwise would
shape the whole product around a capability it structurally does not have.

## Decision

Ludarium never runs, installs or updates a game. It answers "what do I own,
where, and have I played it". Where the user needs to act on that answer, the
card links to the store page, built from `provider.store_url_template` and
`entitlement.provider_item_id`.

Alternatives considered:

- **Protocol deep links** (`steam://run/292030`). Genuinely works, costs almost
  nothing, and would launch a game when the browser happens to be on the machine
  with the client installed. Rejected: it works on exactly one of the devices the
  product is designed to be used from, and a launch button that silently does
  nothing on a phone is worse than no button. It also drags install-state
  tracking into scope to know when to show it.
- **A companion agent that launches on request.** Rejected as scope: the agent
  is already planned for install state and playtime, and making it a remote
  execution channel changes its security profile entirely.

## Consequences

- The scope stays small enough to run headless in a container on ARM hardware,
  and every feature is reachable from any browser.
- People arriving from Playnite will ask for launching. The answer has to be a
  clear "no, by design", which is why this ADR exists.
- The boundary blurs exactly once: the local agent in "Later" reports
  `installed` and accurate playtime. Reporting is not launching, and the
  distinction has to be defended when that agent is built.
- Store links become load-bearing UI rather than a nicety, which is why
  `store_url_template` sits on `provider` from day one.
