# ADR-0033 The play queue is a status with a dense position

Status: accepted, 2026-10-05.

## Context

`not_started` covers every owned game nobody has touched, giveaway filler
included, and `wishlist` means a game to buy. Neither answers "what do I play
next". #121 asks for a short list the user orders by hand, 1 to n.

## Decision

**Queued is a `PlayStatus`, and its place is a column beside it.**
`PlayStatus.QUEUED` sits between `not_started` and `playing`, and
`user_work_state.queue_position` holds the place. A `CHECK` ties the two
together: a queued work has a position of at least 1, and no other work has
one. A work that starts being played leaves the queue by the same update that
says so, so there is no second list to keep in step with the status.

**Positions are dense and renumbered whole.** Every change that could open a
gap or a tie (a move, a work leaving the queue, a merge, its undo, a deleted
work) ends in `queue.arrange`, which writes 1..n again. A queue is tens of
works, so rewriting it costs nothing. Fractional keys or gaps would buy
cheaper single moves, which nobody needs, at the price of a position that is
not the number the user sees.

**A unique constraint on `(user_id, queue_position)` guards the places.** On
SQLite, `BEGIN IMMEDIATE` already serialises two moves (ADR-0016). On
PostgreSQL the constraint is what refuses the second one (ADR-0017). SQLite
checks it row by row inside an `UPDATE`, so a renumber that shifted rows in
place would collide with itself. The renumber therefore runs in two passes:
first it lifts every row past the highest position, then it writes the final
numbers onto places that are now free. A row whose place is not yet decided
(a merge, an undo) waits past the end in the same way.

**The queue is read through the library listing.** `sort=queue` is one more
order over `user_work_state` with the usual keyset, and `status=queued` is the
existing filter. No separate endpoint lists the queue. The move is
`PUT /api/works/{id}/queue`, and it refuses a work that is not queued: queueing
is a change of status, made through the state update.

## Consequences

- A work whose copies have all been removed keeps its place: sync does not
  write user state (rule 3). The listing does not show it, and it is back in
  its place once a copy is restored. The "Up next" view and the work page
  number the works shown, 1 to k, rather than the stored positions, so the
  gap is never seen. A move sends the stored position of the work it swaps
  with, so it lands beside that work and not beside the unseen one.
- A merge keeps the better of the two places, and its undo puts both works
  back where they were. The rest of the queue is renumbered around them.
- The downgrade turns a queued work back into `not_started`. The order is lost.
