import type { components } from '@/lib/api-types'

/**
 * The library's order, as the address holds it.
 *
 * Beside the filters rather than among them: an order narrows nothing, so it
 * is not counted as a filter and "clear filters" leaves it alone. The names and
 * values are the API's own (`backend/src/ludarium/sorting.py`), passed through
 * as the filters are.
 */

export type SortBy = components['schemas']['Sort']
export type Order = components['schemas']['Direction']
export type Sorting = { sort: SortBy; order: Order }

export const SORTS = [
  'title',
  'metacritic',
  'steam_reviews',
  'playtime',
  'last_played',
  'release_date',
] as const satisfies readonly SortBy[]

export const ORDERS = ['asc', 'desc'] as const satisfies readonly Order[]

export const DEFAULT_SORTING: Sorting = { sort: 'title', order: 'asc' }

/**
 * The direction each order starts in when it is picked: the best-rated, the
 * most played, the latest first. Titles read A to Z. Nobody picks "Metacritic"
 * to see the worst game they own first.
 */
export const FIRST_ORDER: Record<SortBy, Order> = {
  title: 'asc',
  metacritic: 'desc',
  steam_reviews: 'desc',
  playtime: 'desc',
  last_played: 'desc',
  release_date: 'desc',
}

/** The order a URL asks for, with an unknown value read as the default rather than an error. */
export function readSorting(params: URLSearchParams): Sorting {
  const sort = params.get('sort')
  const order = params.get('order')
  return {
    sort: sort !== null && isOneOf(SORTS)(sort) ? sort : DEFAULT_SORTING.sort,
    order: order !== null && isOneOf(ORDERS)(order) ? order : DEFAULT_SORTING.order,
  }
}

/** `current` with the order written over it, and every other parameter kept. */
export function writeSorting(sorting: Sorting, current: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(current)
  next.delete('sort')
  next.delete('order')
  for (const [key, value] of sortParams(sorting)) next.append(key, value)
  return next
}

/**
 * The order as the API's query string. The defaults are left out, so the plain
 * library keeps a plain address and one cache entry.
 */
export function sortParams(sorting: Sorting): [string, string][] {
  const out: [string, string][] = []
  if (sorting.sort !== DEFAULT_SORTING.sort) out.push(['sort', sorting.sort])
  if (sorting.order !== DEFAULT_SORTING.order) out.push(['order', sorting.order])
  return out
}

function isOneOf<T extends string>(allowed: readonly T[]) {
  return (value: string): value is T => (allowed as readonly string[]).includes(value)
}
