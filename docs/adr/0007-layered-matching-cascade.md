# ADR-0007 Layered matching cascade, manual queue over silent merges

Status: accepted, 2026-08-10

## Context

Deciding whether `steam:292030` and `gog:1495134320` are the same game is the
hard problem in this project. Store names carry edition marketing, punctuation
and trademarks; different games share titles (*Prey* 2006 and *Prey* 2017);
remasters and originals are neither clearly the same nor clearly different.

The two failure modes are not symmetric. A false negative leaves two cards for
one game — visible, annoying, and fixed in one click. A false positive merges
two different games, folds their playtime together and destroys the distinction
in a way the user may not notice for months.

## Decision

Five layers, cheapest and most certain first:

| Layer | Method | Expected coverage |
|---|---|---|
| 1 | Hard IDs (IGDB `external_games`) | ~70% |
| 2 | Curated alias dataset | ~20% |
| 3 | Normalisation + RapidFuzz / TF-IDF | ~7% |
| 4 | LLM adjudication, batched, structured output | ~3% |
| 5 | Manual review queue | remainder |

Embeddings retrieve candidates; a feature-based classifier decides, using year
delta, publisher, platform overlap, cosine and fuzzy ratio. Below the confidence
threshold, the pair goes to the manual queue rather than being merged. Every
automatic merge is reversible and leaves an audit trail.

Alternatives considered:

- **A single fuzzy threshold.** One number to tune, trivial to implement.
  Rejected: at any threshold loose enough to catch "Complete Edition" it also
  catches *Prey* and *Prey*, and there is no threshold that separates them
  because the titles are identical — only the year and publisher differ, which a
  string ratio cannot see.
- **LLM-first.** Accurate on the hard cases. Rejected as the primary mechanism:
  slow, costly, non-deterministic, requires a key the user may not have, and
  spends the most expensive tool on the 70% that a database join answers
  exactly.

## Consequences

- Each layer is measurable on its own, and the cheap layers carry the volume.
  The M6 goal of ~95% with no manual work is checkable rather than claimable.
- Five layers is five things to build, test and keep consistent, and a bug in an
  early layer silently starves the later ones of the cases they were tuned for.
- The manual queue is work pushed onto the user. An empty queue is only
  achievable by lowering the confidence threshold, which is precisely the trade
  this ADR refuses — so the queue is a permanent feature, not a temporary one.
- The golden set (~300 pairs) has to be maintained alongside the matcher, or the
  per-layer numbers drift into fiction.
- Reversibility constrains the schema: merges must record enough to be undone,
  which is why `match_audit` carries an undo payload (ADR-0015).
