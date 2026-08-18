import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import Login from '@/routes/Login'
import { renderApp, stubFetch } from '@/test/render'

describe('login', () => {
  it('sends the credentials and nothing else', async () => {
    const calls = stubFetch({ 'POST /api/auth/login': { body: { username: 'owner' } } })
    renderApp(<Login />)

    await userEvent.type(screen.getByLabelText('Username'), 'owner')
    await userEvent.type(screen.getByLabelText('Password'), 'correct horse')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toMatchObject({
      method: 'POST',
      path: '/api/auth/login',
      body: { username: 'owner', password: 'correct horse' },
    })
  })

  it('says so when the password is wrong, without saying which half was', async () => {
    stubFetch({
      'POST /api/auth/login': { status: 401, body: { detail: 'wrong username or password' } },
    })
    renderApp(<Login />)

    await userEvent.type(screen.getByLabelText('Username'), 'owner')
    await userEvent.type(screen.getByLabelText('Password'), 'hunter2')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username or password.')
  })

  it('keeps the password out of storage and out of the page', async () => {
    stubFetch({ 'POST /api/auth/login': { body: {} } })
    renderApp(<Login />)

    await userEvent.type(screen.getByLabelText('Username'), 'owner')
    await userEvent.type(screen.getByLabelText('Password'), 'correct horse')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(localStorage.length).toBe(0))
    expect(sessionStorage.length).toBe(0)
    expect(document.body.innerHTML).not.toContain('correct horse')
  })
})
