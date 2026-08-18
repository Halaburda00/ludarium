import { QueryClient } from '@tanstack/react-query'

export function createClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // A self-hosted library changes when a sync runs, not on its own, so
        // refetching because a window regained focus is work nobody asked for.
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
    },
  })
}
