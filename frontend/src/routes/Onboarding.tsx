import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Field, Notice } from '@/components/ui/field'
import { EPIC_LOGIN_URL } from '@/lib/epic'
import { GOG_LOGIN_URL } from '@/lib/gog'
import { useConnect } from '@/lib/queries'

const PLATFORMS = ['steam', 'epic', 'gog'] as const
type Platform = (typeof PLATFORMS)[number]

// The platforms connected by signing in on their own page and pasting back
// what it gives, rather than by typing a key.
const SIGN_IN: Partial<Record<Platform, string>> = { epic: EPIC_LOGIN_URL, gog: GOG_LOGIN_URL }

function isPlatform(value: string | null): value is Platform {
  return PLATFORMS.some((platform) => platform === value)
}

export default function Onboarding() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const connect = useConnect()
  // From the link a failed sync offers, so "sign in again" lands on the right
  // form rather than on Steam's.
  const [params] = useSearchParams()
  const asked = params.get('provider')
  const [platform, setPlatform] = useState<Platform>(isPlatform(asked) ? asked : 'steam')
  const signIn = SIGN_IN[platform]
  // The account being signed in again, from the accounts screen (#129).
  const repairing = params.get('account') || null
  const [secret, setSecret] = useState('')
  const [steamId, setSteamId] = useState(platform === 'steam' ? (repairing ?? '') : '')
  const [label, setLabel] = useState(params.get('label') || 'Main')
  // A sign-in that named another account than the one being repaired. It is
  // connected all the same, as its own account, and the user has to know.
  const [other, setOther] = useState(false)

  return (
    <main className="mx-auto grid min-h-dvh max-w-md content-center gap-6 px-6">
      <header className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t(`onboarding.${platform}.title`)}</h1>
        <p className="text-sm text-muted-foreground">{t(`onboarding.${platform}.intro`)}</p>
        {repairing ? (
          <p className="text-sm">
            {t('onboarding.repairing', { label: params.get('label') || label, id: repairing })}
          </p>
        ) : null}
      </header>

      <div role="group" aria-label={t('onboarding.platform')} className="flex gap-2">
        {PLATFORMS.map((choice) => (
          <Button
            key={choice}
            type="button"
            variant={choice === platform ? 'default' : 'outline'}
            aria-pressed={choice === platform}
            onClick={() => {
              // What was typed for one platform means nothing to the other.
              setSecret('')
              connect.reset()
              setOther(false)
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
              // A sign-in names the account; only Steam asks for it.
              external_account_id: platform === 'steam' ? steamId : '',
              label,
              credentials: secret,
            },
            {
              onSuccess: (connected) => {
                // Dropped from state the moment the server has it. It is never
                // put in `localStorage`, never in the URL, and never rendered
                // back — the response carries a mask, not the secret (rule 7).
                setSecret('')
                if (repairing && connected.external_account_id !== repairing) {
                  setOther(true)
                  return
                }
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
                  href={signIn}
                  // Its own tab: what it gives is pasted back here.
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary underline-offset-4 hover:underline"
                >
                  {t(`onboarding.${platform}.signIn`)}
                </a>
              </li>
              <li>{t(`onboarding.${platform}.copyCode`)}</li>
            </ol>
            <Field
              id="sign-in-code"
              label={t(`onboarding.${platform}.code`)}
              hint={t(`onboarding.${platform}.codeHint`)}
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
        {other ? (
          <Notice>
            {t('onboarding.otherAccount', { provider: t(`onboarding.${platform}.name`) })}
          </Notice>
        ) : null}
        <Button type="submit" disabled={connect.isPending}>
          {connect.isPending ? t(`onboarding.${platform}.working`) : t('onboarding.submit')}
        </Button>
      </form>
    </main>
  )
}
