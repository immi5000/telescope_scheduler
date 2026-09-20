/**
 * How an alert looks, wherever it appears: toast, Now card, activity log.
 *
 * Category is encoded three ways at once -- hue, glyph and a word -- so it
 * survives colour blindness, a dim screen and a glance. Severity is carried
 * separately by weight and texture (see alerts.css), never by the same hue.
 */

import type { CSSProperties, ReactElement } from 'react'
import '../styles/alerts.css'
import { CATEGORY, type Alert, type AlertCategory } from './model'

const s = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.7,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

const GLYPH: Record<AlertCategory, ReactElement> = {
  weather: <path d="M7 18h10a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.2 11.1 3.5 3.5 0 0 0 7 18z" {...s} />,
  transient: (
    <>
      <path d="M12 3v5M12 16v5M3 12h5M16 12h5" {...s} />
      <path d="M12 9.2l.9 1.9 1.9.9-1.9.9-.9 1.9-.9-1.9-1.9-.9 1.9-.9z" fill="currentColor" />
    </>
  ),
  satellite: (
    <>
      <path d="M9.5 9.5l5 5M8 13l3 3M13 8l3 3" {...s} />
      <path d="M4 7l3-3 4 4-3 3zM13 16l3-3 4 4-3 3z" {...s} />
    </>
  ),
  moon: <path d="M19 14.5A7.5 7.5 0 0 1 9.5 5a7.5 7.5 0 1 0 9.5 9.5z" {...s} />,
  horizon: (
    <>
      <path d="M3 18l5.5-7 3.5 4.5 2.5-3L21 18z" {...s} />
      <path d="M2 20.5h20" {...s} />
    </>
  ),
  exposure: (
    <>
      <rect x="7" y="4" width="12" height="12" rx="1.5" {...s} />
      <path d="M4.5 7.5V18.5a1 1 0 0 0 1 1H16.5" {...s} />
    </>
  ),
  plan: <path d="M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3M18 3v4h-4M6 21v-4h4" {...s} />,
}

export function AlertIcon({ category, size = 15 }: { category: AlertCategory; size?: number }) {
  return (
    <svg
      className="alert-icon"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden
      style={{ color: CATEGORY[category].color }}
    >
      {GLYPH[category]}
    </svg>
  )
}

/** Icon plus the word, tinted: the first thing read on every alert. */
export function CategoryBadge({ category }: { category: AlertCategory }) {
  return (
    <span className="cat-badge" style={catVar(category)}>
      <AlertIcon category={category} size={13} />
      {CATEGORY[category].label}
    </span>
  )
}

export const catVar = (category: AlertCategory): CSSProperties =>
  ({ '--cat': CATEGORY[category].color }) as CSSProperties

/**
 * One alert as a row. The title is always visible; what to DO about it is one
 * click away rather than a second paragraph under every row.
 */
export function AlertRow({ alert }: { alert: Alert }) {
  const head = (
    <>
      <AlertIcon category={alert.category} />
      <span className="alert-row__cat">{CATEGORY[alert.category].label}</span>
      <span className="alert-row__text">{alert.title}</span>
    </>
  )
  const cls = `alert-row alert-row--${alert.severity}`
  if (!alert.detail) {
    return (
      <div className={cls} style={catVar(alert.category)}>
        <div className="alert-row__head">{head}</div>
      </div>
    )
  }
  return (
    <details className={cls} style={catVar(alert.category)}>
      <summary className="alert-row__head">
        {head}
        <span className="alert-row__caret" aria-hidden>
          ›
        </span>
      </summary>
      <div className="alert-row__detail">{alert.detail}</div>
    </details>
  )
}
