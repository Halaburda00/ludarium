import { apiParams, type Filters } from '@/lib/filters'
import { sortParams, type Sorting } from '@/lib/sorting'

/**
 * The filters and the order as the API's query string: what the listing is
 * asked, and what a saved view stores.
 */
export function viewQuery(filters: Filters, sorting: Sorting): string {
  const params = apiParams(filters)
  for (const [key, value] of sortParams(sorting)) params.append(key, value)
  return params.toString()
}

/**
 * Whether two query strings ask the same question. The server writes a view's
 * parameters in its own order and the page in another, so the pairs are
 * compared rather than the strings.
 */
export function sameQuery(a: string, b: string): boolean {
  const pairs = (query: string) =>
    [...new URLSearchParams(query)].map(([key, value]) => `${key}=${value}`).sort()
  return pairs(a).join('&') === pairs(b).join('&')
}
