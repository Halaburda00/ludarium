# Ludarium

Self-hosted, web-based game library aggregator. It connects to your accounts on
multiple gaming platforms, pulls the games you own into one catalogue, tags each
entry with the platform it came from, and lets you filter, sort and track what
you have.

**It is a catalogue, not a launcher. It never runs games.**

---

## Motivation

Buying games on four storefronts means owning a library that exists nowhere.
Each launcher shows its own slice, none of them knows about the others, and the
question "do I already own this, and where" has no answer short of checking four
applications on the one machine that has all four installed.

Existing aggregators solve this on Windows, on the desktop, next to the
launchers they depend on. Ludarium solves it on a server: one instance on a NAS
or a spare box, reachable from a phone on the sofa, a Mac, or any browser,
current whether or not the gaming PC is switched on.

A second goal comes with it. Matching titles across storefronts requires a
dataset of cross-platform aliases, and no openly licensed one exists.
`ludamatch-data` is built as a public CC0 dataset so that the next project does
not have to solve this again.

## Prior art

| Project | What it is | Why it does not fill this gap |
|---|---|---|
| [Playnite](https://playnite.link/) | Windows desktop library manager and launcher, extensive plugin ecosystem | Windows only, desktop only, depends on locally installed launchers. Nothing to open from a phone |
| GOG Galaxy 2.0 | Windows/macOS desktop aggregator with third-party integrations | Desktop only, closed, and integrations have been unmaintained for years |
| [Heroic](https://heroicgameslauncher.com/) | Cross-platform launcher for Epic, GOG and Amazon | A launcher first; the library is what it launches from, and it is per-machine |
| [Lutris](https://lutris.net/) | Linux game manager and launcher | Linux-focused, launch-centric, tied to the machine that runs the games |
| [Backloggd](https://backloggd.com/) | Web service for tracking and rating games | Hosted, and the library is entered by hand — it does not connect to your accounts |

The unoccupied combination is **self-hosted, cross-platform, account-based, and
read from a browser**. That is the whole of Ludarium's claim.

## Status

**Pre-alpha. Nothing runs yet.**

| Milestone | State |
|---|---|
| M0 — documentation and decisions | complete |
| M1 — Steam → database → an ugly list | next |
| M2 … M6 | planned |

There is no release, no Docker image and no usable application at this point.
The design is settled and written down; the code is not. `v0.1.0` arrives at M5,
and that is the first version worth anyone's time.

If you want to follow along, [`docs/roadmap.md`](docs/roadmap.md) is honest
about what exists and what does not.

## Roadmap

Vertical slices — every milestone ends with something that runs end to end.

| | Milestone | Contents |
|---|---|---|
| M0 | Documentation and decisions | Schema, ADRs, README, CI |
| M1 | Steam → database → an ugly list | Sync, login, a plain table of your real library |
| M2 | Metadata and a real grid | IGDB and RAWG, matching layer 1, covers, virtualised grid |
| M3 | Filters, statuses, backlog | Filter registry, `PlayStatus`, manual entries, saved views |
| M4 | GOG, Epic, local import | More providers, `galaxy-2.0.db` import, ingest contract, scheduled sync |
| M5 | Docker, docs, first public release | Multi-arch image, export/backup, `v0.1.0` |
| M6 | Matching layers 3–5 | Retrieval, classifier, LLM adjudication, the alias dataset |

Details, estimates and per-milestone exit criteria: [`docs/roadmap.md`](docs/roadmap.md).

Design documents worth reading before contributing:

- [`docs/schema.md`](docs/schema.md) — the complete data model
- [`docs/adr/`](docs/adr/) — one file per decision, with the alternatives that
  were rejected and why

## Licence

Three artefacts, three licences, chosen by what each one is for.

| Artefact | Licence | Repository |
|---|---|---|
| Ludarium (this application) | [AGPL-3.0-or-later](LICENSE) | this one |
| `ludamatch` — the matching library | MIT | separate repository, created in M2 |
| `ludamatch-data` — the alias dataset | CC0 | separate repository |

The application is AGPL because network use is exactly how someone would take it
without giving anything back. The library is MIT because a matcher nobody can
depend on is a matcher nobody uses, and it lives in its own repository from the
moment there is matcher code to put in it — not as a future extraction. The
dataset is CC0 because it exists to be taken.

Contributions are inbound = outbound: what you send to a repository is licensed
under that repository's licence. See [`CONTRIBUTING.md`](CONTRIBUTING.md),
particularly on where matcher code belongs.

The reasoning is recorded in
[ADR-0008](docs/adr/0008-licensing-agpl-mit-cc0.md).

The Ludarium name and logo are not covered by the code licence — see
[`NOTICE`](NOTICE).
