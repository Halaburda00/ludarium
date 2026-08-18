import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import Onboarding from '@/routes/Onboarding'
import { renderApp, stubFetch } from '@/test/render'

const KEY = '0123456789ABCDEF-not-a-real-key'
const STEAM_ID = '76561197960287930'

async function fillIn() {
  await userEvent.type(screen.getByLabelText('Steam Web API key'), KEY)
  await userEvent.type(screen.getByLabelText('SteamID64'), STEAM_ID)
  await userEvent.click(screen.getByRole('button', { name: 'Connect' }))
}

describe('onboarding', () => {
  it('sends the key once, to the endpoint that validates it before storing it', async () => {
    const calls = stubFetch({
      'POST /api/accounts': { status: 201, body: { id: 1, provider: 'steam' } },
    })
    renderApp(<Onboarding />)

    await fillIn()

    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0].body).toEqual({
      provider: 'steam',
      external_account_id: STEAM_ID,
      label: 'Main',
      credentials: KEY,
    })
  })

  it('shows the platform’s own reason inline rather than a generic failure', async () => {
    stubFetch({
      'POST /api/accounts': {
        status: 400,
        body: { detail: 'steam returned an empty response, which means the profile is private' },
      },
    })
    renderApp(<Onboarding />)

    await fillIn()

    // The backend distinguishes a wrong key from a private profile because the
    // fix is different; repeating that here is the whole value of showing it.
    expect(await screen.findByRole('alert')).toHaveTextContent('profile is private')
  })

  it('never writes the key to storage and never renders it back', async () => {
    stubFetch({ 'POST /api/accounts': { status: 201, body: { id: 1, credentials: '••••••••' } } })
    renderApp(<Onboarding />)

    await fillIn()

    await waitFor(() => expect(localStorage.length).toBe(0))
    expect(sessionStorage.length).toBe(0)
    expect(document.body.innerHTML).not.toContain(KEY)
    expect(document.cookie).not.toContain(KEY)
  })

  it('leaves a rejected key in the field, and only in the field', async () => {
    stubFetch({ 'POST /api/accounts': { status: 400, body: { detail: 'steam rejected the key' } } })
    renderApp(<Onboarding />)

    await fillIn()

    // Still in the input on purpose — a rejected key is usually a typo, and
    // clearing the field would make the user paste it again to fix one
    // character. "Never rendered back" is about the success path, where the
    // value is dropped and the response carries a mask instead.
    const field = screen.getByLabelText('Steam Web API key')
    expect(field).toHaveValue(KEY)
    // Nowhere else: the message is the backend's, which never quotes the
    // credential (rule 7), and nothing was persisted.
    expect(await screen.findByRole('alert')).not.toHaveTextContent(KEY)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })
})
