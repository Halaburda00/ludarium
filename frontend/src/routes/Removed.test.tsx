import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Router } from '@/App'
import { missingKeys } from '@/i18n'
import type { Account, RemovedEntitlement } from '@/lib/queries'
import { renderApp, stubFetch } from '@/test/render'

const ACCOUNTS: Account[] = [
  {
    id: 1,
    provider: 'steam',
    label: 'Main',
    external_account_id: '76561197960287930',
    credentials: '••••',
    is_active: true,
    last_success_at: null,
    status: 'success',
    last_error: null,
    created_at: '2026-08-21T10:00:00Z',
    error_kind: null,
    provider_name: 'Steam',
    is_derived: false,
  },
]

const WITCHER: RemovedEntitlement = {
  id: 10,
  provider: 'steam',
  provider_name: 'Steam',
  account_label: 'Main',
  provider_title: 'The Witcher 3: Wild Hunt - Complete Edition',
  work_id: 7,
  work_title: 'The Witcher 3: Wild Hunt',
  removed_at: '2026-09-30T12:00:00Z',
  removed_by_run_id: 41,
}

describe('the removed view', () => {
  it('lists each removed copy with its platform and the day it went', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/removed': { body: [WITCHER] },
    })
    renderApp(<Router />, { route: '/removed' })

    const table = await screen.findByRole('table')
    const [, row] = within(table).getAllByRole('row')
    expect(row).toHaveTextContent('The Witcher 3: Wild Hunt')
    // The store's own name, where the work's differs: what to look for there.
    expect(row).toHaveTextContent('The Witcher 3: Wild Hunt - Complete Edition')
    expect(row).toHaveTextContent('Steam · Main')
    expect(row).toHaveTextContent('Sep 30, 2026')
    expect(missingKeys).toEqual([])
  })

  it('restores a copy in one click and lets it leave the list', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/removed': { body: [WITCHER] },
      'POST /api/entitlements/10/restore': { status: 204 },
    })
    renderApp(<Router />, { route: '/removed' })

    await userEvent.click(
      await screen.findByRole('button', { name: 'Restore The Witcher 3: Wild Hunt' }),
    )

    expect(calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
      '/api/entitlements/10/restore',
    ])
    // Asked again, so the list says what the server now holds.
    await screen.findByRole('table')
    expect(
      calls.filter((call) => call.path === '/api/entitlements/removed').length,
    ).toBeGreaterThan(1)
  })

  it('asks again after a refused restore, so a copy a sync brought back leaves the list', async () => {
    const routes = {
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/removed': { body: [WITCHER] },
      'POST /api/entitlements/10/restore': {
        status: 409,
        body: { detail: 'the copy is not removed' },
      },
    }
    stubFetch(routes)
    renderApp(<Router />, { route: '/removed' })
    const restore = await screen.findByRole('button', { name: 'Restore The Witcher 3: Wild Hunt' })
    // A sync listed it again meanwhile.
    routes['GET /api/entitlements/removed'] = { body: [] }

    await userEvent.click(restore)

    expect(
      await screen.findByText('Nothing has been removed from your accounts.'),
    ).toBeInTheDocument()
  })

  it('keeps every restore under way disabled, not only the latest', async () => {
    const PORTAL: RemovedEntitlement = {
      ...WITCHER,
      id: 11,
      provider_title: 'Portal 2',
      work_id: 8,
      work_title: 'Portal 2',
    }
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/removed': { body: [WITCHER, PORTAL] },
    })
    const answered = globalThis.fetch
    // Restores that never answer: both are still under way when looked at.
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === 'POST' ? new Promise(() => {}) : answered(input, init),
    )
    renderApp(<Router />, { route: '/removed' })
    const witcher = await screen.findByRole('button', {
      name: 'Restore The Witcher 3: Wild Hunt',
    })
    const portal = screen.getByRole('button', { name: 'Restore Portal 2' })

    await userEvent.click(witcher)
    await userEvent.click(portal)

    expect(witcher).toBeDisabled()
    expect(portal).toBeDisabled()
  })

  it('says so when nothing has been removed', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/removed': { body: [] },
    })
    renderApp(<Router />, { route: '/removed' })

    expect(
      await screen.findByText('Nothing has been removed from your accounts.'),
    ).toBeInTheDocument()
  })
})
