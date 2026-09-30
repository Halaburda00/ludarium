import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import { Field, Notice } from '@/components/ui/field'
import {
  useDeleteView,
  useRenameView,
  useReorderViews,
  useSaveView,
  useViews,
  type SavedView,
} from '@/lib/queries'
import { sameQuery } from '@/lib/views'

/**
 * The user's saved views, and the way to save the one on the screen.
 *
 * A view is a query string with a name, so opening one hands its string to the
 * address and the library reads it from there like any other link: nothing
 * about the grid knows a view was involved.
 */
export function SavedViews({
  current,
  onOpen,
}: {
  /** The filters and order on the screen, as the API's query string. */
  current: string
  onOpen: (query: string) => void
}) {
  const { t } = useTranslation()
  const views = useViews()
  const save = useSaveView()
  const rename = useRenameView()
  const reorder = useReorderViews()
  const remove = useDeleteView()
  const [name, setName] = useState('')
  const [editing, setEditing] = useState(false)
  // The id, not the view: looked up in the list, so a rename shows the new
  // name and a deleted view has nothing left to warn about.
  const [openedId, setOpenedId] = useState<number | null>(null)

  const list = views.data ?? []
  const opened = list.find((view) => view.id === openedId)
  const mutations = [save, rename, reorder, remove]
  const failed = mutations.find((mutation) => mutation.isError)?.error
  // Every action starts by forgetting the last one's refusal: a 409 from a
  // save is not news once a delete has gone through.
  const forget = () => mutations.forEach((mutation) => mutation.reset())
  // Said while the view is still what is on the screen, and not after: once
  // a filter has changed, the grid is no longer the view the notice is about.
  const partial =
    opened && opened.dropped.length > 0 && sameQuery(opened.query, current) ? opened : null

  const move = (index: number, by: -1 | 1) => {
    const ids = list.map((view) => view.id)
    ;[ids[index], ids[index + by]] = [ids[index + by], ids[index]]
    forget()
    reorder.mutate(ids)
  }

  return (
    <section aria-labelledby="saved-views" className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="saved-views" className="text-sm font-medium">
          {t('views.title')}
        </h2>
        {list.length > 0 ? (
          <Button variant="ghost" size="sm" onClick={() => setEditing(!editing)}>
            {editing ? t('views.done') : t('views.edit')}
          </Button>
        ) : null}
      </div>

      {list.length === 0 && views.isSuccess ? (
        <p className="text-sm text-muted-foreground">{t('views.none')}</p>
      ) : null}

      <ul className={editing ? 'grid gap-2' : 'flex flex-wrap gap-2'}>
        {list.map((view, index) =>
          editing ? (
            <li key={view.id} className="flex flex-wrap items-end gap-2">
              <Rename
                view={view}
                onRename={(next) => {
                  forget()
                  rename.mutate({ id: view.id, name: next })
                }}
              />
              <Button
                variant="outline"
                size="sm"
                aria-label={t('views.moveUp', { name: view.name })}
                disabled={index === 0 || reorder.isPending}
                onClick={() => move(index, -1)}
              >
                ↑
              </Button>
              <Button
                variant="outline"
                size="sm"
                aria-label={t('views.moveDown', { name: view.name })}
                disabled={index === list.length - 1 || reorder.isPending}
                onClick={() => move(index, 1)}
              >
                ↓
              </Button>
              <Button
                variant="outline"
                size="sm"
                aria-label={t('views.delete', { name: view.name })}
                onClick={() => {
                  forget()
                  remove.mutate(view.id)
                }}
              >
                {t('views.deleteShort')}
              </Button>
            </li>
          ) : (
            <li key={view.id}>
              <Button
                variant={sameQuery(view.query, current) ? 'default' : 'outline'}
                size="sm"
                aria-current={sameQuery(view.query, current) ? 'true' : undefined}
                onClick={() => {
                  setOpenedId(view.id)
                  onOpen(view.query)
                }}
              >
                {view.name}
              </Button>
            </li>
          ),
        )}
      </ul>

      {partial ? (
        <Notice tone="warn">
          {t('views.dropped', { name: partial.name, dropped: partial.dropped.join(', ') })}
        </Notice>
      ) : null}
      {failed ? <Notice>{failed.detail}</Notice> : null}

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          forget()
          save.mutate({ name: name.trim(), query: current }, { onSuccess: () => setName('') })
        }}
      >
        <Field
          id="view-name"
          label={t('views.name')}
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={100}
          autoComplete="off"
        />
        <Button type="submit" variant="outline" disabled={!name.trim() || save.isPending}>
          {t('views.save')}
        </Button>
      </form>
    </section>
  )
}

function Rename({ view, onRename }: { view: SavedView; onRename: (name: string) => void }) {
  const { t } = useTranslation()
  const [name, setName] = useState(view.name)
  const changed = name.trim() !== '' && name.trim() !== view.name
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        if (changed) onRename(name.trim())
      }}
    >
      <Field
        id={`view-${view.id}-name`}
        label={t('views.renameLabel', { name: view.name })}
        value={name}
        onChange={(event) => setName(event.target.value)}
        maxLength={100}
        autoComplete="off"
      />
      <Button type="submit" variant="outline" size="sm" disabled={!changed}>
        {t('views.rename')}
      </Button>
    </form>
  )
}
