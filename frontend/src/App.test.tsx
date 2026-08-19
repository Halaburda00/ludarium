import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Router } from '@/App'
import { renderApp, stubFetch } from '@/test/render'

const ACCOUNT = { id: 1, provider: 'steam', label: 'Main', credentials: '••••••••' }
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
      'GET /api/works': { body: { works: [], next_cursor: null } },
    })
    renderApp(<Router />, { route: '/library' })

    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument()
  })

  it('leaves an unknown path at the library rather than at nothing', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: { works: [], next_cursor: null } },
    })
    renderApp(<Router />, { route: '/somewhere-else' })

    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument()
  })

  it('returns to the login screen after signing out', async () => {
    stubFetch({
      'GET /api/accounts': { body: [ACCOUNT] },
      'GET /api/works': { body: { works: [], next_cursor: null } },
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
