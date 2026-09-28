import { useTranslation } from 'react-i18next'

import { setTheme, THEMES, useTheme, type Theme } from '@/lib/theme'

/**
 * A native select rather than a toggle: three states are not a switch, and a
 * select is labelled, keyboard-operable and announced as what it is for free.
 */
export function ThemePicker() {
  const { t } = useTranslation()
  const theme = useTheme()
  return (
    <label className="flex items-center gap-2 text-sm text-muted-foreground">
      {t('theme.label')}
      <select
        value={theme}
        onChange={(event) => setTheme(event.target.value as Theme)}
        className="h-8 rounded-lg border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        {THEMES.map((option) => (
          <option key={option} value={option}>
            {t(`theme.${option}`)}
          </option>
        ))}
      </select>
    </label>
  )
}
