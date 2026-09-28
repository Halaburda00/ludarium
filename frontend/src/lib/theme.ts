import { useEffect, useSyncExternalStore } from 'react'

/**
 * Light or dark, or whatever the system says (#55).
 *
 * The choice is the viewer's, so it lives in their browser rather than on the
 * server: two people on one instance may want different ones, and nothing is
 * lost if the browser forgets it — the page falls back to the system's. The
 * same key is read by the script in `index.html`, before anything renders, so
 * a dark page does not flash white on load.
 */
export type Theme = 'system' | 'light' | 'dark'

export const THEMES: readonly Theme[] = ['system', 'light', 'dark']
export const STORAGE_KEY = 'ludarium.theme'
const QUERY = '(prefers-color-scheme: dark)'

// One choice per page, however many pickers show it.
let current: Theme = storedTheme()
const listeners = new Set<() => void>()

export function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return THEMES.includes(value as Theme) ? (value as Theme) : 'system'
  } catch {
    // Storage refused — a private window, blocked site data. The system's
    // preference is still an answer.
    return 'system'
  }
}

export function setTheme(theme: Theme): void {
  try {
    if (theme === 'system') localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // Kept for this visit only; the next one follows the system again.
  }
  current = theme
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, () => current)
}

function prefersDark(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(QUERY).matches
}

/** Put the theme on the document: the class Tailwind's `dark:` reads, and the native controls' scheme. */
export function applyTheme(theme: Theme): void {
  const dark = theme === 'dark' || (theme === 'system' && prefersDark())
  const root = document.documentElement
  root.classList.toggle('dark', dark)
  root.style.colorScheme = dark ? 'dark' : 'light'
}

/**
 * Keep the document in step with the choice, and with the system while it is
 * followed. Once, at the root: every screen is themed, not only those with a
 * picker on them.
 */
export function useAppliedTheme(): void {
  const theme = useTheme()
  useEffect(() => {
    applyTheme(theme)
    if (theme !== 'system' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(QUERY)
    const follow = () => applyTheme('system')
    media.addEventListener('change', follow)
    return () => media.removeEventListener('change', follow)
  }, [theme])
}
