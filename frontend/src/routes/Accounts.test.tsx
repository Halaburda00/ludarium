import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { DemoContext } from '@/lib/demo'
import type { Account } from '@/lib/queries'
import Accounts from '@/routes/Accounts'
import { renderApp, stubFetch } from '@/test/render'

function account(overrides: Partial<Account> = {}): Account {
  return {
    id: 1,
    provider: 'steam',
    provider_name: 'Steam',
    external_account_id: '76561197960287930',
    label: 'Main',
    is_active: true,
    is_derived: false,
    created_at: '2026-10-01T09:00:00Z',
    last_success_at: '2026-10-07T18:00:00Z',
    status: 'success',
    last_error: null,
    error_kind: null,
    credentials: '••••••••',
    ...overrides,
  }
}

const FAILING = account({
  id: 2,
  provider: 'epic',
  provider_name: 'Epic Games',
  external_account_id: 'abc123',
  status: 'failed',
  last_error: 'epic has ended this sign-in',
  error_kind: 'credentials',
})
const IMPORTED = account({
  id: 3,
  provider: 'battlenet',
  provider_name: 'Battle.net',
  external_account_id: 'galaxy-1',
  label: 'From Galaxy',
  is_derived: true,
  credentials: null,
  status: 'failed',
  last_error: 'a report could not be read',
  error_kind: 'credentials',
})
const MANUAL = account({
  id: 4,
  provider: 'manual',
  provider_name: 'Manual entry',
  external_account_id: null,
  credentials: null,
})

function row(name: RegExp): HTMLElement {
  const heading = screen.getByRole('heading', { name })
  const item = heading.closest('li')
  if (!item) throw new Error('no row')
  return item
}

describe('accounts', () => {
  it('lists every account a library comes from, with its health', async () => {
    stubFetch({ 'GET /api/accounts': { body: [account(), FAILING, IMPORTED, MANUAL] } })
    renderApp(<Accounts />)

    expect(await screen.findByRole('heading', { name: /Steam/ })).toBeInTheDocument()
    // The manual entries' account is managed through its games.
    expect(screen.queryByRole('heading', { name: /Manual entry/ })).not.toBeInTheDocument()
    expect(within(row(/Steam/)).getByText('Working')).toBeInTheDocument()
    expect(within(row(/Epic Games/)).getByText('epic has ended this sign-in')).toBeInTheDocument()
    expect(within(row(/Battle.net/)).getByText('Imported')).toBeInTheDocument()
  })

  it('shows an account’s id only beside another account on the same platform', async () => {
    const second = account({ id: 5, external_account_id: '76561197960287931' })
    stubFetch({ 'GET /api/accounts': { body: [account(), second, FAILING] } })
    renderApp(<Accounts />)

    await screen.findByRole('heading', { name: /Epic Games/ })
    // Two Steam accounts, both called Main: the id is what tells them apart.
    expect(screen.getByText('76561197960287930')).toBeInTheDocument()
    expect(screen.getByText('76561197960287931')).toBeInTheDocument()
    // One Epic account: its id is kept to the title.
    expect(screen.queryByText('abc123')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Epic Games' })).toHaveAttribute('title', 'abc123')
  })

  it('offers to sign in again only where that is the fix', async () => {
    stubFetch({ 'GET /api/accounts': { body: [account(), FAILING, IMPORTED] } })
    renderApp(<Accounts />)

    await screen.findByRole('heading', { name: /Epic Games/ })
    // To the form for this account, not for the platform: with two accounts
    // on one platform, the form has to say which one it is repairing.
    expect(within(row(/Epic Games/)).getByRole('link', { name: 'Sign in again' })).toHaveAttribute(
      'href',
      '/onboarding?provider=epic&account=abc123&label=Main',
    )
    // An imported account has no sign-in, whatever its last failure says.
    expect(within(row(/Battle.net/)).queryByRole('link')).not.toBeInTheDocument()
    expect(within(row(/Steam/)).queryByRole('link')).not.toBeInTheDocument()
  })

  it('renames an account', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: [account()] },
      'PATCH /api/accounts/1': { body: account({ label: 'Family PC' }) },
    })
    renderApp(<Accounts />)

    const name = await screen.findByRole('textbox', { name: /Name of the Steam account/ })
    await userEvent.clear(name)
    await userEvent.type(name, 'Family PC')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls.some((call) => call.method === 'PATCH')).toBe(true))
    expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({ label: 'Family PC' })
  })

  it('switches an account off without touching anything else', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: [account()] },
      'PATCH /api/accounts/1': { body: account({ is_active: false }) },
      'GET /api/sync/runs': { body: { providers: [], runs: [], enriching: [] } },
    })
    renderApp(<Accounts />)

    await userEvent.click(await screen.findByRole('button', { name: 'Switch off Main on Steam' }))

    await waitFor(() => expect(calls.some((call) => call.method === 'PATCH')).toBe(true))
    expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({ is_active: false })
  })

  it('shows a switched-off account as off, and offers to switch it back on', async () => {
    stubFetch({ 'GET /api/accounts': { body: [account({ is_active: false, status: 'failed' })] } })
    renderApp(<Accounts />)

    expect(await screen.findByText('Off')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch on Main on Steam' })).toBeInTheDocument()
  })

  it('offers a demo nothing it would refuse', async () => {
    stubFetch({ 'GET /api/accounts': { body: [account(), FAILING] } })
    renderApp(
      <DemoContext value>
        <Accounts />
      </DemoContext>,
    )

    await screen.findByRole('heading', { name: /Steam/ })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Switch/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Sign in again' })).not.toBeInTheDocument()
  })
})
