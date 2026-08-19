import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import Library from '@/routes/Library'
import { renderApp, stubFetch } from '@/test/render'

const EMPTY = { works: [], next_cursor: null }
const THREE = {
  works: [
    { id: 1, title: 'Dota 2', is_matched: false },
    { id: 2, title: 'Portal 2', is_matched: false },
    { id: 3, title: 'The Witcher 3: Wild Hunt', is_matched: false },
  ],
  next_cursor: null,
}

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    provider: 'steam',
    account_id: 1,
    status: 'success',
    items_seen: 3,
    items_added: 3,
    items_updated: 0,
    items_removed: 0,
    error_text: null,
    ...overrides,
  }
}

describe('library', () => {
  it('asks for a sync and reports what the run saw', async () => {
    const calls = stubFetch({
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: [run()] },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Synced 3 games.')
    expect(calls.some((call) => call.method === 'POST' && call.path === '/api/sync/steam')).toBe(
      true,
    )
  })

  it('reports a run that failed rather than pretending it worked', async () => {
    stubFetch({
      'GET /api/works': { body: EMPTY },
      'POST /api/sync/steam': {
        body: [run({ status: 'failed', items_seen: 0, error_text: 'steam did not answer' })],
      },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    // 200 with a failed run is the shape rule 4 gives this: the request
    // succeeded, the sync did not, and the screen has to say the second thing.
    expect(await screen.findByRole('alert')).toHaveTextContent('steam did not answer')
  })

  it('treats a 409 as news rather than as an error', async () => {
    stubFetch({
      'GET /api/works': { body: EMPTY },
      'POST /api/sync/steam': { status: 409, body: { detail: 'account 1 is already syncing' } },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    // Something is already doing the thing that was asked for, which is not a
    // failure to explain in the backend's words.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A sync is already running for this account.',
    )
  })

  it('says the library is empty rather than showing nothing at all', async () => {
    stubFetch({ 'GET /api/works': { body: EMPTY } })
    renderApp(<Library />)

    expect(await screen.findByText(/Nothing here yet/)).toBeInTheDocument()
  })

  it('lists what came back, with the count pluralised', async () => {
    stubFetch({ 'GET /api/works': { body: THREE } })
    renderApp(<Library />)

    expect(await screen.findByText('3 games')).toBeInTheDocument()
    expect(screen.getByText('The Witcher 3: Wild Hunt')).toBeInTheDocument()
  })

  it('signs out through the endpoint rather than by forgetting locally', async () => {
    const calls = stubFetch({
      'GET /api/works': { body: THREE },
      'POST /api/auth/logout': { status: 204 },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    // The cookie is httpOnly, so there is nothing local to forget: the session
    // ends because the server deleted the row. Where the user goes next is
    // `App.test.tsx`'s subject.
    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/auth/logout')).toBe(true),
    )
  })
})

describe('a partial run', () => {
  it('is not reported as a success', async () => {
    stubFetch({
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: [run({ status: 'partial', items_seen: 2 })] },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    // "Synced 2 games" over a partial run tells the user everything arrived.
    // Some of their library is missing and only the next run may fix it.
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('did not hand over all of it')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
