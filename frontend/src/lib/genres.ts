import type { i18n as I18n } from 'i18next'

/**
 * A genre's name in the user's language where there is a translation, and
 * IGDB's English name where there is not. Keyed by slug, which IGDB keeps
 * stable while it rewords a name.
 *
 * Asked whether the key exists rather than given a default: most genres have
 * no key, on purpose, and a default would report each as a missing key.
 */
export function genreName(i18n: I18n, slug: string, name: string): string {
  const key = `genre.${slug}`
  return i18n.exists(key) ? i18n.t(key) : name
}
