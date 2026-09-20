/**
 * Transient notifications.
 *
 * Portalled to `document.body`, and the dev assertion below exists because the
 * reason that matters is easy to forget: a `filter`, `transform`,
 * `backdrop-filter` or `perspective` on any ancestor creates a containing
 * block, which silently re-anchors `position: fixed` to that element instead
 * of the viewport. The removed brightness slider did exactly this, and the
 * toasts drifted for it with nothing in the console.
 *
 * The timer pauses on hover AND while the document is hidden. A night-long
 * session WILL be backgrounded -- that is the normal case, not the edge case
 * -- and without the second pause the whole queue drains into an empty tab
 * while the observer is at the eyepiece.
 *
 * Every toast now fades after five seconds, whatever its severity, and says
 * what the update did to the SCHEDULE rather than only that something
 * happened. Criticals used to stay until dismissed; they no longer do,
 * because a stack of undismissed alerts over the sky covers the very thing
 * they are about, and the Activity log keeps all of them either way.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { CategoryBadge, catVar } from '../alerts/AlertViews'
import type { AlertCategory } from '../alerts/model'

export interface Toast {
  id: string
  title: string
  body?: string
  severity?: 'info' | 'warn' | 'critical'
  /** Drives the stripe, the tint and the badge. */
  category?: AlertCategory
  categoryLabel?: string
  /** Night time the alert is about, "01:40". */
  when?: string
  /** ms. Zero means it stays until dismissed. Defaults to TTL. */
  ttl?: number
  action?: { label: string; run: () => void }
}

const MAX_VISIBLE = 3
/** Older ones are dropped rather than queued: a toast that surfaces ten
 *  minutes after its moment is history, and the activity log has it. */
const MAX_QUEUED = 5
/** Every toast, every severity. Hovering and a hidden tab still hold it. */
const TTL = 5000
/** The fade-out, which must finish before the row leaves the DOM. */
const FADE_MS = 350

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = useCallback((t: Omit<Toast, 'id'> & { id?: string }) => {
    const id = t.id ?? `${t.title}-${toastSeq++}`
    setToasts((prev) =>
      prev.some((x) => x.id === id) ? prev : [...prev, { ...t, id }].slice(-MAX_QUEUED),
    )
  }, [])
  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])
  return { toasts, push, dismiss }
}

let toastSeq = 0

export function Toasts({
  toasts,
  onDismiss,
}: {
  toasts: Toast[]
  onDismiss: (id: string) => void
}) {
  const host = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!import.meta.env.DEV) return
    const el = host.current
    if (!el) return
    for (let n: HTMLElement | null = el.parentElement; n; n = n.parentElement) {
      const cs = getComputedStyle(n)
      const culprit = (['filter', 'transform', 'backdropFilter', 'perspective'] as const).find(
        (p) => cs[p] && cs[p] !== 'none',
      )
      if (culprit) {
        console.warn(
          `[toasts] <${n.tagName.toLowerCase()}> sets ${culprit}: ${cs[culprit]}. That makes ` +
            'it a containing block, so position:fixed inside it is relative to IT, not the ' +
            'viewport, and these toasts will be misplaced.',
        )
      }
    }
  }, [])

  const visible = toasts.slice(-MAX_VISIBLE)
  return createPortal(
    <div ref={host} className="toasts" role="status" aria-live="polite">
      {visible.map((t) => (
        <ToastRow key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>,
    document.body,
  )
}

function ToastRow({ toast, onDismiss }: { toast: Toast; onDismiss: (id: string) => void }) {
  const [paused, setPaused] = useState(false)
  const [leaving, setLeaving] = useState(false)
  const remaining = useRef(toast.ttl ?? TTL)
  const since = useRef(Date.now())

  // The fade runs from a state flag rather than from the timer that started
  // it, so that a re-render mid-fade -- a pointer entering, say -- cannot
  // cancel a removal that has already visibly begun.
  useEffect(() => {
    if (!leaving) return
    const t = setTimeout(() => onDismiss(toast.id), FADE_MS)
    return () => clearTimeout(t)
  }, [leaving, toast.id, onDismiss])

  useEffect(() => {
    if (leaving || remaining.current <= 0) return
    let timer: ReturnType<typeof setTimeout> | null = null

    const resume = () => {
      since.current = Date.now()
      timer = setTimeout(() => setLeaving(true), remaining.current)
    }
    const hold = () => {
      if (timer) clearTimeout(timer)
      timer = null
      remaining.current -= Date.now() - since.current
    }

    const onVisibility = () => (document.hidden || paused ? hold() : resume())
    document.addEventListener('visibilitychange', onVisibility)
    if (!document.hidden && !paused) resume()
    else hold()

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      if (timer) clearTimeout(timer)
    }
  }, [toast.id, paused, leaving])

  const severity = toast.severity ?? 'info'
  return (
    <div
      className={`toast toast--${severity}${leaving ? ' toast--leaving' : ''}`}
      style={toast.category ? catVar(toast.category) : undefined}
      role={severity === 'critical' ? 'alert' : undefined}
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
    >
      <div className="toast__head">
        {toast.category && <CategoryBadge category={toast.category} />}
        {severity !== 'info' && <span className={`sev sev--${severity}`}>{severity}</span>}
        {toast.when && <span className="toast__when num">{toast.when} UTC</span>}
        <button
          type="button"
          className="copy"
          style={toast.when ? undefined : { marginLeft: 'auto' }}
          onClick={() => onDismiss(toast.id)}
          aria-label="Dismiss"
        >
          &times;
        </button>
      </div>
      <div className="toast__title">{toast.title}</div>
      {toast.body && <div className="toast__body">{toast.body}</div>}
      {toast.action && (
        <button
          type="button"
          className="toast__action"
          onClick={() => {
            toast.action?.run()
            onDismiss(toast.id)
          }}
        >
          {toast.action.label}
        </button>
      )}
    </div>
  )
}
