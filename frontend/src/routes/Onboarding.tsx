import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Field, Notice } from '@/components/ui/field'
import { useConnect } from '@/lib/queries'

export default function Onboarding() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const connect = useConnect()
  const [apiKey, setApiKey] = useState('')
  const [steamId, setSteamId] = useState('')
  const [label, setLabel] = useState('Main')

  return (
    <main className="mx-auto grid min-h-dvh max-w-md content-center gap-6 px-6">
      <header className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t('onboarding.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('onboarding.intro')}</p>
      </header>

      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          connect.mutate(
            { provider: 'steam', external_account_id: steamId, label, credentials: apiKey },
            {
              onSuccess: () => {
                // Dropped from state the moment the server has it. It is never
                // put in `localStorage`, never in the URL, and never rendered
                // back — the response carries a mask, not the key (rule 7).
                setApiKey('')
                void navigate('/library', { replace: true })
              },
            },
          )
        }}
      >
        <Field
          id="api-key"
          label={t('onboarding.apiKey')}
          hint={t('onboarding.apiKeyHint')}
          // `password`, so it is not shoulder-read and not offered to a password
          // manager as a username.
          type="password"
          autoComplete="off"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
        />
        <Field
          id="steam-id"
          label={t('onboarding.steamId')}
          hint={t('onboarding.steamIdHint')}
          inputMode="numeric"
          value={steamId}
          onChange={(event) => setSteamId(event.target.value)}
        />
        <Field
          id="label"
          label={t('onboarding.label')}
          value={label}
          onChange={(event) => setLabel(event.target.value)}
        />
        {connect.isError ? <Notice>{connect.error.detail}</Notice> : null}
        <Button type="submit" disabled={connect.isPending}>
          {connect.isPending ? t('onboarding.working') : t('onboarding.submit')}
        </Button>
      </form>
    </main>
  )
}
