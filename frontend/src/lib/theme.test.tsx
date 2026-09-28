import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import '@/i18n'
import { ThemePicker } from '@/components/ThemePicker'
import { STORAGE_KEY, setTheme, storedTheme, useAppliedTheme } from '@/lib/theme'

/** A system preference jsdom does not have, and a way to change it. */
function system(dark: boolean) {
  const listeners = new Set<() => void>()
  const media = {
    matches: dark,
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  }
  vi.stubGlobal('matchMedia', () => media)
  return {
    turn(next: boolean) {
      media.matches = next
      listeners.forEach((listener) => listener())
    },
  }
}

function Themed() {
  useAppliedTheme()
  return <ThemePicker />
}

const isDark = () => document.documentElement.classList.contains('dark')

beforeEach(() => {
  setTheme('system')
  document.documentElement.className = ''
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the theme', () => {
  it('follows the system until told otherwise, and while it does, follows it live', () => {
    const preference = system(true)
    render(<Themed />)

    expect(isDark()).toBe(true)
    expect(document.documentElement.style.colorScheme).toBe('dark')
    preference.turn(false)
    expect(isDark()).toBe(false)
  })

  it('keeps an explicit choice across reloads, whatever the system says', async () => {
    const preference = system(false)
    render(<Themed />)

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Theme' }), 'Dark')

    expect(isDark()).toBe(true)
    expect(localStorage.getItem(STORAGE_KEY)).toBe('dark')
    expect(storedTheme()).toBe('dark')
    // An explicit choice ignores the system.
    preference.turn(false)
    expect(isDark()).toBe(true)
  })

  it('forgets the choice when the system is chosen again', async () => {
    system(false)
    localStorage.setItem(STORAGE_KEY, 'dark')
    render(<Themed />)

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Theme' }), 'System')

    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(isDark()).toBe(false)
  })

  it('reads anything it did not write as no choice at all', () => {
    localStorage.setItem(STORAGE_KEY, 'sepia')
    expect(storedTheme()).toBe('system')
  })

  it('is decided before the first paint by index.html, by the same key and rule', () => {
    // Otherwise a dark page flashes white while the bundle loads. The script is
    // run as the browser would run it, so a renamed key here fails there.
    const html = readFileSync(join(process.cwd(), 'index.html'), 'utf8')
    // Parsed, not cut out with a pattern: the one inline script, not the bundle's.
    const script = new DOMParser()
      .parseFromString(html, 'text/html')
      .querySelector('head script:not([src])')?.textContent
    expect(script).toBeTruthy()

    for (const [stored, prefers, expected] of [
      ['dark', false, true],
      ['light', true, false],
      [null, true, true],
      [null, false, false],
    ] as const) {
      system(prefers)
      if (stored === null) localStorage.removeItem(STORAGE_KEY)
      else localStorage.setItem(STORAGE_KEY, stored)
      document.documentElement.className = ''
      new Function(script ?? '')()
      expect(isDark()).toBe(expected)
    }
  })
})
