/**
 * A server error, as a sentence a person can act on.
 *
 * FastAPI answers a bad request with a 422 whose `detail` is a list of
 * {loc, msg} records; the API client hands that on as its JSON text. Shown raw
 * it reads as `[{"type":"greater_than","loc":["body","snrGoal"],...}]`, so the
 * target picker and the setup sheet both pass it through here first.
 */

const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/** Field names for a 422's `loc`, so a message can say which input it means. */
const WIRE_NAMES: Record<string, string> = {
  date: 'Night of',
  start: 'Start',
  startHourUtc: 'Start',
  hours: 'Hours',
  slotMinutes: 'Slot length',
  snrGoal: 'SNR goal',
  tSubS: 'Sub-exposure',
  siteId: 'Site',
  equipment: 'Equipment',
  targets: 'Targets',
}

/** A FastAPI error body, as one readable line. */
export function detailText(detail: string): string {
  let items: unknown
  try {
    items = JSON.parse(detail)
  } catch {
    return capital(detail)
  }
  if (!Array.isArray(items)) return detail
  const lines = items.flatMap((it: unknown) => {
    if (typeof it !== 'object' || it === null) return []
    const { msg, loc } = it as { msg?: unknown; loc?: unknown }
    if (typeof msg !== 'string') return []
    const text = msg.replace(/^Value error,\s*/, '')
    const last: unknown = Array.isArray(loc) ? loc[loc.length - 1] : undefined
    const field = typeof last === 'string' ? (WIRE_NAMES[last] ?? last) : undefined
    return [field ? `${field}: ${text.charAt(0).toLowerCase()}${text.slice(1)}` : capital(text)]
  })
  return lines.length ? lines.join(' · ') : detail
}

/**
 * An error message whose origin is not known: a 422 body, a server's own
 * sentence, or a network failure's text. Only something that looks like JSON
 * is taken apart; anything else is already words, and is shown as it came.
 */
export function errorText(message: string): string {
  return /^\s*[[{]/.test(message) ? detailText(message) : message
}
