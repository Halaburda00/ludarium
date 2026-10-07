import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { EPIC_LOGIN_URL } from '@/lib/epic'
import { GOG_LOGIN_URL } from '@/lib/gog'
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

describe('connecting Epic', () => {
  it('sends Epic to its own sign-in and posts the code it shows', async () => {
    const calls = stubFetch({
      'POST /api/accounts': { status: 201, body: { id: 2, provider: 'epic' } },
    })
    renderApp(<Onboarding />)

    await userEvent.click(screen.getByRole('button', { name: 'Epic Games' }))
    const signIn = screen.getByRole('link', { name: 'Sign in on epicgames.com' })
    expect(signIn).toHaveAttribute('href', EPIC_LOGIN_URL)
    expect(signIn).toHaveAttribute('target', '_blank')
    await userEvent.type(screen.getByLabelText('Authorization code'), 'not-a-real-code')
    await userEvent.click(screen.getByRole('button', { name: 'Connect' }))

    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0].body).toEqual({
      provider: 'epic',
      external_account_id: '',
      label: 'Main',
      credentials: 'not-a-real-code',
    })
  })

  it('opens on Epic when a failed sync sent the user here to sign in again', () => {
    stubFetch({})
    renderApp(<Onboarding />, { route: '/onboarding?provider=epic' })

    expect(screen.getByRole('heading', { name: 'Connect Epic Games' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Epic Games' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})

describe('connecting GOG', () => {
  const PAGE = 'https://embed.gog.com/on_login_success?origin=client&code=not-a-real-code'

  it('sends GOG to its own sign-in and posts the address it ends on', async () => {
    const calls = stubFetch({
      'POST /api/accounts': { status: 201, body: { id: 3, provider: 'gog' } },
    })
    renderApp(<Onboarding />)

    await userEvent.click(screen.getByRole('button', { name: 'GOG' }))
    const signIn = screen.getByRole('link', { name: 'Sign in on gog.com' })
    expect(signIn).toHaveAttribute('href', GOG_LOGIN_URL)
    expect(signIn).toHaveAttribute('target', '_blank')
    await userEvent.type(screen.getByLabelText("Address of GOG's page"), PAGE)
    await userEvent.click(screen.getByRole('button', { name: 'Connect' }))

    await waitFor(() => expect(calls).toHaveLength(1))
    // The backend takes the code out of the address.
    expect(calls[0].body).toEqual({
      provider: 'gog',
      external_account_id: '',
      label: 'Main',
      credentials: PAGE,
    })
  })

  it('opens on GOG when a failed sync sent the user here to sign in again', () => {
    stubFetch({})
    renderApp(<Onboarding />, { route: '/onboarding?provider=gog' })

    expect(screen.getByRole('heading', { name: 'Connect GOG' })).toBeInTheDocument()
  })

  it('opens on Steam for a provider it does not know', () => {
    stubFetch({})
    renderApp(<Onboarding />, { route: '/onboarding?provider=nowhere' })

    expect(screen.getByRole('heading', { name: 'Connect Steam' })).toBeInTheDocument()
  })
})
