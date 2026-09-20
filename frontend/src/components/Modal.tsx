/**
 * The one dialog that interrupts.
 *
 * Everything else in this app reports: a toast slides in, a panel updates, and
 * you read it when you get to it. A refusal cannot work that way. You clicked
 * "Add to schedule", the night did NOT change, and a message you might miss
 * would leave you believing it did -- so this is the one place that takes the
 * page until it is acknowledged.
 *
 * Nothing else should use it. If a message can be missed without the observer
 * acting on a false belief, it is a toast.
 *
 * The markup follows the CSS contract in components.css exactly: `.modal__scrim`
 * IS the fixed, full-screen, centering container, and `.modal__sheet` is its
 * child. Rendering the scrim as a sibling of the sheet instead -- which is the
 * shape most modals in other codebases have -- leaves the sheet in the document
 * flow with the fixed scrim painted over it at z-index 60: a dark blur where
 * the reason should be, swallowing every click including the one on OK.
 */

import { useEffect, useId, useRef, type ReactNode } from 'react'

export function Modal({
  title,
  children,
  onClose,
  dismissLabel = 'OK',
}: {
  title: string
  children: ReactNode
  onClose: () => void
  dismissLabel?: string
}) {
  const id = useId()
  const sheet = useRef<HTMLDivElement | null>(null)
  const dismiss = useRef<HTMLButtonElement | null>(null)

  // Escape closes, and focus moves to the dismiss button so the dialog can be
  // read and closed from the keyboard without hunting for it. Focus goes back
  // to whatever opened it -- the Add button -- on the way out, when that
  // element is still on the page to take it.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    dismiss.current?.focus()
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
        return
      }
      // `aria-modal` tells a screen reader the rest of the page is out of
      // play; without this, Tab walks straight out into it and the claim is
      // false. The page behind is not made inert -- that would mean reaching
      // into markup this component does not own -- so the trap is the keyboard
      // half of the same promise.
      if (e.key !== 'Tab' || !sheet.current) return
      const stops = sheet.current.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      const first = stops[0]
      const last = stops[stops.length - 1]
      if (!first || !last) return
      const active = document.activeElement
      if (e.shiftKey && (active === first || !sheet.current.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !sheet.current.contains(active))) {
        e.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', key)
    return () => {
      window.removeEventListener('keydown', key)
      if (opener?.isConnected) opener.focus?.()
    }
  }, [onClose])

  return (
    // Only a click on the scrim ITSELF dismisses: a click that started inside
    // the sheet -- selecting the reason to copy it, say -- must not close the
    // thing being read.
    <div
      className="modal__scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={sheet}
        className="modal__sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
      >
        <h2 className="modal__title" id={id}>
          {title}
        </h2>
        <div className="modal__body">{children}</div>
        <div className="modal__actions">
          <button type="button" className="ghost" ref={dismiss} onClick={onClose}>
            {dismissLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
