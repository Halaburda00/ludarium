import { describe, expect, it } from 'vitest'

import {
  activeCount,
  apiParams,
  NO_FILTERS,
  readFilters,
  writeFilters,
  type Filters,
} from '@/lib/filters'

const read = (query: string) => readFilters(new URLSearchParams(query))

describe('reading filters from the address', () => {
  it('takes what the API takes, repeated keys as lists', () => {
    expect(read('platform=steam&platform=epic&kind=dlc&metacritic_min=80&hidden=only')).toEqual({
      ...NO_FILTERS,
      platform: ['steam', 'epic'],
      kind: ['dlc'],
      metacritic_min: 80,
      hidden: 'only',
    })
  })

  it('drops what the API would refuse rather than failing the page over it', () => {
    expect(
      read(
        'kind=spaceship&status=abandoned&metacritic_min=101&year_min=1066' +
          '&playtime_max=-5&year_max=20x&hidden=sometimes&platform=Not%20A%20Key',
      ),
    ).toEqual(NO_FILTERS)
  })

  it('reads a value given twice once', () => {
    expect(read('kind=game&kind=game').kind).toEqual(['game'])
  })
})

describe('writing filters to the address', () => {
  it('keeps the search and anything else it does not own', () => {
    const next = writeFilters(
      { ...NO_FILTERS, status: ['playing'] },
      new URLSearchParams('q=hades&kind=dlc&other=1'),
    )
    expect(next.toString()).toBe('q=hades&other=1&status=playing')
  })

  it('leaves the defaults out, so the plain library has a plain address', () => {
    expect(writeFilters(NO_FILTERS, new URLSearchParams()).toString()).toBe('')
  })

  it('round-trips through the address', () => {
    const filters: Filters = {
      ...NO_FILTERS,
      platform: ['steam'],
      steam_min: 80,
      year_min: 2010,
      year_max: 2020,
      playtime_max: 0,
      hidden: 'include',
    }
    expect(readFilters(writeFilters(filters, new URLSearchParams()))).toEqual(filters)
  })
})

describe('asking the API', () => {
  it('leaves out a range whose minimum is above its maximum', () => {
    // Typed a field at a time, a range passes through this on the way to the
    // one the user meant; the API would answer it with a 422.
    const params = apiParams({ ...NO_FILTERS, year_min: 2020, year_max: 2010, kind: ['game'] })
    expect(params.toString()).toBe('kind=game')
  })
})

describe('counting active filters', () => {
  it('counts a range once, whichever ends are set', () => {
    expect(activeCount({ ...NO_FILTERS, year_min: 2010, year_max: 2020 })).toBe(1)
    expect(activeCount({ ...NO_FILTERS, hidden: 'include', kind: ['dlc', 'game'] })).toBe(2)
    expect(activeCount(NO_FILTERS)).toBe(0)
  })
})
