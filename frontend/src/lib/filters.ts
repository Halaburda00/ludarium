import type { ItemKind, PlayStatus } from '@/lib/queries'

/**
 * The library's filters, as the address holds them.
 *
 * The URL is the one source of truth: the panel reads it and writes it, and
 * the works query keys on it, so back and forward step through filter changes
 * and a copied link reproduces the view. Its parameter names and values are the
 * API's own (`backend/src/ludarium/filters.py`), so the query string is passed
 * through rather than translated, and there is one vocabulary instead of two.
 */

export type Hidden = 'exclude' | 'include' | 'only'

export type Filters = {
  platform: string[]
  kind: ItemKind[]
  status: PlayStatus[]
  metacritic_min: number | null
  metacritic_max: number | null
  year_min: number | null
  year_max: number | null
  playtime_min: number | null
  playtime_max: number | null
  hidden: Hidden
}

export const KINDS = [
  'game',
  'dlc',
  'demo',
  'playtest',
  'soundtrack',
  'video',
  'tool',
  'mod',
] as const satisfies readonly ItemKind[]

export const STATUSES = [
  'not_started',
  'playing',
  'completed',
  'mastered',
  'on_hold',
  'dropped',
  'wishlist',
] as const satisfies readonly PlayStatus[]

const HIDDEN = ['exclude', 'include', 'only'] as const satisfies readonly Hidden[]

export type RangeName = 'metacritic' | 'year' | 'playtime'

/** The API's bounds for each range, so a value it would refuse never leaves the page. */
export const RANGES: Record<RangeName, { min: number; max: number }> = {
  metacritic: { min: 0, max: 100 },
  year: { min: 1950, max: 2100 },
  // A century, in minutes: `MAX_MINUTES` in the backend.
  playtime: { min: 0, max: 60 * 24 * 365 * 100 },
}

const RANGE_NAMES = Object.keys(RANGES) as RangeName[]

export const NO_FILTERS: Filters = {
  platform: [],
  kind: [],
  status: [],
  metacritic_min: null,
  metacritic_max: null,
  year_min: null,
  year_max: null,
  playtime_min: null,
  playtime_max: null,
  hidden: 'exclude',
}

/** Every key this module owns in the address. Anything else — `q`, above all — is left alone. */
const KEYS = [
  'platform',
  'kind',
  'status',
  ...RANGE_NAMES.flatMap((name) => [`${name}_min`, `${name}_max`]),
  'hidden',
]

/**
 * The filters a URL asks for, with anything unusable dropped.
 *
 * A pasted or hand-edited link should open the library with what it got
 * right, not an error page over what it got wrong. The server refuses the
 * same values, so dropping them here is what keeps the one from reaching the
 * other.
 */
export function readFilters(params: URLSearchParams): Filters {
  const range = (key: string, { min, max }: { min: number; max: number }) => {
    const raw = params.get(key)
    if (raw === null || !/^\d+$/.test(raw)) return null
    const value = Number(raw)
    return value >= min && value <= max ? value : null
  }
  const hidden = params.get('hidden')
  return {
    // A key the backend does not know is still refused there; the syntax is
    // all that can be checked here without a copy of its list.
    platform: unique(params.getAll('platform').filter((key) => /^[a-z_]+$/.test(key))),
    kind: unique(params.getAll('kind')).filter(isOneOf(KINDS)),
    status: unique(params.getAll('status')).filter(isOneOf(STATUSES)),
    metacritic_min: range('metacritic_min', RANGES.metacritic),
    metacritic_max: range('metacritic_max', RANGES.metacritic),
    year_min: range('year_min', RANGES.year),
    year_max: range('year_max', RANGES.year),
    playtime_min: range('playtime_min', RANGES.playtime),
    playtime_max: range('playtime_max', RANGES.playtime),
    hidden: hidden !== null && isOneOf(HIDDEN)(hidden) ? hidden : 'exclude',
  }
}

/** `current` with the filters written over it, and every other parameter kept. */
export function writeFilters(filters: Filters, current: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(current)
  for (const key of KEYS) next.delete(key)
  for (const [key, value] of entries(filters)) next.append(key, value)
  return next
}

/**
 * The filters as the API's query string, leaving out a range that cannot hold
 * anything.
 *
 * The API refuses a minimum above its maximum with a 422. Typed one field at a
 * time, a range passes through that state on the way to the one the user
 * meant, and the grid should not flash an error on the way.
 */
export function apiParams(filters: Filters): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, value] of entries(filters)) {
    const name = RANGE_NAMES.find((range) => key.startsWith(`${range}_`))
    if (name && isInverted(filters, name)) continue
    params.append(key, value)
  }
  return params
}

export function isInverted(filters: Filters, name: RangeName): boolean {
  const min = filters[`${name}_min`]
  const max = filters[`${name}_max`]
  return min !== null && max !== null && min > max
}

/** How many filters narrow the listing, for the panel's label and the clear control. */
export function activeCount(filters: Filters): number {
  return (
    (filters.platform.length > 0 ? 1 : 0) +
    (filters.kind.length > 0 ? 1 : 0) +
    (filters.status.length > 0 ? 1 : 0) +
    RANGE_NAMES.filter(
      (name) => filters[`${name}_min`] !== null || filters[`${name}_max`] !== null,
    ).length +
    (filters.hidden === 'exclude' ? 0 : 1)
  )
}

function entries(filters: Filters): [string, string][] {
  const out: [string, string][] = []
  for (const key of ['platform', 'kind', 'status'] as const) {
    for (const value of filters[key]) out.push([key, value])
  }
  for (const name of RANGE_NAMES) {
    for (const end of ['min', 'max'] as const) {
      const value = filters[`${name}_${end}`]
      if (value !== null) out.push([`${name}_${end}`, String(value)])
    }
  }
  // The default is left out, so the plain library has a plain address.
  if (filters.hidden !== 'exclude') out.push(['hidden', filters.hidden])
  return out
}

function isOneOf<T extends string>(allowed: readonly T[]) {
  return (value: string): value is T => (allowed as readonly string[]).includes(value)
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)]
}
