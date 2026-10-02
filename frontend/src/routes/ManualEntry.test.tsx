import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Router } from '@/App'
import { missingKeys } from '@/i18n'
import type { Account, ManualEntry, SyncOverview, WorkDetail } from '@/lib/queries'
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
  },
]
const IDLE: SyncOverview = { providers: [], runs: [], enriching: [] }

const DISC: ManualEntry = {
  id: 12,
  work_id: 40,
  title: "Baldur's Gate II",
  store_label: 'Big box, CD',
  ownership_type: 'physical',
  item_kind: 'game',
  release_year: 2000,
}

const GATE: WorkDetail = {
  id: 40,
  title: "Baldur's Gate II",
  sort_title: "Baldur's Gate II",
  is_matched: false,
  item_kind: 'game',
  release_year: 2000,
  play_status: 'not_started',
  is_favourite: false,
  is_hidden: false,
  playtime_minutes: 0,
  last_played_at: null,
  metacritic: null,
  steam_reviews: null,
  cover: null,
  addon_count: 0,
  entitlements: [
    {
      id: 12,
      provider: 'manual',
      provider_name: 'Manual entry',
      provider_item_id: null,
      provider_title: "Baldur's Gate II",
      store_label: 'Big box, CD',
      playtime_minutes: null,
      store_url: null,
      kept: false,
    },
  ],
  summary: null,
  release_date: null,
  genres: [],
  companies: [],
  rating: null,
  notes: null,
  started_at: null,
  completed_at: null,
  addons: [],
  parent: null,
}

describe('adding a game by hand', () => {
  it('sends what was typed and opens the game it made', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'POST /api/entitlements/manual': { status: 201, body: DISC },
      'GET /api/works/40': { body: GATE },
    })
    renderApp(<Router />, { route: '/manual/new' })

    await userEvent.type(await screen.findByLabelText('Title'), "  Baldur's Gate II ")
    await userEvent.type(screen.getByLabelText('Where it is'), 'Big box, CD')
    await userEvent.type(screen.getByLabelText('Release year'), '2000')
    await userEvent.click(screen.getByRole('button', { name: 'Add to library' }))

    expect(
      await screen.findByRole('heading', { level: 1, name: "Baldur's Gate II" }),
    ).toBeInTheDocument()
    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
      title: "Baldur's Gate II",
      store_label: 'Big box, CD',
      // A disc is the likeliest reason to be here, so it is the default.
      ownership_type: 'physical',
      item_kind: 'game',
      release_year: 2000,
    })
    expect(missingKeys).toEqual([])
  })

  it('sends no year and no label when none were given', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'POST /api/entitlements/manual': { status: 201, body: DISC },
      'GET /api/works/40': { body: GATE },
    })
    renderApp(<Router />, { route: '/manual/new' })

    await userEvent.type(await screen.findByLabelText('Title'), 'Celeste')
    await userEvent.type(screen.getByLabelText('Where it is'), '   ')
    await userEvent.selectOptions(screen.getByLabelText('How you own it'), 'owned')
    await userEvent.click(screen.getByRole('button', { name: 'Add to library' }))

    await screen.findByRole('heading', { level: 1 })
    expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({
      store_label: null,
      ownership_type: 'owned',
      release_year: null,
    })
  })

  it('says why the server refused it', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'POST /api/entitlements/manual': { status: 422, body: { detail: 'not a year' } },
    })
    renderApp(<Router />, { route: '/manual/new' })

    await userEvent.type(await screen.findByLabelText('Title'), 'Celeste')
    await userEvent.click(screen.getByRole('button', { name: 'Add to library' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('not a year')
  })
})

describe('editing a game added by hand', () => {
  it('starts from what the user wrote and sends the whole entry back', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/manual/12': { body: DISC },
      'PUT /api/entitlements/manual/12': { body: { ...DISC, release_year: null } },
      'GET /api/works/40': { body: GATE },
    })
    renderApp(<Router />, { route: '/manual/12' })

    const year = await screen.findByLabelText('Release year')
    expect(screen.getByLabelText('Title')).toHaveValue("Baldur's Gate II")
    expect(screen.getByLabelText('Where it is')).toHaveValue('Big box, CD')
    expect(screen.getByLabelText('How you own it')).toHaveValue('physical')
    expect(year).toHaveValue(2000)
    await userEvent.clear(year)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByRole('heading', { level: 1, name: "Baldur's Gate II" })
    expect(calls.find((call) => call.method === 'PUT')?.body).toEqual({
      title: "Baldur's Gate II",
      store_label: 'Big box, CD',
      ownership_type: 'physical',
      item_kind: 'game',
      release_year: null,
    })
    expect(missingKeys).toEqual([])
  })

  it('deletes only once asked twice, then goes back to the library', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/manual/12': { body: DISC },
      'DELETE /api/entitlements/manual/12': { status: 204 },
      'GET /api/works': { body: { works: [], next_cursor: null } },
      'GET /api/sync/runs': { body: IDLE },
    })
    renderApp(<Router />, { route: '/manual/12' })

    await userEvent.click(await screen.findByRole('button', { name: 'Delete this game' }))
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)
    expect(screen.getByText(/cannot be restored/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Delete for good' }))

    expect(await screen.findByRole('heading', { level: 1, name: 'Library' })).toBeInTheDocument()
    expect(calls.filter((call) => call.method === 'DELETE').map((call) => call.path)).toEqual([
      '/api/entitlements/manual/12',
    ])
  })

  it('takes a delete back before it is sent', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/manual/12': { body: DISC },
    })
    renderApp(<Router />, { route: '/manual/12' })

    await userEvent.click(await screen.findByRole('button', { name: 'Delete this game' }))
    await userEvent.click(screen.getByRole('button', { name: 'Keep it' }))

    expect(screen.getByRole('button', { name: 'Delete this game' })).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)
  })

  it('says so for an entry that is not there', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/entitlements/manual/99': { status: 404, body: { detail: 'no such manual entry' } },
    })
    renderApp(<Router />, { route: '/manual/99' })

    expect(
      await screen.findByText('There is no game you added with this link.'),
    ).toBeInTheDocument()
  })
})

describe('a game added by hand on its page', () => {
  it('says where it is and links to its form', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/40': { body: GATE },
    })
    renderApp(<Router />, { route: '/library/40' })

    const row = (await screen.findByRole('table')).querySelector('tbody tr')
    expect(row).toHaveTextContent('Manual entry · Big box, CD')
    expect(screen.getByRole('link', { name: "Edit Baldur's Gate II" })).toHaveAttribute(
      'href',
      '/manual/12',
    )
    expect(missingKeys).toEqual([])
  })
})
