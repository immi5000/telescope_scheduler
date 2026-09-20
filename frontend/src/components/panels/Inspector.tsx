/**
 * Click an object, see what it is — after Stellarium's info panel, but
 * answering a scheduler's question rather than a stargazer's.
 *
 * Stellarium tells you magnitude, distance and coordinates. Those are here,
 * but the thing an observer actually wants at 2 a.m. is narrower: **is this
 * worth pointing at tonight, and when.** So the panel leads with the
 * visibility bar — when it is up, when it clears the altitude floor, when it
 * transits — and puts the catalogue data underneath.
 *
 * The distinction the bar draws, between "above the horizon" and "actually
 * usable", is the whole point. A target can be up for six hours and
 * schedulable for none of them, because it never clears 30° or because the
 * Moon is sitting next to it, and no single rise/set pair can say that.
 */

import { useState } from 'react'
import type { GeometryRow, Plan, QualityGrid, Session, Target } from '../../api/client'
import { pointingAt, compassPoint } from '../../sky/pointing'
import { slotClock } from '../../time/slots'
import { More, Panel } from '../Panel'

export function Inspector({
  session,
  geometryRow,
  target,
  plan,
  grid,
  slot,
  onClose,
}: {
  session: Session
  geometryRow: GeometryRow
  target: Target | undefined
  plan: Plan | undefined
  grid: QualityGrid | undefined
  slot: number
  onClose: () => void
}) {
  const clock = slotClock(session.grid)
  const now = pointingAt(clock, geometryRow.altitudeDeg, geometryRow.azimuthDeg, clock.startMs)
  // Displayed numbers come from the backend's own slot value, never from the
  // interpolation used for drawing -- see time/slots.ts.
  const alt = geometryRow.altitudeDeg[slot] ?? 0
  const az = geometryRow.azimuthDeg[slot] ?? 0
  const moonSep = geometryRow.moonSeparationDeg[slot] ?? 0
  const usableNow = geometryRow.visible[slot] ?? false
  const vetoed = moonSep < session.site.minMoonSeparationDeg
  void now

  const row = grid?.rows.find((r) => r.targetId === geometryRow.targetId)
  const eta = row?.efficiency[slot] ?? 0
  const pref = row?.preference[slot] ?? 0
  const progress = plan?.progress.find((p) => p.targetId === geometryRow.targetId)

  return (
    <Panel
      title={geometryRow.name}
      right={
        <button type="button" className="copy" onClick={onClose} aria-label="Close">
          &times;
        </button>
      }
    >
      <Thumbnail row={geometryRow} />

      <VisibilityBar session={session} target={target} slot={slot} />

      <dl className="kv">
        <dt>Position</dt>
        <dd>
          {alt.toFixed(1)}&deg; up, {compassPoint(az)}
        </dd>
        <dt>Moon</dt>
        <dd style={vetoed ? { color: 'var(--warn)' } : undefined}>
          {moonSep.toFixed(0)}&deg; away{vetoed ? ' · vetoed' : ''}
        </dd>
        <dt>Schedulable now</dt>
        <dd style={{ color: usableNow ? 'var(--accent)' : 'var(--ink-faint)' }}>
          {usableNow ? 'yes' : 'no'}
        </dd>
      </dl>

      <Bar label="Efficiency" value={eta} color="var(--accent)" />
      <Bar label="Preference" value={pref} color="var(--moon)" />

      {progress && target && (
        <div className="faint" style={{ fontSize: 11.5 }}>
          {progress.included ? 'Scheduled tonight' : 'Not scheduled tonight'}
        </div>
      )}

      <More>
        <dl className="kv">
          <dt>RA</dt>
          <dd className="mono">{target?.ra ?? geometryRow.raDeg.toFixed(4)}</dd>
          <dt>Dec</dt>
          <dd className="mono">{target?.dec ?? geometryRow.decDeg.toFixed(4)}</dd>
          <dt>Azimuth</dt>
          <dd>{az.toFixed(0)}&deg;</dd>
          {target && (
            <>
              <dt>Surface brightness</dt>
              <dd>{target.magnitude.toFixed(1)} mag/&#9633;&Prime;</dd>
              <dt>Priority</dt>
              <dd>
                {target.priority.toFixed(1)} &times; urgency {target.urgency.toFixed(1)}
              </dd>
            </>
          )}
        </dl>
        {vetoed && (
          <div>
            Inside the {session.site.minMoonSeparationDeg.toFixed(0)}&deg; lunar exclusion, so it
            cannot be scheduled while the Moon is this close.
          </div>
        )}
        <div>
          <strong style={{ color: 'var(--ink)' }}>Efficiency</strong> is what the sky is worth
          here. <strong style={{ color: 'var(--ink)' }}>Preference</strong> is how much we want
          it. They are never multiplied together.
        </div>
      </More>
    </Panel>
  )
}

function Bar({ label, value, color }: { label: string; value: number; color: string }) {
  // Efficiency legitimately exceeds 1 when conditions beat the reference, so
  // the bar is scaled to 1.5 rather than clamped at 1 — clamping would hide
  // exactly the slots worth knowing about.
  const pct = Math.max(0, Math.min(100, (value / 1.5) * 100))
  return (
    <div>
      <div className="row spread" style={{ fontSize: 11 }}>
        <span className="dim">{label}</span>
        <span className="num faint">{value.toFixed(2)}</span>
      </div>
      <div className="meter">
        <div className="meter__fill" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  )
}

/**
 * When it is up, and when it is actually usable.
 *
 * Two bands over the same night: the pale one is above the true horizon, the
 * bright one clears the altitude floor AND sits outside the lunar exclusion.
 * The gap between them is the answer to "why is this not scheduled".
 */
function VisibilityBar({
  session,
  target,
  slot,
}: {
  session: Session
  target: Target | undefined
  slot: number
}) {
  if (!target) return null
  const n = session.grid.nSlots
  const pct = (i: number | null | undefined, fallback: number) =>
    (((i ?? fallback) / n) * 100).toFixed(2) + '%'

  const up = target.riseSlot !== null || target.setSlot !== null || target.visibleSlots > 0
  const start = session.grid.start

  const at = (i: number | null | undefined): string => {
    if (i === null || i === undefined) return '—'
    return new Date(Date.parse(start) + i * session.grid.slotMinutes * 60_000)
      .toISOString()
      .slice(11, 16)
  }

  return (
    <div>
      <div className="inspect__track" aria-hidden>
        {up && (
          <div
            className="inspect__span"
            style={{
              left: pct(target.riseSlot, 0),
              width: `calc(${pct(target.setSlot, n)} - ${pct(target.riseSlot, 0)})`,
              background: 'var(--line-soft)',
              top: 0,
              bottom: 0,
            }}
          />
        )}
        {target.firstUsableSlot !== null && (
          <div
            className="inspect__span"
            style={{
              left: pct(target.firstUsableSlot, 0),
              width: `calc(${pct(
                target.lastUsableSlot === null ? n : target.lastUsableSlot + 1,
                n,
              )} - ${pct(target.firstUsableSlot, 0)})`,
              background: 'var(--accent-deep)',
              top: 5,
              bottom: 5,
              borderRadius: 3,
            }}
          />
        )}
        {target.transitSlot !== null && (
          <div
            className="inspect__span"
            style={{
              left: pct(target.transitSlot, 0),
              width: 2,
              background: 'var(--moon)',
              top: 0,
              bottom: 0,
            }}
          />
        )}
        <div
          className="inspect__span"
          style={{ left: pct(slot, 0), width: 2, background: 'var(--accent)', top: 0, bottom: 0 }}
        />
      </div>
      <div className="row spread faint" style={{ fontSize: 10, marginTop: 3 }}>
        <span>up {at(target.riseSlot)}–{at(target.setSlot)}</span>
        <span style={{ color: 'var(--accent)' }}>
          usable {at(target.firstUsableSlot)}–{at(target.lastUsableSlot)}
        </span>
        <span style={{ color: 'var(--moon)' }}>transit {at(target.transitSlot)}</span>
      </div>
    </div>
  )
}

/**
 * A real photograph of the object, from the Digitized Sky Survey via CDS.
 *
 * Fetched through our own endpoint rather than straight from the survey: a cutout
 * takes about 1.4 seconds upstream, so the server caches it to disk and every
 * later request is instant. Failure is silent by design — an object without a
 * picture is an object without a picture, not a broken panel.
 */
function Thumbnail({ row }: { row: GeometryRow }) {
  const [failed, setFailed] = useState(false)
  if (failed) return null
  const src =
    `/api/thumbnail?ra=${row.raDeg.toFixed(4)}&dec=${row.decDeg.toFixed(4)}` +
    `&fov=0.6&size=384`
  return (
    <img
      className="inspect__thumb"
      src={src}
      alt={`Digitized Sky Survey image of ${row.name}`}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  )
}
