import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import openapiTS, { astToString } from 'openapi-typescript'
import { describe, expect, it } from 'vitest'

const DOCUMENT = join(process.cwd(), '..', 'docs', 'openapi.json')
const GENERATED = join(process.cwd(), 'src', 'lib', 'api-types.ts')

const REGENERATE = [
  'the committed API types are not the ones `docs/openapi.json` describes — regenerate them:',
  '  cd frontend && pnpm run api:types',
  'and if the document itself is behind the backend, that first:',
  '  cd backend && uv run ludarium-openapi > ../docs/openapi.json',
].join('\n')

/**
 * The frontend half of #35: the types are the document's, not a transcription.
 *
 * The backend's `test_openapi_document` pins the document to the app, and this
 * pins the types to the document — so a renamed field cannot reach the UI
 * without one of the two going red. Run here rather than as a CI step, because
 * a check nobody can run before pushing is one that only ever fails late.
 */
describe('the generated API types', () => {
  it('are what the committed document generates', async () => {
    const ast = await openapiTS(JSON.parse(readFileSync(DOCUMENT, 'utf8')))
    const committed = readFileSync(GENERATED, 'utf8')

    // From the first declaration: the CLI writes a "do not edit" banner that
    // the Node API does not, and pinning the banner's wording here would make
    // this fail on an openapi-typescript upgrade that changed nothing else.
    const body = committed.slice(committed.indexOf('export interface paths'))

    expect(body, REGENERATE).toBe(astToString(ast))
  })
})
