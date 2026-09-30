import { useTranslation } from 'react-i18next'

import { FIRST_ORDER, ORDERS, SORTS, type Order, type SortBy, type Sorting } from '@/lib/sorting'

const CONTROL =
  'h-8 rounded-lg border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50'

/** What the library is ordered by, and which way. */
export function SortControl({
  sorting,
  onChange,
}: {
  sorting: Sorting
  onChange: (sorting: Sorting) => void
}) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-2 text-sm">
        {t('sorting.label')}
        <select
          value={sorting.sort}
          // A new order starts the way it is usually read; the second control
          // turns it round.
          onChange={(event) => {
            const sort = event.target.value as SortBy
            onChange({ sort, order: FIRST_ORDER[sort] })
          }}
          className={CONTROL}
        >
          {SORTS.map((sort) => (
            <option key={sort} value={sort}>
              {t(`sorting.by.${sort}`)}
            </option>
          ))}
        </select>
      </label>
      {/* Named for the order it is in — "highest first", "newest first" — as
          "ascending" means little about a score and less about a date. */}
      <select
        aria-label={t('sorting.direction')}
        value={sorting.order}
        onChange={(event) => onChange({ ...sorting, order: event.target.value as Order })}
        className={CONTROL}
      >
        {ORDERS.map((order) => (
          <option key={order} value={order}>
            {t(`sorting.order.${sorting.sort}.${order}`)}
          </option>
        ))}
      </select>
    </div>
  )
}
