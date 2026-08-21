import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  // The shadcn primitives and `api-types.ts` are generated: editing a generated
  // file to satisfy a lint rule means losing the edit the next time it is
  // regenerated. `react-refresh/only-export-components` fires on every one of
  // the primitives, and `api-types.ts` is checked against its own generator by
  // `api-types.test.ts`, which is a stricter reading than any rule here.
  globalIgnores(['dist', 'src/components/ui', 'src/lib/api-types.ts']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
])
