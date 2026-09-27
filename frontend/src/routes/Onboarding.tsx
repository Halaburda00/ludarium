import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Field, Notice } from '@/components/ui/field'
import { EPIC_LOGIN_URL } from '@/lib/epic'
import { useConnect } from '@/lib/queries'

type Platform = 'steam' | 'epic'

export default function Onboarding() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const connect = useConnect()
  // From the link a failed Epic sync offers, so "sign in again" lands on the
  // right form rather than on Steam's.
  const [params] = useSearchParams()
  const [platform, setPlatform] = useState<Platform>(
    params.get('provider') === 'epic' ? 'epic' : 'steam',
  )
  const [secret, setSecret] = useState('')
  const [steamId, setSteamId] = useState('')
  const [label, setLabel] = useState('Main')

  return (
    <main className="mx-auto grid min-h-dvh max-w-md content-center gap-6 px-6">
      <header className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t(`onboarding.${platform}.title`)}</h1>
        <p className="text-sm text-muted-foreground">{t(`onboarding.${platform}.intro`)}</p>
      </header>

      <div role="group" aria-label={t('onboarding.platform')} className="flex gap-2">
        {(['steam', 'epic'] as const).map((choice) => (
          <Button
            key={choice}
            type="button"
            variant={choice === platform ? 'default' : 'outline'}
            aria-pressed={choice === platform}
            onClick={() => {
              // What was typed for one platform means nothing to the other.
              setSecret('')
              connect.reset()
              setPlatform(choice)
            }}
          >
            {t(`onboarding.${choice}.name`)}
          </Button>
        ))}
      </div>

      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          connect.mutate(
            {
              provider: platform,
              // Epic's sign-in names the account; only Steam asks for it.
              external_account_id: platform === 'steam' ? steamId : '',
              label,
              credentials: secret,
            },
            {
              onSuccess: () => {
                // Dropped from state the moment the server has it. It is never
                // put in `localStorage`, never in the URL, and never rendered
                // back — the response carries a mask, not the secret (rule 7).
                setSecret('')
                void navigate('/library', { replace: true })
              },
            },
          )
        }}
      >
        {platform === 'steam' ? (
          <>
            <Field
              id="api-key"
              label={t('onboarding.apiKey')}
              hint={t('onboarding.apiKeyHint')}
              // `password`, so it is not shoulder-read and not offered to a
              // password manager as a username.
              type="password"
              autoComplete="off"
              value={secret}
              onChange={(event) => setSecret(event.target.value)}
            />
            <Field
              id="steam-id"
              label={t('onboarding.steamId')}
              hint={t('onboarding.steamIdHint')}
              inputMode="numeric"
              value={steamId}
              onChange={(event) => setSteamId(event.target.value)}
            />
          </>
        ) : (
          <>
            <ol className="grid list-decimal gap-1 pl-5 text-sm text-muted-foreground">
              <li>
                <a
                  href={EPIC_LOGIN_URL}
                  // Its own tab: the code it shows is pasted back here.
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary underline-offset-4 hover:underline"
                >
                  {t('onboarding.epic.signIn')}
                </a>
              </li>
              <li>{t('onboarding.epic.copyCode')}</li>
            </ol>
            <Field
              id="epic-code"
              label={t('onboarding.epic.code')}
              hint={t('onboarding.epic.codeHint')}
              type="password"
              autoComplete="off"
              value={secret}
              onChange={(event) => setSecret(event.target.value)}
            />
          </>
        )}
        <Field
          id="label"
          label={t('onboarding.label')}
          value={label}
          onChange={(event) => setLabel(event.target.value)}
        />
        {connect.isError ? <Notice>{connect.error.detail}</Notice> : null}
        <Button type="submit" disabled={connect.isPending}>
          {connect.isPending ? t(`onboarding.${platform}.working`) : t('onboarding.submit')}
        </Button>
      </form>
    </main>
  )
}
