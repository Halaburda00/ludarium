import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ThemePicker } from '@/components/ThemePicker'
import { Button } from '@/components/ui/button'
import { Field, Notice } from '@/components/ui/field'
import { KINDS } from '@/lib/filters'
import {
  useDeleteManualEntry,
  useManualEntry,
  useSaveManualEntry,
  type ManualEntry as Entry,
  type ManualEntryForm,
  type OwnershipType,
} from '@/lib/queries'

// The likely ones first: a disc, then a key or a download.
const OWNERSHIP: OwnershipType[] = [
  'physical',
  'owned',
  'free',
  'subscription',
  'family_shared',
  'trial',
]

// The API's bounds, so the browser says so before the server has to.
const FIRST_YEAR = 1950
const LAST_YEAR = 2100

const BLANK: ManualEntryForm = {
  title: '',
  store_label: null,
  ownership_type: 'physical',
  item_kind: 'game',
  release_year: null,
}

const CONTROL =
  'h-9 rounded-lg border border-input bg-background px-3 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50'

/**
 * A game no platform reports, added or edited by hand (#97): a disc, an
 * unredeemed key, an itch.io download. `/manual/new` adds one; `/manual/:id`
 * edits the entry with that id, and deletes it.
 */
export default function ManualEntryPage() {
  const { t } = useTranslation()
  const { entitlementId } = useParams()
  const adding = entitlementId === undefined
  // As on a work's page: anything but a positive whole number is no entry.
  const id = /^[1-9]\d{0,15}$/.test(entitlementId ?? '') ? Number(entitlementId) : null
  const entry = useManualEntry(id ?? 0, !adding && id !== null)

  let body: React.ReactNode
  if (adding) {
    body = <EntryForm initial={BLANK} id={null} />
  } else if (id === null || entry.error?.status === 404) {
    body = <p className="text-sm text-muted-foreground">{t('manual.notFound')}</p>
  } else if (entry.isPending) {
    body = <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
  } else if (entry.isError) {
    body = (
      <div className="grid justify-items-start gap-3">
        <Notice>{entry.error.detail || t('error.offline')}</Notice>
        <Button variant="outline" onClick={() => void entry.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    )
  } else {
    // Keyed, so the form starts from this entry rather than from the last one.
    body = <EntryForm key={entry.data.id} initial={asForm(entry.data)} id={entry.data.id} />
  }

  return (
    <main className="mx-auto grid max-w-xl gap-6 px-6 py-10">
      <header className="flex items-center justify-between gap-4">
        <nav>
          <Link to="/library" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('work.back')}
          </Link>
        </nav>
        <ThemePicker />
      </header>
      <div className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">
          {t(adding ? 'manual.addTitle' : 'manual.editTitle')}
        </h1>
        <p className="text-sm text-muted-foreground">{t('manual.intro')}</p>
      </div>
      {body}
    </main>
  )
}

function asForm(entry: Entry): ManualEntryForm {
  return {
    title: entry.title,
    store_label: entry.store_label,
    ownership_type: entry.ownership_type,
    // An entry made outside this form may have no kind; saving it says one.
    item_kind: entry.item_kind ?? 'game',
    release_year: entry.release_year,
  }
}

function EntryForm({ initial, id }: { initial: ManualEntryForm; id: number | null }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const save = useSaveManualEntry(id)
  const [form, setForm] = useState(initial)
  // Typed as text, so a half-typed year is not turned into a number mid-word.
  const [year, setYear] = useState(initial.release_year?.toString() ?? '')
  const set = (changes: Partial<ManualEntryForm>) => setForm((current) => ({ ...current, ...changes }))

  return (
    <div className="grid gap-8">
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          save.mutate(
            {
              ...form,
              title: form.title.trim(),
              store_label: form.store_label?.trim() || null,
              release_year: year.trim() ? Number(year) : null,
            },
            { onSuccess: (saved) => void navigate(`/library/${saved.work_id}`) },
          )
        }}
      >
        <Field
          id="manual-title"
          label={t('manual.title')}
          value={form.title}
          onChange={(event) => set({ title: event.target.value })}
          required
          maxLength={300}
          autoComplete="off"
        />
        <Field
          id="manual-store"
          label={t('manual.store')}
          hint={t('manual.storeHint')}
          value={form.store_label ?? ''}
          onChange={(event) => set({ store_label: event.target.value })}
          maxLength={100}
          autoComplete="off"
        />
        <label className="grid gap-1.5 text-sm font-medium">
          {t('manual.ownership')}
          <select
            value={form.ownership_type}
            onChange={(event) => set({ ownership_type: event.target.value as OwnershipType })}
            className={CONTROL}
          >
            {OWNERSHIP.map((ownership) => (
              <option key={ownership} value={ownership}>
                {t(`ownershipType.${ownership}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-1.5 text-sm font-medium">
          {t('manual.kind')}
          <select
            value={form.item_kind}
            onChange={(event) =>
              set({ item_kind: event.target.value as ManualEntryForm['item_kind'] })
            }
            className={CONTROL}
          >
            {KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {t(`itemKind.${kind}`)}
              </option>
            ))}
          </select>
        </label>
        <Field
          id="manual-year"
          type="number"
          label={t('manual.year')}
          hint={t('manual.yearHint')}
          value={year}
          onChange={(event) => setYear(event.target.value)}
          min={FIRST_YEAR}
          max={LAST_YEAR}
          step={1}
          className="max-w-32"
        />
        {save.isError ? <Notice>{save.error.detail || t('error.offline')}</Notice> : null}
        <div>
          <Button type="submit" disabled={save.isPending}>
            {t(id === null ? 'manual.add' : 'manual.save')}
          </Button>
        </div>
      </form>
      {id !== null ? <Delete id={id} title={initial.title} /> : null}
    </div>
  )
}

/** Two steps, because there is no removed view to bring a typed entry back from (ADR-0031). */
function Delete({ id, title }: { id: number; title: string }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const remove = useDeleteManualEntry()
  const [asked, setAsked] = useState(false)

  return (
    <section aria-labelledby="manual-delete" className="grid justify-items-start gap-2 text-sm">
      <h2 id="manual-delete" className="font-heading text-lg font-semibold">
        {t('manual.deleteHeading')}
      </h2>
      {asked ? (
        <>
          <p>{t('manual.deleteConfirm', { title })}</p>
          <div className="flex gap-2">
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() =>
                remove.mutate(id, {
                  onSuccess: () => void navigate('/library', { replace: true }),
                })
              }
            >
              {t('manual.deleteForGood')}
            </Button>
            <Button variant="outline" disabled={remove.isPending} onClick={() => setAsked(false)}>
              {t('manual.keep')}
            </Button>
          </div>
        </>
      ) : (
        <Button variant="outline" onClick={() => setAsked(true)}>
          {t('manual.delete')}
        </Button>
      )}
      {remove.isError ? <Notice>{remove.error.detail || t('error.offline')}</Notice> : null}
    </section>
  )
}
