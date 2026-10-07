import { describe, expect, it } from 'vitest'

import { initials } from '@/lib/initials'

describe('initials', () => {
  it.each([
    ['Lanterns of Vael', 'LV'],
    ['The Quiet Orbit', 'TQ'],
    ['Brine & Bramble', 'BB'],
    ['Mirelight', 'M'],
    ['7 Days to Die', '7D'],
    ['"Quoted" Title', 'QT'],
    ['ōkami hd', 'ŌH'],
    ['', ''],
  ])('%s is %s', (title, expected) => {
    expect(initials(title)).toBe(expected)
  })
})
