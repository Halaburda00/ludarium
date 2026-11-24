import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Router } from '@/App'
import { missingKeys } from '@/i18n'
import type { ImportPreview, ImportResult } from '@/lib/queries'
import { renderApp, stubFetch } from '@/test/render'

const PREVIEW: ImportPreview = {
  format: 'csv',
  encoding: 'windows-1250',
  delimiter: ';',
  columns: ['title', 'platform'],
  ignored_columns: ['notes'],
  rows_read: 3,
  problems: [],
  problem_count: 0,
  sample: [
    { row: 2, title: 'Wiedźmin 3', platform: 'Pudełko' },
    { row: 3, title: 'Mass Effect', platform: 'Origin' },
  ],
  groups: [
    {
      provider: 'other',
      provider_name: 'Other',
      label: 'Pudełko',
      items: 1,
      status: 'existing',
      account_id: 4,
      would_remove: 2,
    },
    {
      provider: 'ea',
      provider_name: 'EA app',
      label: 'Origin',
      items: 1,
      status: 'new',
      account_id: null,
      would_remove: 0,
    },
    {
      provider: 'steam',
      provider_name: 'Steam',
      label: 'Steam',
      items: 1,
      status: 'connected',
      account_id: null,
      would_remove: 0,
    },
  ],
  can_sweep: true,
}

const RESULT: ImportResult = {
  outcomes: [
    {
      provider: 'other',
      label: 'Pudełko',
      status: 'existing',
      detail: null,
      run: {
        id: 9,
        provider: 'manual',
        account_id: 4,
        trigger: 'import',
        status: 'success',
        started_at: '2026-10-09T10:00:00Z',
        finished_at: '2026-10-09T10:00:01Z',
        items_seen: 1,
        items_added: 0,
        items_updated: 1,
        items_removed: 2,
        items_skipped: 0,
        error_text: null,
        error_kind: null,
      },
    },
    { provider: 'steam', label: 'Steam', status: 'connected', detail: null, run: null },
  ],
}

const FILE = new File(['title;platform\nWiedźmin 3;Pudełko\n'], 'gry.csv', { type: 'text/csv' })

describe('importing a file', () => {
  it('shows where each row goes before anything is written', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: [] },
      'POST /api/import/preview': { body: PREVIEW },
    })
    renderApp(<Router />, { route: '/import' })

    await userEvent.upload(await screen.findByLabelText('CSV or JSON file'), FILE)

    expect(await screen.findByText('Wiedźmin 3')).toBeInTheDocument()
    expect(screen.getByText('Read 3 rows as CSV, windows-1250.', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Columns that are not used: notes.')).toBeInTheDocument()
    expect(screen.getByText('EA app · Origin')).toBeInTheDocument()
    expect(
      screen.getByText('1 game skipped: Steam syncs your library on its own'),
    ).toBeInTheDocument()
    // Only the two accounts that take the file count.
    expect(screen.getByRole('button', { name: 'Import 2 games' })).toBeEnabled()
    expect(calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
      '/api/import/preview',
    ])
    expect(missingKeys).toEqual([])
  })

  it('imports with removal only when asked, and says what happened', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: [] },
      'POST /api/import/preview': { body: PREVIEW },
      'POST /api/import': { body: RESULT },
    })
    renderApp(<Router />, { route: '/import' })

    await userEvent.upload(await screen.findByLabelText('CSV or JSON file'), FILE)
    expect(await screen.findByText(/Moves 2 copies to removed games/)).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText('Also remove what this file no longer lists'))
    await userEvent.click(screen.getByRole('button', { name: 'Import 2 games' }))

    expect(await screen.findByText(/0 added, 1 updated, 2 removed/)).toBeInTheDocument()
    expect(screen.getByText(/skipped, its platform syncs on its own/)).toBeInTheDocument()
    const sent = calls.find((call) => call.path === '/api/import')?.body
    expect(sent).toMatchObject({ sweep: 'true' })
  })

  it('offers no removal for a file with unread rows', async () => {
    stubFetch({
      'GET /api/accounts': { body: [] },
      'POST /api/import/preview': {
        body: {
          ...PREVIEW,
          problems: [{ row: 5, message: '`release_year` is 1949; expected 1950 to 2100' }],
          problem_count: 1,
          can_sweep: false,
        },
      },
    })
    renderApp(<Router />, { route: '/import' })

    await userEvent.upload(await screen.findByLabelText('CSV or JSON file'), FILE)

    expect(
      await screen.findByText('Row 5: `release_year` is 1949; expected 1950 to 2100'),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Also remove what this file no longer lists')).toBeDisabled()
  })

  it('says why a file was refused', async () => {
    stubFetch({
      'GET /api/accounts': { body: [] },
      'POST /api/import/preview': {
        status: 422,
        body: { detail: 'a `title` column is required' },
      },
    })
    renderApp(<Router />, { route: '/import' })

    await userEvent.upload(await screen.findByLabelText('CSV or JSON file'), FILE)

    expect(await screen.findByRole('alert')).toHaveTextContent('a `title` column is required')
  })
})
