import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import {
  activeCount,
  isInverted,
  KINDS,
  NO_FILTERS,
  RANGES,
  STATUSES,
  STEAM_REVIEWS,
  type Filters,
  type Addons,
  type Hidden,
  type RangeName,
} from '@/lib/filters'
import { genreName } from '@/lib/genres'

/**
 * How long typing in a number has to pause before it filters: "85" is one
 * question, not "8" and then "85", and not two steps in the history.
 */
export const BOUND_DEBOUNCE_MS = 400

const CONTROL =
  'h-8 rounded-lg border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive'

/**
 * Every filter the library API accepts, over the filters the address holds.
 *
 * A `<details>` rather than a dialog: the panel pushes the grid down instead of
 * covering it, so the results under a filter are visible as it is set, and it
 * needs no focus management of its own. Open by default when a filter is
 * already on, so a shared link shows what it is narrowed by.
 */
export function FilterPanel({
  filters,
  platforms,
  genres,
  onChange,
}: {
  filters: Filters
  /** Every platform the user has an account on, with the name it is shown under. */
  platforms: { key: string; name: string }[]
  /** Every genre in the library, with IGDB's name for it. */
  genres: { slug: string; name: string }[]
  onChange: (filters: Filters) => void
}) {
  const { t, i18n } = useTranslation()
  const active = activeCount(filters)
  // Its own state, taken from the filters once: tied to them, clearing the
  // last one would shut the panel under the pointer that cleared it.
  const [open, setOpen] = useState(active > 0)
  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className="rounded-lg border border-border px-4 py-3"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {active > 0 ? t('filters.titleActive', { count: active }) : t('filters.title')}
      </summary>
      <div className="mt-4 grid gap-5">
        <div className="grid gap-5 sm:grid-cols-4">
          <Choices
            legend={t('filters.platform')}
            options={platforms.map(({ key, name }) => ({ value: key, label: name }))}
            chosen={filters.platform}
            onChange={(platform) => onChange({ ...filters, platform })}
          />
          <Choices
            legend={t('filters.kind')}
            options={KINDS.map((kind) => ({ value: kind, label: t(`itemKind.${kind}`) }))}
            chosen={filters.kind}
            onChange={(kind) => onChange({ ...filters, kind })}
          />
          <Choices
            legend={t('filters.genre')}
            options={genres.map(({ slug, name }) => ({ value: slug, label: genreName(i18n, slug, name) }))}
            chosen={filters.genre}
            onChange={(genre) => onChange({ ...filters, genre })}
          />
          <Choices
            legend={t('filters.status')}
            options={STATUSES.map((status) => ({ value: status, label: t(`playStatus.${status}`) }))}
            chosen={filters.status}
            onChange={(status) => onChange({ ...filters, status })}
          />
        </div>
        <div className="grid gap-5 sm:grid-cols-4">
          <Range name="metacritic" filters={filters} onChange={onChange} />
          {/* Said on the panel, because a game at 100% is left out and the
              reason is not on its card: Steam gave too few reviews a verdict. */}
          <Range
            name="steam"
            filters={filters}
            onChange={onChange}
            hint={t('filters.steamHint', { count: filters.steam_reviews_min })}
          >
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              {t('filters.steamReviews')}
              <Bound
                label={t('filters.steamReviews')}
                value={filters.steam_reviews_min}
                bounds={STEAM_REVIEWS}
                scale={1}
                invalid={false}
                // Emptied, the field goes back to the default rather than to no
                // threshold: every score needs at least one review.
                onCommit={(value) =>
                  onChange({ ...filters, steam_reviews_min: value ?? STEAM_REVIEWS.default })
                }
              />
            </label>
          </Range>
          <Range name="year" filters={filters} onChange={onChange} />
          {/* Hours on the page, minutes in the address: the API's unit, and
              the one `playtime_minutes` is reported in everywhere else. */}
          <Range name="playtime" filters={filters} onChange={onChange} scale={60} />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label className="flex items-center gap-2 text-sm">
            {t('filters.hidden')}
            <select
              value={filters.hidden}
              onChange={(event) => onChange({ ...filters, hidden: event.target.value as Hidden })}
              className={CONTROL}
            >
              <option value="exclude">{t('filters.hiddenExclude')}</option>
              <option value="include">{t('filters.hiddenInclude')}</option>
              <option value="only">{t('filters.hiddenOnly')}</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            {t('filters.addons')}
            <select
              value={filters.addons}
              onChange={(event) => onChange({ ...filters, addons: event.target.value as Addons })}
              className={CONTROL}
            >
              <option value="fold">{t('filters.addonsFold')}</option>
              <option value="separate">{t('filters.addonsSeparate')}</option>
            </select>
          </label>
          {/* Also for a changed review threshold alone: it is not counted as a
              filter, since it narrows nothing, but it is still something to clear. */}
          <Button
            variant="outline"
            disabled={active === 0 && filters.steam_reviews_min === STEAM_REVIEWS.default}
            onClick={() => onChange(NO_FILTERS)}
          >
            {t('filters.clear')}
          </Button>
        </div>
      </div>
    </details>
  )
}

function Choices<T extends string>({
  legend,
  options,
  chosen,
  onChange,
}: {
  legend: string
  options: { value: T; label: string }[]
  chosen: T[]
  onChange: (chosen: T[]) => void
}) {
  return (
    <fieldset className="grid content-start gap-1.5 text-sm">
      <legend className="mb-1 font-medium">{legend}</legend>
      {options.map(({ value, label }) => (
        <label key={value} className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={chosen.includes(value)}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...chosen, value]
                  : chosen.filter((other) => other !== value),
              )
            }
          />
          {label}
        </label>
      ))}
    </fieldset>
  )
}

/**
 * A from–to pair. What is typed is kept here until it is a value the API
 * accepts, so "2" on the way to "2018" neither reaches the address nor is
 * thrown away under the cursor.
 */
function Range({
  name,
  filters,
  onChange,
  scale = 1,
  hint,
  children,
}: {
  name: RangeName
  filters: Filters
  onChange: (filters: Filters) => void
  scale?: number
  hint?: string
  /** More controls for the same range, under its bounds. */
  children?: React.ReactNode
}) {
  const { t } = useTranslation()
  const inverted = isInverted(filters, name)
  const described = [
    hint ? `filter-${name}-hint` : null,
    inverted ? `filter-${name}-inverted` : null,
  ].filter((id) => id !== null)
  return (
    <fieldset className="grid content-start gap-1.5 text-sm">
      <legend className="mb-1 font-medium">{t(`filters.${name}`)}</legend>
      <div className="flex items-center gap-2">
        {(['min', 'max'] as const).map((end) => (
          <Bound
            key={end}
            label={t(`filters.${end}`, { range: t(`filters.${name}`) })}
            value={filters[`${name}_${end}`]}
            bounds={RANGES[name]}
            scale={scale}
            // Why, not only that: a screen reader otherwise hears "invalid" and
            // nothing it could act on.
            describedBy={described.length > 0 ? described.join(' ') : undefined}
            invalid={inverted}
            onCommit={(value) => onChange({ ...filters, [`${name}_${end}`]: value })}
          />
        ))}
      </div>
      {children}
      {hint ? (
        <p id={`filter-${name}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {inverted ? (
        <p id={`filter-${name}-inverted`} className="text-xs text-destructive">
          {t('filters.inverted')}
        </p>
      ) : null}
    </fieldset>
  )
}

function Bound({
  label,
  value,
  bounds,
  scale,
  invalid,
  describedBy,
  onCommit,
}: {
  label: string
  value: number | null
  bounds: { min: number; max: number }
  scale: number
  invalid: boolean
  describedBy?: string
  onCommit: (value: number | null) => void
}) {
  // Shown exactly, to two places: a link filtering at 90 minutes shows 1.5
  // hours, not a rounded 2 over a list cut at one and a half.
  const shown = (stored: number | null) =>
    stored === null ? '' : String(Number((stored / scale).toFixed(2)))
  const [typed, setTyped] = useState(shown(value))
  // Fractions only where the page's unit is coarser than the API's.
  const pattern = scale > 1 ? /^\d+(\.\d+)?$/ : /^\d+$/
  const parsed = pattern.test(typed) ? Math.round(Number(typed) * scale) : null
  // Taken from outside only when it moved without this field: clearing the
  // filters, or going back through the history. A value this field committed
  // itself is already what is typed, and resetting to it would move the cursor.
  const [seen, setSeen] = useState(value)
  if (value !== seen) {
    setSeen(value)
    if (value !== parsed) setTyped(shown(value))
  }
  const acceptable = parsed !== null && parsed >= bounds.min && parsed <= bounds.max
  const next = typed === '' ? null : acceptable ? parsed : undefined
  const commit = () => {
    if (next !== undefined && next !== value) onCommit(next)
  }
  // The latest callback, read when the timer fires. A dependency on it would
  // restart the pause on every render of the page, and the library re-renders
  // on its own while a sync's steps are polled.
  const latest = useRef(onCommit)
  useEffect(() => {
    latest.current = onCommit
  })
  useEffect(() => {
    if (next === undefined || next === value) return
    const timer = setTimeout(() => latest.current(next), BOUND_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [next, value])
  return (
    <input
      type="number"
      inputMode={scale > 1 ? 'decimal' : 'numeric'}
      aria-label={label}
      aria-invalid={invalid || (typed !== '' && !acceptable) || undefined}
      aria-describedby={describedBy}
      min={Math.ceil(bounds.min / scale)}
      max={Math.floor(bounds.max / scale)}
      step={scale > 1 ? 'any' : 1}
      value={typed}
      onChange={(event) => setTyped(event.target.value)}
      // Leaving the field or pressing Enter is the user saying they are done,
      // so neither waits for the pause. A number the API would refuse is not
      // kept past that: left in the field, it would sit there after the
      // filters were cleared, marked invalid and applying nothing.
      onBlur={() => {
        if (next === undefined) setTyped(shown(value))
        else commit()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commit()
      }}
      className={`${CONTROL} w-24`}
    />
  )
}
