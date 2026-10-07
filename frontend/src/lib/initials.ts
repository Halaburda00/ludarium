/**
 * A cover's stand-in: the first letter of the title's first two words that
 * start with a capital or a digit, so "Lanterns of Vael" is "LV" rather than
 * "LO". A title with no such word falls back to its first two words.
 */
export function initials(title: string): string {
  const words = title.split(/\s+/).filter((word) => /[\p{L}\p{N}]/u.test(word))
  const named = words.filter((word) => /^[^\p{L}\p{N}]*[\p{Lu}\p{N}]/u.test(word))
  return (named.length > 0 ? named : words)
    .slice(0, 2)
    .map((word) => (word.match(/[\p{L}\p{N}]/u)?.[0] ?? '').toLocaleUpperCase())
    .join('')
}
