import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import {
  activeCount,
  isInverted,
  KINDS,
  NO_FILTERS,
  RANGES,
  STATUSES,
  type Filters,
  type Hidden,
  type RangeName,
} from '@/lib/filters'

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
  onChange,
}: {
  filters: Filters
  /** Every platform the user has an account on, with the name it is shown under. */
  platforms: { key: string; name: string }[]
  onChange: (filters: Filters) => void
}) {
  const { t } = useTranslation()
  const active = activeCount(filters)
  return (
    <details open={active > 0} className="rounded-lg border border-border px-4 py-3">
      <summary className="cursor-pointer text-sm font-medium">
        {active > 0 ? t('filters.titleActive', { count: active }) : t('filters.title')}
      </summary>
      <div className="mt-4 grid gap-5">
        <div className="grid gap-5 sm:grid-cols-3">
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
            legend={t('filters.status')}
            options={STATUSES.map((status) => ({ value: status, label: t(`playStatus.${status}`) }))}
            chosen={filters.status}
            onChange={(status) => onChange({ ...filters, status })}
          />
        </div>
        <div className="grid gap-5 sm:grid-cols-3">
          <Range name="metacritic" filters={filters} onChange={onChange} />
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
          <Button variant="outline" disabled={active === 0} onClick={() => onChange(NO_FILTERS)}>
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
}: {
  name: RangeName
  filters: Filters
  onChange: (filters: Filters) => void
  scale?: number
}) {
  const { t } = useTranslation()
  const inverted = isInverted(filters, name)
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
            invalid={inverted}
            onCommit={(value) => onChange({ ...filters, [`${name}_${end}`]: value })}
          />
        ))}
      </div>
      {inverted ? <p className="text-xs text-destructive">{t('filters.inverted')}</p> : null}
    </fieldset>
  )
}

function Bound({
  label,
  value,
  bounds,
  scale,
  invalid,
  onCommit,
}: {
  label: string
  value: number | null
  bounds: { min: number; max: number }
  scale: number
  invalid: boolean
  onCommit: (value: number | null) => void
}) {
  const shown = (stored: number | null) => (stored === null ? '' : String(Math.round(stored / scale)))
  const [typed, setTyped] = useState(shown(value))
  const parsed = /^\d+$/.test(typed) ? Number(typed) * scale : null
  // Taken from outside only when it moved without this field: clearing the
  // filters, or going back through the history. A value this field committed
  // itself is already what is typed, and resetting to it would move the cursor.
  const [seen, setSeen] = useState(value)
  if (value !== seen) {
    setSeen(value)
    if (value !== parsed) setTyped(shown(value))
  }
  const acceptable = parsed !== null && parsed >= bounds.min && parsed <= bounds.max
  return (
    <input
      type="number"
      inputMode="numeric"
      aria-label={label}
      aria-invalid={invalid || (typed !== '' && !acceptable) || undefined}
      min={Math.ceil(bounds.min / scale)}
      max={Math.floor(bounds.max / scale)}
      value={typed}
      onChange={(event) => {
        const next = event.target.value
        setTyped(next)
        if (next === '') onCommit(null)
        else if (/^\d+$/.test(next)) {
          const minutes = Number(next) * scale
          if (minutes >= bounds.min && minutes <= bounds.max) onCommit(minutes)
        }
      }}
      className={`${CONTROL} w-24`}
    />
  )
}
