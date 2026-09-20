/**
 * The night, as a control.
 *
 * Subtle on purpose: no legend, no border, no axis. The track is painted with
 * the session's own twilight bands, so the shape of the night IS the control
 * -- you can see dusk deepen and dawn come up without anything labelling them.
 *
 * Under it: a thin moon-altitude ribbon, a single-row strip of the plan's
 * blocks (all that survives of the old Gantt), one marker per update along the
 * bottom edge and one per alert along the top, each in its category's colour.
 *
 * The cursor and handle are moved by writing `transform` directly, outside
 * React, on the frame channel. The rest of this component re-renders only when
 * the plan or the session changes.
 *
 * On a night that is still happening, everything right of the live edge is
 * under a "not yet" veil. The plan's blocks and the predicted alerts show
 * through it, because a plan IS a statement about the future, but the cursor
 * cannot go there: the store clamps every seek to the edge, and a seek that
 * reaches it resumes following the present.
 */

import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { Plan, Session } from '../../api/client'
import { palette, rgba, toHex, twilightColor } from '../../theme/tokens'
import { cursor, useCursorMode } from '../../state/cursorStore'
import { fractionToMs, nightFraction, slotClock } from '../../time/slots'

export interface NightMarker {
  id: string
  tMs: number
  /** Decision points sit on the bottom edge and are what PageUp/PageDown step
   *  through; alerts sit on the top edge. */
  kind: 'decision' | 'alert'
  /** Any CSS colour, usually the alert category's `var()`. */
  color: string
  label: string
  /** Drawn hollow: an update that arrived and changed nothing. */
  quiet?: boolean
}

/** Markers closer than this on screen are collapsed into one. */
const CLUSTER_PX = 6

export function Nightbar({
  session,
  plan,
  markers,
  onSeek,
}: {
  session: Session
  plan: Plan | undefined
  markers: NightMarker[]
  onSeek: (tMs: number) => void
}) {
  const trackRef = useRef<HTMLDivElement | null>(null)
  const playheadRef = useRef<HTMLDivElement | null>(null)
  const tagRef = useRef<HTMLSpanElement | null>(null)
  const elapsedRef = useRef<HTMLDivElement | null>(null)
  const veilRef = useRef<HTMLDivElement | null>(null)
  const timeRef = useRef<HTMLSpanElement | null>(null)
  const clock = useMemo(() => slotClock(session.grid), [session.grid])
  const { playing, live, following } = useCursorMode()

  // -- the frame channel ---------------------------------------------------
  useEffect(() => {
    const move = () => {
      const f = nightFraction(clock, cursor.live.tMs)
      // The playhead layer is as wide as the track, so a translateX
      // percentage IS a fraction of the night. (A 2px-wide element translated
      // by a percentage moves by a percentage of 2px: the old cursor sat at
      // the left end all night, which is why nobody could tell where it was.)
      if (playheadRef.current) {
        playheadRef.current.style.transform = `translateX(${f * 100}%)`
        // Near the right end the time tag flips to the left of the line.
        playheadRef.current.classList.toggle('nightbar__playhead--flip', f > 0.9)
      }
      if (elapsedRef.current) elapsedRef.current.style.transform = `scaleX(${f})`
      if (tagRef.current) tagRef.current.textContent = `${new Date(cursor.live.tMs).toISOString().slice(11, 16)} UTC`
      if (timeRef.current) {
        timeRef.current.textContent = new Date(cursor.live.tMs).toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
        })
      }
    }
    move()
    return cursor.subscribeFrame(move)
  }, [clock])

  // -- the live edge: once a second, outside React --------------------------
  useEffect(() => {
    if (!live) return
    const move = () => {
      const f = nightFraction(clock, cursor.live.edgeMs)
      if (veilRef.current) veilRef.current.style.left = `${f * 100}%`
    }
    move()
    return cursor.subscribeEdge(move)
  }, [clock, live])

  // -- playback (replay only: the store never sets `playing` on a live night) --
  useEffect(() => {
    if (!playing) return
    let raf = 0
    let last = performance.now()
    const tick = (now: number) => {
      raf = requestAnimationFrame(tick)
      const dt = (now - last) / 1000
      last = now
      if (cursor.live.dragging) return
      const next = cursor.live.tMs + dt * 1000 * cursor.live.rate
      if (next >= clock.endMs) {
        cursor.setT(clock.endMs)
        cursor.setMode({ playing: false })
        return
      }
      cursor.setT(next)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing, clock])

  // -- pointer -------------------------------------------------------------
  const seekFromEvent = useCallback(
    (clientX: number) => {
      const el = trackRef.current
      if (!el) return
      const r = el.getBoundingClientRect()
      cursor.setT(fractionToMs(clock, (clientX - r.left) / Math.max(1, r.width)))
    },
    [clock],
  )

  useEffect(() => {
    const el = trackRef.current
    if (!el) return

    const down = (e: PointerEvent) => {
      // Capture on the STABLE wrapper, not on whatever child was under the
      // pointer: a marker or a block can be re-rendered mid-drag, and a
      // capture held by a removed node is silently released.
      el.setPointerCapture(e.pointerId)
      cursor.setMode({ dragging: true })
      seekFromEvent(e.clientX)
    }
    const move = (e: PointerEvent) => {
      // Deliberately NOT `e.buttons === 1`. Pen and touch report buttons
      // differently -- a stylus hovering reports 0 while genuinely dragging --
      // so the capture state is the thing to trust.
      if (!cursor.live.dragging) return
      seekFromEvent(e.clientX)
    }
    const up = (e: PointerEvent) => {
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
      cursor.setMode({ dragging: false })
    }

    el.addEventListener('pointerdown', down)
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
    // Fires when the browser takes the capture away -- a scroll gesture, a
    // context menu, the element being removed. Without it, `dragging` sticks
    // on and the cursor follows the pointer forever.
    el.addEventListener('lostpointercapture', up)
    return () => {
      el.removeEventListener('pointerdown', down)
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      el.removeEventListener('lostpointercapture', up)
    }
  }, [seekFromEvent])

  const onKeyDown = (e: React.KeyboardEvent) => {
    const jump = (dir: number) => {
      const here = cursor.live.tMs
      const pts = markers.filter((m) => m.kind === 'decision').map((m) => m.tMs)
      const next =
        dir > 0
          ? pts.find((t) => t > here + 1000)
          : [...pts].reverse().find((t) => t < here - 1000)
      if (next !== undefined) cursor.setT(next)
    }
    switch (e.key) {
      case 'ArrowLeft':
        cursor.nudgeSlots(e.shiftKey ? -12 : -1)
        break
      case 'ArrowRight':
        cursor.nudgeSlots(e.shiftKey ? 12 : 1)
        break
      case 'Home':
        cursor.setT(clock.startMs)
        break
      case 'End':
        // On a live night the end of what can be shown is now.
        cursor.setT(clock.endMs)
        break
      case 'PageUp':
        jump(-1)
        break
      case 'PageDown':
        jump(1)
        break
      case ' ':
        cursor.togglePlay()
        break
      default:
        return
    }
    e.preventDefault()
  }

  // -- static layers -------------------------------------------------------
  const twilight = useMemo(
    () =>
      session.twilight
        .map((b) => {
          const from = (b.fromSlot / session.grid.nSlots) * 100
          const to = (b.toSlot / session.grid.nSlots) * 100
          const c = toHex(twilightColor[b.kind] ?? palette.twilightNight)
          return `${c} ${from}%, ${c} ${to}%`
        })
        .join(', '),
    [session.twilight, session.grid.nSlots],
  )

  const moonRibbon = useMemo(() => {
    const alt = session.moon.altitudeDeg
    if (!alt.length) return ''
    return alt
      .map((a, i) => {
        const up = Math.max(0, Math.min(1, ((a ?? 0) + 5) / 65))
        const from = (i / alt.length) * 100
        const to = ((i + 1) / alt.length) * 100
        const c = rgba(palette.moon, up * 0.5)
        return `${c} ${from}%, ${c} ${to}%`
      })
      .join(', ')
  }, [session.moon.altitudeDeg])

  // Each edge clusters on its own, so an alert never swallows an update.
  const clustered = useMemo(
    () => [
      ...cluster(
        markers.filter((m) => m.kind === 'decision'),
        clock,
      ),
      ...cluster(
        markers.filter((m) => m.kind === 'alert'),
        clock,
      ),
    ],
    [markers, clock],
  )

  const slotPct = 100 / session.grid.nSlots

  return (
    <div className="nightbar">
      <div className="transport">
        {live ? (
          <button
            type="button"
            className="transport__live"
            aria-pressed={following}
            aria-label={following ? 'Pause at this moment' : 'Back to live'}
            title={following ? 'Live — pause here' : 'Back to live'}
            onClick={() => cursor.togglePlay()}
          >
            {following ? <span className="live-dot" aria-hidden /> : '⟳'}
          </button>
        ) : (
          <button
            type="button"
            aria-label={playing ? 'Pause' : 'Play the night'}
            onClick={() => cursor.togglePlay()}
          >
            {playing ? '❚❚' : '▶'}
          </button>
        )}
        <span ref={timeRef} className="nightbar__label num" style={{ minWidth: 46 }} />
      </div>

      <div
        ref={trackRef}
        className="nightbar__track"
        role="slider"
        tabIndex={0}
        aria-label={live ? 'Time through the night, up to now' : 'Time through the night'}
        aria-valuemin={clock.startMs}
        aria-valuemax={live ? Math.max(clock.startMs, cursor.live.edgeMs) : clock.endMs}
        aria-valuenow={cursor.live.tMs}
        aria-valuetext={new Date(cursor.live.tMs).toLocaleTimeString()}
        onKeyDown={onKeyDown}
        style={{ background: `linear-gradient(90deg, ${twilight})` }}
      >
        {/* The plan, one row high: the only remnant of the old Gantt. */}
        {plan?.blocks.map((b) => (
          <div
            key={`${b.targetId}-${b.slotStart}`}
            title={`${b.name} · ${b.startsAt.slice(11, 16)}–${b.endsAt.slice(11, 16)}`}
            style={{
              position: 'absolute',
              left: `${b.slotStart * slotPct}%`,
              width: `${(b.slotEnd - b.slotStart) * slotPct}%`,
              top: 9,
              height: 12,
              borderRadius: 3,
              background: rgba(palette.accent, 0.42),
              boxShadow: `inset 0 0 0 1px ${rgba(palette.accent, 0.55)}`,
            }}
          />
        ))}

        {moonRibbon && (
          <div
            aria-hidden
            style={{
              position: 'absolute',
              left: 0,
              right: 0,
              top: 25,
              height: 4,
              background: `linear-gradient(90deg, ${moonRibbon})`,
            }}
          />
        )}

        {clustered.map((m) => (
          <div
            key={m.id}
            className={[
              'nightbar__marker',
              m.kind === 'alert' ? 'nightbar__marker--alert' : '',
              m.quiet && m.count === 1 ? 'nightbar__marker--quiet' : '',
            ].join(' ')}
            title={m.label}
            onPointerDown={(e) => {
              e.stopPropagation()
              onSeek(m.tMs)
            }}
            style={{
              left: `${nightFraction(clock, m.tMs) * 100}%`,
              background: m.color,
              pointerEvents: 'auto',
              cursor: 'pointer',
              width: m.count > 1 ? 9 : 7,
              height: m.count > 1 ? 9 : 7,
            }}
          />
        ))}

        {live && (
          <div ref={veilRef} className="nightbar__veil" aria-hidden title="Not yet — this part of the night has not happened" />
        )}

        <div ref={elapsedRef} className="nightbar__elapsed" aria-hidden />
        <div ref={playheadRef} className="nightbar__playhead" aria-hidden>
          <div className="nightbar__playhead-line" />
          <div className="nightbar__playhead-knob" />
          <span ref={tagRef} className="nightbar__playhead-tag num" />
        </div>
      </div>

      <span className="nightbar__label">
        {session.grid.start.slice(11, 16)}&ndash;{session.grid.end.slice(11, 16)} UTC
      </span>
    </div>
  )
}

interface Clustered extends NightMarker {
  count: number
}

/**
 * Collapse markers that would overlap.
 *
 * A busy night has a forecast run every hour, an alert whenever one arrives
 * and a TLE batch on top; without this the marker row becomes a solid line
 * and stops carrying any information at all.
 *
 * Clustering is done in TIME using an assumed track width, not in pixels,
 * because the track has no measured width until after layout and a marker row
 * that reflows on every resize flickers.
 */
function cluster(markers: NightMarker[], clock: { startMs: number; endMs: number }): Clustered[] {
  const ASSUMED_TRACK_PX = 1100
  const span = clock.endMs - clock.startMs
  if (span <= 0) return []
  const minGapMs = (CLUSTER_PX / ASSUMED_TRACK_PX) * span

  const sorted = [...markers].sort((a, b) => a.tMs - b.tMs)
  const out: Clustered[] = []
  for (const m of sorted) {
    const last = out[out.length - 1]
    if (last && m.tMs - last.tMs < minGapMs) {
      last.count += 1
      // A cluster containing anything that moved is not quiet, and wears
      // that marker's colour.
      if (last.quiet && !m.quiet) last.color = m.color
      last.quiet = !!last.quiet && !!m.quiet
      last.label = `${last.count} ${m.kind === 'alert' ? 'alerts' : 'updates'} around ${new Date(
        last.tMs,
      ).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
      continue
    }
    out.push({ ...m, count: 1 })
  }
  return out
}
