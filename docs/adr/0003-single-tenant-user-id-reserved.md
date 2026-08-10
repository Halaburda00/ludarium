# ADR-0003 Single-tenant with a `user_id` column reserved

Status: accepted, 2026-08-10

## Context

Ludarium is a personal library tool. A multi-user interface — invitations,
per-user visibility, shared or separate libraries — is explicitly out of scope,
and building it would cost more than everything in M1 combined.

The risk is the opposite one. Retrofitting user scoping into a schema that never
had it means touching every user-owned table, every index, every query and every
endpoint at once, on data that already exists.

## Decision

One login (single account, argon2), no user management UI. Every user-scoped
table carries `user_id INTEGER NOT NULL DEFAULT 1` with a foreign key to
`app_user`, and every user-scoped index leads with it.

The column is real and populated; only the UI is single-tenant.

Alternatives considered:

- **No `user_id` at all.** Honest about the current scope and slightly smaller.
  Rejected: the migration to add it later is the expensive kind — schema, every
  composite index, and a backfill — and it would land exactly when the project
  has real users with real data.
- **Full multi-tenancy from day one.** Rejected: authentication is the easy
  part; sharing rules, per-user matching decisions and per-user provenance are
  not, and nobody has asked for any of it.

## Consequences

- Going multi-user later is authentication and UI work plus a relaxed default,
  not a data migration.
- Several columns and index prefixes carry a constant value. The storage cost is
  trivial; the confusion cost for a newcomer reading the schema is not, which is
  what this ADR is for.
- The discipline holds only if every new user-scoped table remembers the column.
  It belongs on the review checklist, because the first table that forgets it is
  the one that breaks the promise.
- Index prefixes like `(user_id, play_status)` are pure overhead today. They are
  kept anyway, since changing index shape later is as disruptive as adding the
  column would have been.
