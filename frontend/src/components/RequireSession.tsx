import { Navigate, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { DemoContext } from '@/lib/demo'
import { useAccounts, useHealth } from '@/lib/queries'

/**
 * The gate in front of every screen that needs a session.
 *
 * There is no client-side notion of "signed in" to consult: the cookie is
 * httpOnly and the frontend cannot read it. So the question is asked of the
 * server, and a 401 from a guarded endpoint is the answer — which means the
 * gate cannot drift out of step with what the backend actually allows.
 */
export default function RequireSession({ needsAccount }: { needsAccount?: boolean }) {
  const { t } = useTranslation()
  const accounts = useAccounts()
  const health = useHealth()

  // Waiting on the health check too, so a demo never draws the actions it
  // refuses for a moment before hiding them. A failed one is no demo: the
  // gate does not hold the library back over it.
  if (accounts.isPending || health.isPending) {
    return <p className="p-6 text-sm text-muted-foreground">{t('common.loading')}</p>
  }
  if (accounts.error?.status === 401) {
    return <Navigate to="/login" replace />
  }
  if (accounts.error) {
    return <p className="p-6 text-sm text-destructive">{t('error.offline')}</p>
  }
  // A first run has a session and no library to show yet. Sending them to the
  // empty grid would leave them looking for the button that is the whole point
  // of the screen they should have been on.
  if (needsAccount && accounts.data?.length === 0) {
    return <Navigate to="/onboarding" replace />
  }
  const demo = health.data?.demo ?? false
  return (
    <DemoContext value={demo}>
      {demo ? (
        <p role="status" className="border-b bg-muted px-6 py-2 text-center text-sm">
          {t('demo.banner')}
        </p>
      ) : null}
      <Outlet />
    </DemoContext>
  )
}
