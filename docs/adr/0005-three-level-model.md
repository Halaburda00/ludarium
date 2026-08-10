# ADR-0005 Three-level model: Work / Edition / Entitlement

Status: accepted, 2026-08-10

## Context

The obvious model is two levels: a game, and the fact that you own it. It breaks
on two cases that occur constantly in a real library.

The same game exists in editions that differ in bundled content — Standard,
Game of the Year, Complete — and those editions have their own store ids, their
own cover art and their own contents. And a single purchase frequently grants
several distinct games: a bundle, a season pass, a GOTY edition whose expansions
IGDB records as separate titles.

Two levels can express neither without lying about one of them.

## Decision

Three levels:

```
Work         The Witcher 3: Wild Hunt          canonical, IGDB-anchored
 └─ Edition  GOTY / Complete / Standard        differs in bundled content
     └─ Entitlement  steam:292030, gog:1495134320   what the user actually owns
```

`Entitlement` ↔ `Work` is many-to-many, because a bundle grants several works
and one work is reached by several entitlements. `Entitlement.edition_id`
records which edition was bought; the `primary` link records which work it
belongs to (ADR-0013).

Alternative considered: **two levels with an `edition_name` string on the
entitlement.** Cheap, and it renders "GOG: GOTY" perfectly well. Rejected: the
string cannot carry the edition's own store id, cover or contents, and it does
nothing at all for the bundle case, which needs the many-to-many regardless.

## Consequences

- Bundles, cross-platform deduplication and per-edition store ids are all
  expressible without special cases.
- Answering "what do I own" costs three joins, and every query in the system
  pays them.
- Edition is a level users do not think in. The UI has to hide it nearly
  everywhere, surfacing it only on the ownership line ("GOG: GOTY · Steam:
  Standard"), or the model leaks into the product.
- Stub creation (ADR-0015) has to create a work *and* an edition per new
  entitlement, so the row count per synced game is three, not one.
- Merging two works means merging their editions too, which is the most
  intricate part of `merge_work`.
