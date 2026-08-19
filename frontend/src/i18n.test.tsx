import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import ts from 'typescript'

import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Router } from '@/App'
import { missingKeys } from '@/i18n'
import { renderApp, stubFetch } from '@/test/render'

const SOURCE = join(process.cwd(), 'src')

/** Everything under `src`, minus the generated primitives and the tests. */
function sources(directory = SOURCE): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      return entry.name === 'ui' || entry.name === 'test' || entry.name === 'assets'
        ? []
        : sources(path)
    }
    return entry.name.endsWith('.tsx') && !entry.name.includes('.test.') ? [path] : []
  })
}

/**
 * Text a browser would show, taken from the parse tree rather than guessed at.
 *
 * `JsxText` is exactly the node this rule is about — a sentence typed between
 * two tags. A regex cannot do it: `=>` and `length > 0` both look like a tag
 * closing onto text, and the first version of this test reported both.
 */
function literalText(source: string): string[] {
  const file = ts.createSourceFile('screen.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const found: string[] = []
  const visit = (node: ts.Node): void => {
    // Any letter at all: `OK`, `Hi` and `Go` are strings someone has to
    // translate as surely as a sentence is.
    if (ts.isJsxText(node) && /[A-Za-z]/.test(node.text)) {
      found.push(node.text.trim())
    }
    ts.forEachChild(node, visit)
  }
  visit(file)
  return found
}

describe('i18n', () => {
  it('has a resource for every key the screens ask for', async () => {
    // A page with a row on it and a cursor after it, so the table's own keys —
    // its columns, its caption, the link label and the button that asks for the
    // next page — are rendered rather than skipped by an empty library.
    stubFetch({
      'GET /api/accounts': { body: [{ id: 1, provider: 'steam', label: 'Main' }] },
      'GET /api/works': {
        body: {
          works: [
            {
              id: 1,
              title: 'Dota 2',
              sort_title: 'Dota 2',
              is_matched: false,
              item_kind: 'game',
              release_year: null,
              play_status: 'not_started',
              is_favourite: false,
              is_hidden: false,
              playtime_minutes: 0,
              last_played_at: null,
              entitlements: [
                {
                  id: 1,
                  provider: 'steam',
                  provider_name: 'Steam',
                  provider_item_id: '570',
                  provider_title: 'Dota 2',
                  playtime_minutes: 0,
                  store_url: 'https://store.steampowered.com/app/570',
                },
              ],
            },
          ],
          next_cursor: 'more',
        },
      },
    })
    renderApp(<Router />, { route: '/library' })
    await screen.findByRole('button', { name: 'Load more' })

    // A typo'd key renders as the key itself, which looks like a design choice
    // in a screenshot and like nothing at all in a review.
    expect(missingKeys).toEqual([])
  })

  it('has no hardcoded user-visible string in any screen', () => {
    const offenders = sources()
      .map((path) => [path, literalText(readFileSync(path, 'utf8'))] as const)
      .filter(([, text]) => text.length > 0)

    expect(offenders).toEqual([])
  })

  it('would notice one', () => {
    // The guard above passes on an empty set as readily as on a correct one,
    // so here is a screen with the mistake in it.
    expect(literalText('<p>Sign in</p>')).toEqual(['Sign in'])
    // Short ones too: `OK` and `Go` need translating as surely as a sentence.
    expect(literalText('<button>OK</button>')).toEqual(['OK'])
    expect(literalText('<p>{t("login.submit")}</p>')).toEqual([])
  })
})
