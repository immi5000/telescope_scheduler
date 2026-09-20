import { useState, type ReactNode } from 'react'

export function Panel({
  title,
  right,
  children,
  variant,
}: {
  title?: ReactNode
  right?: ReactNode
  children: ReactNode
  variant?: 'now'
}) {
  return (
    <section className={`panel${variant ? ` panel--${variant}` : ''}`}>
      {(title || right) && (
        <header className="panel__head">
          {typeof title === 'string' ? <h2>{title}</h2> : title}
          {right}
        </header>
      )}
      <div className="panel__body">{children}</div>
    </section>
  )
}

/**
 * Where explanations go. The panel states the answer; the reasoning, the
 * footnote and the secondary numbers sit behind this, one click away. A native
 * <details>, so it is keyboard- and screen-reader-correct for free.
 */
export function More({ label = 'Details', children }: { label?: string; children: ReactNode }) {
  return (
    <details className="more">
      <summary>{label}</summary>
      <div className="more__body">{children}</div>
    </details>
  )
}

/** A panel that starts closed. Used for things worth having but not worth
 *  spending sidebar height on once the night is under way. */
export function Collapsible({
  title,
  defaultOpen = false,
  children,
}: {
  title: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="panel">
      <button
        type="button"
        className="collapse"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <h2>{title}</h2>
        <span className="collapse__caret" aria-hidden>
          ›
        </span>
      </button>
      {open && (
        <div className="panel__body" style={{ marginTop: 9 }}>
          {children}
        </div>
      )}
    </section>
  )
}
