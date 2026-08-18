import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import en from '@/locales/en.json'

/** English is the default and the only bundled language; the rest arrive with M5. */
export const defaultLanguage = 'en'

// Exported so a test can assert every key a screen asks for actually exists.
export const missingKeys: string[] = []

void i18n.use(initReactI18next).init({
  lng: defaultLanguage,
  fallbackLng: defaultLanguage,
  resources: { en: { translation: en } },
  // React escapes for us; doing it twice turns an apostrophe into `&#39;`.
  interpolation: { escapeValue: false },
  saveMissing: true,
  missingKeyHandler: (_languages, _namespace, key) => {
    missingKeys.push(key)
  },
})

export default i18n
