import { createContext, useContext } from 'react'

/**
 * Whether the screens are showing a demo, which refuses every write
 * (ADR-0034). Provided by the session gate, so a screen rendered outside it
 * is never a demo and asks for nothing to find out.
 */
export const DemoContext = createContext(false)

export function useDemo(): boolean {
  return useContext(DemoContext)
}
