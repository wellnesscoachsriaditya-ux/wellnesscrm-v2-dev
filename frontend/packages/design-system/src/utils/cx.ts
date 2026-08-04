/**
 * Class-name composition.
 *
 * Deliberately six lines rather than a `clsx` dependency (NFR-078: every
 * dependency must be justifiable and debuggable by a solo developer). This is
 * the entire useful surface of that package for our purposes.
 */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
