import { useState } from 'react'

import { QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import RequireSession from '@/components/RequireSession'
import { createClient } from '@/lib/client'
import Library from '@/routes/Library'
import Login from '@/routes/Login'
import Onboarding from '@/routes/Onboarding'

export function Router() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireSession />}>
        <Route path="/onboarding" element={<Onboarding />} />
      </Route>
      <Route element={<RequireSession needsAccount />}>
        <Route path="/library" element={<Library />} />
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
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Router />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
