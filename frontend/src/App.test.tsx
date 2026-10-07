import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { MemoryRouter } from 'react-router-dom'

import { Router } from '@/App'
import { createClient } from '@/lib/client'
import type { Account, Health, WorksPage } from '@/lib/queries'
import { renderApp, stubFetch } from '@/test/render'

// The whole response, not the two fields the gate reads: a stub that answers
// less than the API does is a fixture that can drift away from it (#35).
const ACCOUNT: Account = {
  id: 1,
  provider: 'steam',
  external_account_id: '76561197960287930',
  label: 'Main',
  is_active: true,
  created_at: '2026-08-01T09:00:00Z',
  last_success_at: null,
  status: 'success',
  last_error: null,
  credentials: '••••••••',
  error_kind: null,
  provider_name: 'Steam',
  is_derived: false,
}
const EMPTY_LIBRARY: WorksPage = { works: [], next_cursor: null }
const UNAUTHORISED = { status: 401, body: { detail: 'not signed in' } }

describe('routing', () => {
  it('sends an unauthenticated visitor to the login screen', async () => {
    stubFetch({ 'GET /api/accounts': UNAUTHORISED })
    renderApp(<Router />, { route: '/library' })

    // No client-side notion of "signed in" to consult — the cookie is httpOnly.
    // The gate asks a guarded endpoint, so it cannot drift out of step with
    // what the backend actually allows.
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('sends a signed-in visitor with no account to onboarding', async () => {
    stubFetch({ 'GET /api/accounts': { body: [] } })
    renderApp(<Router />, { route: '/library' })

    // The empty grid would leave a first-run user hunting for the button that
    // is the entire point of the screen they should have been on.
    expect(await screen.findByRole('heading', { name: 'Connect Steam' })).toBeInTheDocument()
  })

  it('lets a connected visitor through to the library', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: EMPTY_LIBRARY },
    })
    renderApp(<Router />, { route: '/library' })

    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument()
  })

  it('leaves an unknown path at the library rather than at nothing', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: EMPTY_LIBRARY },
    })
    renderApp(<Router />, { route: '/somewhere-else' })

    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument()
  })

  it('shows a demo as one, without the actions it would refuse', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: EMPTY_LIBRARY },
      'GET /api/health': {
        body: { status: 'ok', version: '0.0.0', database: true, demo: true } satisfies Health,
      },
    })
    renderApp(<Router />, { route: '/library' })

    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('This is a demo')
    for (const action of ['Sync now', 'Sign out']) {
      expect(screen.queryByRole('button', { name: action })).not.toBeInTheDocument()
    }
    for (const action of ['Connect an account', 'Add a game']) {
      expect(screen.queryByRole('link', { name: action })).not.toBeInTheDocument()
    }
    // Reading is what a demo is for.
    expect(screen.getByRole('link', { name: 'Up next' })).toBeInTheDocument()
  })

  it('does not hold the library back over a failed health check', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: EMPTY_LIBRARY },
      'GET /api/health': { status: 503, body: { detail: 'down' } },
    })
    // The library's own client, not the test one: its defaults retry a failed
    // query three times with backoff, which `renderApp` turns off.
    render(
      <QueryClientProvider client={createClient()}>
        <MemoryRouter initialEntries={['/library']}>
          <Router />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument()
  })

  it('offers an ordinary instance every action', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: EMPTY_LIBRARY },
    })
    renderApp(<Router />, { route: '/library' })

    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    expect(screen.queryByText(/This is a demo/)).not.toBeInTheDocument()
  })

  it('returns to the login screen after signing out', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: EMPTY_LIBRARY },
      'POST /api/auth/logout': { status: 204 },
    })
    renderApp(<Router />, { route: '/library' })

    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('says the server is unreachable rather than sending anyone to login', async () => {
    stubFetch({ 'GET /api/accounts': { status: 503, body: { detail: 'down' } } })
    renderApp(<Router />, { route: '/library' })

    // A 503 is not a 401. Redirecting on it would tell the user their session
    // expired, and they would spend the outage typing a password that works.
    expect(await screen.findByText('Could not reach the server.')).toBeInTheDocument()
  })
})
