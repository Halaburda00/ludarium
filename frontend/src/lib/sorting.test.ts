import { describe, expect, it } from 'vitest'

import { DEFAULT_SORTING, readSorting, writeSorting } from '@/lib/sorting'

const read = (query: string) => readSorting(new URLSearchParams(query))

describe('the order in the address', () => {
  it('reads what the API takes', () => {
    expect(read('sort=release_date&order=desc')).toEqual({ sort: 'release_date', order: 'desc' })
  })

  it('reads anything else as the default rather than failing the page', () => {
    expect(read('sort=rating&order=sideways')).toEqual(DEFAULT_SORTING)
  })

  it('leaves the defaults out, and keeps every parameter it does not own', () => {
    const current = new URLSearchParams('q=hades&kind=dlc&sort=playtime&order=desc')

    expect(writeSorting(DEFAULT_SORTING, current).toString()).toBe('q=hades&kind=dlc')
    expect(writeSorting({ sort: 'metacritic', order: 'desc' }, current).toString()).toBe(
      'q=hades&kind=dlc&sort=metacritic&order=desc',
    )
  })
})
