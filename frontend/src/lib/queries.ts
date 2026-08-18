import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { api, ApiError } from '@/lib/api'

export type Account = {
  id: number
  provider: string
  external_account_id: string | null
  label: string
  is_active: boolean
  created_at: string
  last_success_at: string | null
  credentials: string | null
}

export type SyncRun = {
  id: number
  provider: string
  account_id: number | null
  status: 'pending' | 'running' | 'success' | 'partial' | 'failed'
  items_seen: number
  items_added: number
  items_updated: number
  items_removed: number
  error_text: string | null
}

export type WorksPage = {
  works: { id: number; title: string; is_matched: boolean }[]
  next_cursor: string | null
}

export const accountsKey = ['accounts'] as const
export const worksKey = ['works'] as const

/**
 * The connected accounts, and the session probe in the same request.
 *
 * There is no `/api/auth/session` endpoint, and adding one would be a second
 * way to ask the same question: every guarded endpoint answers 401 without a
 * cookie, and this is the one the shell needs the answer to anyway.
 */
export function useAccounts(): UseQueryResult<Account[], ApiError> {
  return useQuery<Account[], ApiError>({
    queryKey: accountsKey,
    queryFn: () => api<Account[]>('/api/accounts'),
    // A 401 is an answer, not a network blip. Retrying it delays the redirect
    // to the login screen for no gain.
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

export function useLogin() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, { username: string; password: string }>({
    mutationFn: (credentials) =>
      api('/api/auth/login', { method: 'POST', body: credentials }),
    onSuccess: () => client.invalidateQueries(),
  })
}

export function useLogout() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, void>({
    mutationFn: () => api('/api/auth/logout', { method: 'POST' }),
    // Cleared rather than invalidated: refetching a library we are no longer
    // allowed to read would answer 401 and flash an error on the way out.
    onSuccess: () => client.clear(),
  })
}

export type Connection = {
  provider: string
  external_account_id: string
  label: string
  credentials: string
}

export function useConnect() {
  const client = useQueryClient()
  return useMutation<Account, ApiError, Connection>({
    mutationFn: (connection) => api<Account>('/api/accounts', { method: 'POST', body: connection }),
    onSuccess: () => client.invalidateQueries({ queryKey: accountsKey }),
  })
}

export function useSync() {
  const client = useQueryClient()
  return useMutation<SyncRun[], ApiError, string>({
    mutationFn: (provider) => api<SyncRun[]>(`/api/sync/${provider}`, { method: 'POST' }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: worksKey })
      await client.invalidateQueries({ queryKey: accountsKey })
    },
  })
}

export function useWorks(): UseQueryResult<WorksPage, ApiError> {
  return useQuery<WorksPage, ApiError>({
    queryKey: worksKey,
    queryFn: () => api<WorksPage>('/api/works'),
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}
