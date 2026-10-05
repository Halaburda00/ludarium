import { describe, expect, it } from 'vitest'

import { movedInQueue, queueMove, type WorkSummary } from '@/lib/queries'

function work(id: number, queue_position: number): WorkSummary {
  return { id, queue_position } as WorkSummary
}

// Position 2 is held by a work the listing does not show.
const SHOWN = [work(1, 1), work(2, 3), work(3, 4)]

describe('a move in the shown queue', () => {
  it('hands the positions out again in the new order, gap and all', () => {
    expect(movedInQueue(SHOWN, 3, 0).map((w) => [w.id, w.queue_position])).toEqual([
      [3, 1],
      [1, 3],
      [2, 4],
    ])
  })

  it('sends the position of the work at the place it goes to', () => {
    expect(queueMove(SHOWN, 1, 1)).toEqual({ id: 1, to: 1, position: 3 })
    expect(queueMove(SHOWN, 3, 0)).toEqual({ id: 3, to: 0, position: 1 })
  })

  it('leaves the list alone for a work it does not hold', () => {
    expect(movedInQueue(SHOWN, 9, 0)).toBe(SHOWN)
  })
})
