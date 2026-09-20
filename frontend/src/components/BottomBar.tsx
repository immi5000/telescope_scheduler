/**
 * The bar along the bottom of the sky, laid out the way Stellarium Web lays
 * out its own: where you are on the left, how you are looking in the middle,
 * when you are on the right.
 *
 * The middle holds only two options. Everything real is always drawn -- the
 * Milky Way, the stars, the constellation figures, planets, Moon, satellites,
 * ground, targets and slew path -- so there is nothing to curate, only two
 * ways to make the sky easier to read: pull the plan forward, and lay the
 * alt-az grid over it for "point ENE, 45 degrees up".
 *
 * Licence note, since provenance is taken seriously everywhere else here:
 * stellarium-web-engine is AGPL-3.0 and NONE of it is used -- not a line of
 * code, not an icon, not an asset. What is borrowed is the layout and the
 * interaction, which are ideas rather than expression.
 */

import { useEffect, useState } from 'react'
import type { Session } from '../api/client'
import type { SkyOptions } from '../sky/layers'
import { TimeBox } from './TimeBox'

const s = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.6 } as const
const sr = { ...s, strokeLinecap: 'round', strokeLinejoin: 'round' } as const

export function BottomBar({
  session,
  options,
  onChange,
  onZoom,
  panelsHidden,
  onTogglePanels,
}: {
  session: Session
  options: SkyOptions
  onChange: (next: SkyOptions) => void
  onZoom: (factor: number) => void
  panelsHidden: boolean
  onTogglePanels: () => void
}) {
  const [full, setFull] = useState(false)
  useEffect(() => {
    const sync = () => setFull(document.fullscreenElement !== null)
    document.addEventListener('fullscreenchange', sync)
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [])

  const site = session.site
  const lat = `${Math.abs(site.latitudeDeg).toFixed(2)}°${site.latitudeDeg >= 0 ? 'N' : 'S'}`
  const lon = `${Math.abs(site.longitudeDeg).toFixed(2)}°${site.longitudeDeg >= 0 ? 'E' : 'W'}`

  return (
    <div className="bottombar">
      <div className="bottombar__place" title={
          `${site.name} · ${lat} ${lon} · Bortle ${site.bortle}\n` +
          'The skyline is generated scenery, not this site\u2019s real horizon.'
        }>
        <svg viewBox="0 0 24 24" aria-hidden>
          <path d="M12 21s-6-5.6-6-11a6 6 0 0 1 12 0c0 5.4-6 11-6 11z" {...sr} />
          <circle cx="12" cy="10" r="2.2" fill="currentColor" />
        </svg>
        <span>{site.name}</span>
        <span className="bottombar__coords num">
          {lat} {lon}
        </span>
      </div>

      <div className="bottombar__tools" role="group" aria-label="View">
        <button
          type="button"
          className="tool"
          aria-pressed={options.focus}
          title="Focus on the plan — fade the Milky Way, constellations, faint stars and satellites so the targets and slew path stand out"
          onClick={() => onChange({ ...options, focus: !options.focus })}
        >
          <svg viewBox="0 0 24 24" aria-hidden>
            <circle cx="12" cy="12" r="7.5" {...s} />
            <circle cx="12" cy="12" r="2.4" fill="currentColor" />
            <path d="M12 1.5v4M12 18.5v4M1.5 12h4M18.5 12h4" {...sr} />
          </svg>
          <span>Focus on plan</span>
        </button>
        <button
          type="button"
          className="tool"
          aria-pressed={options.grid}
          title="Alt-azimuth grid — altitude every 15°, azimuth every 30°, and the altitude floor"
          onClick={() => onChange({ ...options, grid: !options.grid })}
        >
          <svg viewBox="0 0 24 24" aria-hidden>
            <path d="M2 19h20" {...sr} />
            <path d="M12 19V5M4.5 19a7.5 14 0 0 1 15 0" {...s} />
            <path d="M3.6 14.5h16.8M6 10h12" {...s} />
          </svg>
          <span>Grid</span>
        </button>
        <button
          type="button"
          className="tool"
          aria-pressed={panelsHidden}
          title={
            panelsHidden
              ? 'Show the side panels again'
              : 'Fade the side panels to a hint, so the sky behind them can be seen. They stop taking clicks while faded.'
          }
          onClick={onTogglePanels}
        >
          {/* The two side docks, as they sit on the sky. Under 900px the label
              is hidden and this carries the button on its own, so it has to read
              as "the things down either side" rather than as a generic panel. */}
          <svg viewBox="0 0 24 24" aria-hidden>
            <rect x="2.5" y="4.5" width="5.5" height="15" rx="1.4" {...s} />
            <rect x="16" y="4.5" width="5.5" height="15" rx="1.4" {...s} />
            <path d="M11 9.5h2M11 14.5h2" {...sr} />
          </svg>
          <span>{panelsHidden ? 'Show components' : 'Hide components'}</span>
        </button>
        <span className="bottombar__sep" aria-hidden />
        <button type="button" className="tool tool--icon" title="Zoom out" aria-label="Zoom out" onClick={() => onZoom(0.6)}>
          <svg viewBox="0 0 24 24" aria-hidden>
            <path d="M6 12h12" {...sr} />
          </svg>
        </button>
        <button type="button" className="tool tool--icon" title="Zoom in" aria-label="Zoom in" onClick={() => onZoom(1.66)}>
          <svg viewBox="0 0 24 24" aria-hidden>
            <path d="M6 12h12M12 6v12" {...sr} />
          </svg>
        </button>
        <button
          type="button"
          className="tool tool--icon"
          aria-pressed={full}
          title={full ? 'Leave full screen' : 'Full screen'}
          aria-label="Full screen"
          onClick={() => {
            if (document.fullscreenElement) void document.exitFullscreen()
            else void document.documentElement.requestFullscreen?.()
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden>
            <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" {...sr} />
          </svg>
        </button>
      </div>

      <TimeBox session={session} />
    </div>
  )
}
