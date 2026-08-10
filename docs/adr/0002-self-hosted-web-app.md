# ADR-0002 Self-hosted web app instead of a desktop application

Status: accepted, 2026-08-10

## Context

Every existing library aggregator is a Windows desktop application that depends
on locally installed launchers. That architecture makes three things impossible
at once: checking your library from a phone, using it from a Mac, and having it
stay current while the desktop is switched off.

The gap Ludarium fills is not "another aggregator" — it is an aggregator that
lives somewhere always on and is reachable from anywhere.

## Decision

A single-process web application: FastAPI backend, React frontend served by it,
packaged as a multi-arch Docker image (`linux/amd64` and `linux/arm64`) so it
runs on a NAS. The user self-hosts it. State lives in one SQLite file under a
mounted volume.

Alternatives considered:

- **Electron or Tauri desktop app.** Direct filesystem access — it could read
  `galaxy-2.0.db` and installed-game state without an upload or an agent, and it
  needs no authentication at all. Rejected: it reintroduces exactly the
  limitation that motivates the project, and the two heaviest features it would
  simplify (Galaxy import, install state) are one milestone and one "Later" item
  respectively.
- **A hosted service.** Out of scope for a separate reason: it would mean
  holding other people's platform credentials.

## Consequences

- One instance serves every device the user owns, and syncs on a schedule
  whether or not anyone is looking.
- Authentication, session handling and a password hash become mandatory from M1
  — a desktop app would need none of it.
- No access to the user's filesystem. `galaxy-2.0.db` has to be uploaded by
  hand, and installed state requires the local agent in "Later". Both are real
  costs paid to keep the architecture.
- The user must be willing to run a container. That is a materially higher
  barrier than downloading an `.exe`, and it caps the audience to people who
  already self-host something.
- Reverse proxies, subpaths and `PUID`/`PGID` become first-class concerns
  (M5), because a self-hosted app that only works at the root of a domain is
  half-finished.
