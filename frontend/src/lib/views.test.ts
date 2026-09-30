import { describe, expect, it } from 'vitest'

import { sameQuery } from '@/lib/views'

describe('comparing two queries', () => {
  it('ignores the order the pairs come in', () => {
    expect(sameQuery('status=playing&kind=dlc', 'kind=dlc&status=playing')).toBe(true)
  })

  it('counts a repeated key once per value', () => {
    expect(sameQuery('kind=dlc&kind=game', 'kind=dlc')).toBe(false)
    expect(sameQuery('', '')).toBe(true)
  })
})
