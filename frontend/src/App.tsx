import { useState } from 'react'

import { QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import RequireSession from '@/components/RequireSession'
import { createClient } from '@/lib/client'
import { useAppliedTheme } from '@/lib/theme'
import Accounts from '@/routes/Accounts'
import Library from '@/routes/Library'
import WorkPage from '@/routes/WorkPage'
import Login from '@/routes/Login'
import ManualEntryPage from '@/routes/ManualEntry'
import Onboarding from '@/routes/Onboarding'
import Removed from '@/routes/Removed'
import UpNext from '@/routes/UpNext'

export function Router() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireSession />}>
        <Route path="/onboarding" element={<Onboarding />} />
      </Route>
      <Route element={<RequireSession needsAccount />}>
        <Route path="/library" element={<Library />} />
        <Route path="/library/:workId" element={<WorkPage />} />
        <Route path="/removed" element={<Removed />} />
        <Route path="/queue" element={<UpNext />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/manual/new" element={<ManualEntryPage />} />
        <Route path="/manual/:entitlementId" element={<ManualEntryPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/library" replace />} />
    </Routes>
  )
}

export default function App() {
  // Created once. Built in the render body it would be a new cache on every
  // pass, which under StrictMode is visible immediately and in production is a
  // slow leak of everything already fetched.
  const [client] = useState(createClient)
  useAppliedTheme()
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Router />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
