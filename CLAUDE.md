# Ludarium

Self-hosted, web-based game library aggregator (Steam, GOG, Epic, ...).
A **catalogue**, not a launcher — we do not run games.

## Stack

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, SQLite
- **Frontend:** React + TypeScript, Vite, Tailwind v4, shadcn/ui, TanStack Query + Virtual
- **Matching:** RapidFuzz, sqlite-vec, fastembed (never torch — image size matters)

## Language policy

- Code, comments, docstrings, README, docs, commit messages: **English**
- UI strings: **English by default**, all via i18next keys — never hardcoded

## Code conventions

- PEP 8, enforced by ruff; line length 100
- mypy `--strict` — full type annotations, including return types
- Comments: short and substantive. Explain *why*, not *what*. No decorative headers.
- Docstrings only where behaviour is non-obvious. No boilerplate.
- Conventional Commits, short and specific: `feat: add Steam owned-games client`
- No signatures, emoji, or generated-by footers in commit messages
- Tests use respx — never hit real platform APIs in CI

## Architecture rules

- Sync **never** deletes records — it only sets `removed_at`
- Records with `source = manual` are immutable to sync
- Providers are isolated: an Epic outage must not affect Steam
- Matching: a false positive is worse than a false negative — prefer the manual queue
- Every entity may have multiple sources with per-field precedence
- Secrets only via environment variables, never in the repository

## Out of scope (deliberately)

- Launching games
- Multi-tenant / multi-user (schema is prepared, UI is not)
- Subscription services (Game Pass) — `OwnershipType` enum exists for later
