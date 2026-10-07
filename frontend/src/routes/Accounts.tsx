import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { ThemePicker } from '@/components/ThemePicker'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import { useDemo } from '@/lib/demo'
import { useAccounts, useUpdateAccount, type Account } from '@/lib/queries'

// The account that holds games added by hand. It has no sync and no health,
// and the games themselves are where it is managed (#97).
const MANUAL = 'manual'

/**
 * Every account the library comes from, with its health, a name the user
 * chooses, and a switch (#129).
 *
 * Switching one off stops its syncs and keeps its games: nothing is removed
 * (rule 1). Deleting an account is not offered at all.
 */
export default function Accounts() {
  const { t } = useTranslation()
  const accounts = useAccounts()
  const update = useUpdateAccount()
  const demo = useDemo()

  const listed = (accounts.data ?? []).filter((account) => account.provider !== MANUAL)
  // How many accounts each platform has here. An account's id is noise beside
  // its platform's name until there is a second account it has to be told from.
  const perPlatform = new Map<string, number>()
  for (const account of listed) {
    perPlatform.set(account.provider, (perPlatform.get(account.provider) ?? 0) + 1)
  }
  let body: React.ReactNode
  if (accounts.isPending) {
    body = <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
  } else if (accounts.isError) {
    body = <Notice>{accounts.error.detail || t('error.offline')}</Notice>
  } else if (listed.length === 0) {
    body = <p className="text-sm text-muted-foreground">{t('accounts.empty')}</p>
  } else {
    body = (
      <ul className="grid gap-3">
        {listed.map((account) => (
          <Row
            key={account.id}
            account={account}
            shared={(perPlatform.get(account.provider) ?? 0) > 1}
            // A demo refuses every write (ADR-0034), so it offers none.
            editable={!demo}
            busy={update.isPending && update.variables.id === account.id}
            onChange={(changes) => update.mutate({ id: account.id, changes })}
          />
        ))}
      </ul>
    )
  }

  return (
    <main className="mx-auto grid max-w-4xl gap-6 px-6 py-10">
      <header className="flex items-center justify-between gap-4">
        <nav>
          <Link to="/library" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('work.back')}
          </Link>
        </nav>
        <ThemePicker />
      </header>
      <div className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t('accounts.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('accounts.intro')}</p>
      </div>
      {update.isError ? <Notice>{update.error.detail}</Notice> : null}
      {body}
    </main>
  )
}

function Row({
  account,
  shared,
  editable,
  busy,
  onChange,
}: {
  account: Account
  // Another account is on the same platform.
  shared: boolean
  editable: boolean
  busy: boolean
  onChange: (changes: { label?: string; is_active?: boolean }) => void
}) {
  const { t, i18n } = useTranslation()
  const [label, setLabel] = useState(account.label)
  const day = new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium' })
  const named = { label: account.label, provider: account.provider_name }
  // Signing in again fixes a credential, and only a connected account has one.
  const signInAgain =
    account.error_kind === 'credentials' && account.is_active && !account.is_derived

  return (
    <li
      className={
        'grid gap-2 rounded-lg border border-border p-4 ' +
        (account.is_active ? '' : 'text-muted-foreground')
      }
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-medium" title={account.external_account_id ?? undefined}>
          {account.provider_name}
          {/* Shown where another account is on the same platform: their names
              may be the same, and the id is what tells them apart. Otherwise
              it waits in the title, for whoever needs to check it. */}
          {shared && account.external_account_id ? (
            <span className="ml-2 font-mono text-xs text-muted-foreground">
              {account.external_account_id}
            </span>
          ) : null}
        </h2>
        <p className="text-sm">
          {account.is_active ? t(`accounts.status.${account.status}`) : t('accounts.off')}
          {account.is_derived ? (
            <span className="ml-2 text-xs text-muted-foreground" title={t('accounts.importedHint')}>
              {t('accounts.imported')}
            </span>
          ) : null}
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        {account.last_success_at
          ? t('accounts.lastSuccess', { date: day.format(new Date(account.last_success_at)) })
          : t('accounts.never')}
      </p>
      {account.is_active && account.last_error ? (
        <p className="text-sm text-destructive">{account.last_error}</p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {editable ? (
          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              onChange({ label })
            }}
          >
            <input
              aria-label={t('accounts.labelFor', {
                provider: account.provider_name,
                id: account.external_account_id ?? account.id,
              })}
              value={label}
              maxLength={256}
              onChange={(event) => setLabel(event.target.value)}
              className="h-8 rounded-md border border-input bg-background px-2 text-sm"
            />
            <Button
              type="submit"
              size="sm"
              variant="outline"
              disabled={busy || !label.trim() || label.trim() === account.label}
            >
              {t('accounts.save')}
            </Button>
          </form>
        ) : (
          <span className="text-sm">{account.label}</span>
        )}
        {editable ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => onChange({ is_active: !account.is_active })}
            aria-label={t(account.is_active ? 'accounts.switchOffLabel' : 'accounts.switchOnLabel', named)}
          >
            {t(account.is_active ? 'accounts.switchOff' : 'accounts.switchOn')}
          </Button>
        ) : null}
        {editable && signInAgain ? (
          <Link
            // The account as well as the platform: with two on one platform,
            // the form has to say which one it is repairing.
            to={`/onboarding?${new URLSearchParams({
              provider: account.provider,
              account: account.external_account_id ?? '',
              label: account.label,
            }).toString()}`}
            className="px-2 text-sm text-primary underline-offset-4 hover:underline"
          >
            {t('library.signInAgain')}
          </Link>
        ) : null}
      </div>
    </li>
  )
}
