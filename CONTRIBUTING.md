# Contributing

Thanks for looking. The project is pre-alpha and the design is deliberately
opinionated, so the most useful thing you can do before writing code is read
[`docs/adr/`](docs/adr/) — every significant decision is there with the
alternatives that were rejected and why. If you disagree with one, open an issue
about the ADR rather than a pull request against its consequences.

## Licensing: inbound = outbound

Anything you contribute to a repository is licensed under that repository's
licence. No CLA, no copyright assignment.

| Repository | Licence |
|---|---|
| `ludarium` (this one) | AGPL-3.0-or-later |
| `ludamatch` | MIT |
| `ludamatch-data` | CC0 |

### Matcher logic goes in `ludamatch`, not here

**This is the one boundary that cannot be fixed later.** Title normalisation,
candidate retrieval, similarity scoring, the adjudication classifier, the
mapping types — all of it belongs in the `ludamatch` repository under MIT.
Ludarium depends on that library; it holds no matcher logic of its own.

The reason is mechanical rather than aesthetic. Code contributed here is AGPL,
and moving it to MIT afterwards requires the agreement of everyone who wrote any
part of it. One accepted pull request in the wrong repository makes the library
permanently harder to relicense, and there is no way to undo it without deleting
the contribution. Everything else in this project can be refactored; this
cannot.

What belongs in Ludarium: the sync services, provider clients, the schema and
migrations, the API, the frontend, and the code that *calls* the matcher and
stores its results.

If you are unsure which side of the line something falls on, ask in the issue
before writing it.

## Setup

```bash
# backend
cd backend && uv sync
cp .env.example .env

# frontend
cd frontend && pnpm install

# hooks (ruff, mypy, gitleaks, commit-message format)
pre-commit install && pre-commit install --hook-type commit-msg
```

## Running tests

```bash
cd backend && uv run pytest              # all backend tests, with coverage
cd backend && uv run pytest -k steam     # one area
cd backend && uv run mypy src            # strict type checking
cd backend && uv run ruff check .        # lint
cd backend && uv run ruff format .       # format

cd frontend && pnpm run lint             # not yet enforced in CI, see below
cd frontend && pnpm run build            # tsc -b && vite build

pre-commit run --all-files               # everything the hooks check
```

CI runs the backend commands plus the frontend build. `pnpm run lint` is not
enforced yet — eslint's `react-refresh/only-export-components` rule fails on the
generated shadcn components, and that needs an ignore rule before the check can
be made blocking. Run it locally anyway; new violations outside
`src/components/ui/` are real.

The backend runs on Python 3.13 and nothing else. The backend ships as
a Docker image based on `python:3.13-slim`, so the interpreter version is part
of the artefact rather than a support matrix.

**Tests never call real platform APIs.** Use `respx` with recorded fixtures. A
test that reaches the network is a test that fails in CI for reasons unrelated
to your change, and one that leaks an API key into a fixture is worse.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/), short and
  specific: `feat: add Steam owned-games client`, `fix: keep manual entries out
  of the removal pass`. The format is enforced by a commit-msg hook.
- **No signatures, emoji, or generated-by footers in commit messages.** This
  includes anything added automatically by an editor or an AI tool.
- One logical change per commit. A pull request per feature.
- CI green before merge. A pull request that fails lint or types will not be
  reviewed until it does not.
- Code, comments, docstrings, commit messages and documentation are in English.
  UI strings go through i18next keys and are never hardcoded.

## Code conventions

The full set is in [`CLAUDE.md`](CLAUDE.md), which is the working agreement for
both human and AI contributors. The parts people get wrong most often:

- Line length 100, enforced by ruff.
- `mypy --strict`: full annotations, including return types and generics.
- **Comments explain *why*, not *what*.** No decorative separators, no banner
  headers, no restating the code in prose. Docstrings only where the behaviour
  is non-obvious.
- New user-scoped tables carry `user_id NOT NULL DEFAULT 1`
  ([ADR-0003](docs/adr/0003-single-tenant-user-id-reserved.md)), even though the
  UI is single-user.
- Providers never write entity columns; they write `field_provenance` rows
  ([ADR-0011](docs/adr/0011-providers-write-provenance-rows.md)).
- Never add `torch` or `sentence-transformers`
  ([ADR-0009](docs/adr/0009-fastembed-instead-of-sentence-transformers.md)).

## Reporting a security issue

Do not open a public issue for anything involving credentials, token handling or
the encryption of stored platform keys. Email the address in `backend/pyproject.toml`
instead.
