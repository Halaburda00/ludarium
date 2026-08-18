import { describe, expect, it } from 'vitest'

import { api, ApiError } from '@/lib/api'
import { stubFetch } from '@/test/render'

describe('api', () => {
  it('sends the session cookie on every request', async () => {
    const calls = stubFetch({ 'GET /api/works': { body: {} } })

    await api('/api/works')

    // The cookie is httpOnly, so this is the whole of the auth handling on this
    // side — and without it every guarded endpoint answers 401 while the app
    // looks like it simply has no data.
    expect(calls[0].credentials).toBe('include')
  })

  it('reports the backend’s own reason rather than a status code', async () => {
    stubFetch({ 'POST /api/accounts': { status: 400, body: { detail: 'steam rejected the key' } } })

    await expect(api('/api/accounts', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 400,
      detail: 'steam rejected the key',
    })
  })

  it('falls back to the status text when the body is not ours', async () => {
    // A 502 from a reverse proxy is an HTML page. Putting that in the UI turns
    // a bad moment into an unreadable one.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('<html>Bad Gateway</html>', { status: 502 })),
    )

    const error = await api('/api/works').catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).detail).not.toContain('<html>')
  })

  it('accepts an empty answer, because logout gives one', async () => {
    stubFetch({ 'POST /api/auth/logout': { status: 204 } })

    await expect(api('/api/auth/logout', { method: 'POST' })).resolves.toBeUndefined()
  })
})
