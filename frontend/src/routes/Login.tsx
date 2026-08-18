import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { Field, Notice } from '@/components/ui/field'
import { Button } from '@/components/ui/button'
import { useLogin } from '@/lib/queries'

export default function Login() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const login = useLogin()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  return (
    <main className="mx-auto grid min-h-dvh max-w-sm content-center gap-6 px-6">
      <header className="grid gap-1">
        <h1 className="font-heading text-2xl font-semibold">{t('app.name')}</h1>
        <p className="text-sm text-muted-foreground">{t('app.tagline')}</p>
      </header>

      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          login.mutate(
            { username, password },
            {
              onSuccess: () => {
                // Cleared on the way out. Nothing in this app writes a
                // credential anywhere it could outlive the request.
                setPassword('')
                void navigate('/library', { replace: true })
              },
            },
          )
        }}
      >
        <h2 className="sr-only">{t('login.title')}</h2>
        <Field
          id="username"
          label={t('login.username')}
          autoComplete="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
        />
        <Field
          id="password"
          label={t('login.password')}
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {login.isError ? <Notice>{t('login.failed')}</Notice> : null}
        <Button type="submit" disabled={login.isPending}>
          {login.isPending ? t('login.working') : t('login.submit')}
        </Button>
      </form>
    </main>
  )
}
