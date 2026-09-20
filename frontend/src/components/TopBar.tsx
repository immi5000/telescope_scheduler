/**
 * The strip across the top of the sky: where you are, a search box, the
 * field of view, and the way out (a new night).
 *
 * Translucent and thin, so the sky runs underneath it -- the same restraint as
 * Stellarium Web's header, which is most of why that app feels like looking up
 * rather than looking at a dashboard.
 */

import { useEffect, useRef, useState } from 'react'
import type { Session } from '../api/client'
import type { SearchHit, SkySelection } from '../sky/scene'
import type { SkyApi } from '../sky/SkyScene'
import { useSkyView } from '../sky/viewStore'
import '../styles/sky-chrome.css'

export function TopBar({
  session,
  api,
  onNewNight,
  onChoose,
}: {
  session: Session | null
  api: SkyApi | null
  onNewNight: () => void
  onChoose: (sel: SkySelection) => void
}) {
  const { fovDeg } = useSkyView()
  return (
    <header className="topbar">
      <div className="topbar__brand">
        <svg viewBox="0 0 24 24" aria-hidden className="topbar__logo">
          <path d="M15.5 3.5a8.5 8.5 0 1 0 5 15.2A7 7 0 0 1 15.5 3.5z" fill="currentColor" />
          <path d="M2 21h20" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <span className="topbar__title">Traveling Telescope</span>
        {/*
          The session's own name, which the server builds from the LOCAL night
          ("Urbana, IL · night of 2026-09-17"). The grid's start is a UTC
          instant, and for any site west of Greenwich its date is the next day.
        */}
        {session && <span className="topbar__where">{session.name}</span>}
      </div>

      {session && api ? <Search api={api} onChoose={onChoose} /> : <span />}

      <div className="topbar__right">
        {session && <span className="topbar__fov num">FOV {fovDeg}°</span>}
        {session && (
          <button type="button" className="topbar__btn" onClick={onNewNight}>
            New night
          </button>
        )}
      </div>
    </header>
  )
}

function Search({ api, onChoose }: { api: SkyApi; onChoose: (sel: SkySelection) => void }) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const box = useRef<HTMLDivElement | null>(null)

  // Computed on every render, not cached per query: the sky's data (the plan,
  // the planets, tonight's satellites) arrives after the box can be typed in,
  // and a result list frozen at the first keystroke would stay empty. A search
  // over a few thousand names is well under a millisecond.
  const hits: SearchHit[] = open && q ? api.search(q) : []

  useEffect(() => setActive(0), [q])

  useEffect(() => {
    const close = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('pointerdown', close)
    return () => window.removeEventListener('pointerdown', close)
  }, [])

  const choose = (h: SearchHit | undefined) => {
    if (!h) return
    onChoose(h.selection)
    api.flyTo(h.selection, { follow: h.selection.kind === 'satellite' })
    setQ('')
    setOpen(false)
  }

  return (
    <div ref={box} className="search">
      <svg viewBox="0 0 24 24" aria-hidden className="search__icon">
        <circle cx="10.5" cy="10.5" r="6" fill="none" stroke="currentColor" strokeWidth="1.8" />
        <path d="M15 15l5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
      <input
        value={q}
        placeholder="Search targets, planets, stars, satellites…"
        aria-label="Search the sky"
        onChange={(e) => {
          setQ(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            setActive((a) => Math.min(hits.length - 1, a + 1))
            e.preventDefault()
          } else if (e.key === 'ArrowUp') {
            setActive((a) => Math.max(0, a - 1))
            e.preventDefault()
          } else if (e.key === 'Enter') {
            choose(hits[active])
          } else if (e.key === 'Escape') {
            setOpen(false)
          }
        }}
      />
      {open && q && (
        <ul className="search__list" role="listbox">
          {hits.length === 0 && <li className="search__empty">Nothing tonight by that name</li>}
          {hits.map((h, i) => (
            <li
              key={`${h.kind}-${h.label}-${i}`}
              role="option"
              aria-selected={i === active}
              className="search__item"
              onPointerDown={(e) => {
                e.preventDefault()
                choose(h)
              }}
            >
              <span>{h.label}</span>
              <span className="search__kind">{h.kind}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
