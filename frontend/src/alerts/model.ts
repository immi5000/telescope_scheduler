/**
 * Alerts: what happened to the night, sorted by KIND before severity.
 *
 * Three sources, answering different questions:
 *
 *   NEWS      a forecast run (later, a ZTF/LSST transient) arrived and the plan
 *             moved. Happens at an instant.
 *   HAZARDS   something about one block -- cloud over it, the Moon near it, too
 *             few frames. Holds for the block's duration.
 *   CROSSINGS a sunlit satellite passes through a block's field of view.
 *
 * The category leads everywhere an alert is shown -- colour, icon and label --
 * because "a satellite" and "a supernova" call for opposite reactions, and the
 * observer should know which this is before reading a word of it.
 */

import type { Block, DecisionPoint, Plan, Session } from '../api/client'
import type { components } from '../api/schema'
import { shortName } from '../format'

export type AlertCategory =
  | 'transient'
  | 'weather'
  | 'satellite'
  | 'moon'
  | 'horizon'
  | 'exposure'
  | 'plan'

export type AlertSeverity = 'info' | 'warn' | 'critical'

export interface Alert {
  id: string
  category: AlertCategory
  severity: AlertSeverity
  /** When it starts: a decision point's instant, a block's start, a crossing. */
  tMs: number
  /** When it stops being current. */
  untilMs: number
  /** Short enough to read at a glance. */
  title: string
  /** One line of context: which block, what changed. */
  summary: string
  /** What to do about it, or why -- behind a disclosure, never inline. */
  detail?: string
  /** The block it belongs to, so the Now card can show only its own. */
  block?: { targetId: string; slotStart: number }
  /** Where "Show me" should take the cursor, when that is somewhere else. */
  seekMs?: number
}

export const CATEGORY: Record<AlertCategory, { label: string; color: string }> = {
  transient: { label: 'Transient', color: 'var(--alert-transient)' },
  weather: { label: 'Weather', color: 'var(--alert-weather)' },
  satellite: { label: 'Satellite', color: 'var(--alert-satellite)' },
  moon: { label: 'Moon', color: 'var(--moon)' },
  horizon: { label: 'Horizon', color: 'var(--ink-dim)' },
  exposure: { label: 'Frames', color: 'var(--ink-dim)' },
  plan: { label: 'Plan', color: 'var(--accent)' },
}

const RANK: Record<AlertSeverity, number> = { critical: 2, warn: 1, info: 0 }

export const bySeverity = (a: Alert, b: Alert): number =>
  RANK[b.severity] - RANK[a.severity] || a.tMs - b.tMs

/** How long news counts as "just happened", in night time. */
const NEWS_MS = 20 * 60_000

/** The instant after which an alert is no longer worth popping. */
export const liveUntil = (a: Alert): number => Math.max(a.untilMs, a.tMs + NEWS_MS)

/** Hazards at info level (the Moon being near) are shown, never popped. */
export const pops = (a: Alert): boolean => a.severity !== 'info' || a.block === undefined

const hhmm = (ms: number): string => new Date(ms).toISOString().slice(11, 16)

const capitalise = (s: string): string => (s ? s[0]!.toUpperCase() + s.slice(1) : s)

export function decisionCategory(dp: DecisionPoint): AlertCategory {
  // `amend` -- the observer added a target -- is plan news, not weather.
  return dp.kind === 'weather' ? 'weather' : 'plan'
}

const NEWS_TITLE: Partial<Record<AlertCategory, string>> = {
  weather: 'Forecast update',
  transient: 'New transient',
}

/** "dropped NGC 7000 · 12 slots moved", with names rather than ids. */
export function changeSummary(dp: DecisionPoint, names: Map<string, string>): string {
  const n = (ids: string[]) => ids.map((id) => shortName(names.get(id) ?? id)).join(', ')
  return [
    dp.droppedTargets.length ? `dropped ${n(dp.droppedTargets)}` : '',
    dp.addedTargets.length ? `added ${n(dp.addedTargets)}` : '',
    dp.changedSlots ? `${dp.changedSlots} slot${dp.changedSlots === 1 ? '' : 's'} moved` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

/**
 * What an update did to the SCHEDULE, in one or two plain sentences.
 *
 * `changeSummary` is the log's shorthand -- right for a dense list someone is
 * scanning deliberately. A toast is read once, in passing, by someone who may
 * be at the eyepiece with cold hands, so it says the same thing in words.
 *
 * It names the time a new target actually gets, because "added" on its own
 * does not answer the only question worth asking at 3 a.m. -- do I need to do
 * something now? Only the plan the decision produced can answer that, so the
 * time appears when `plan` is that plan and is left out rather than guessed
 * when it is not.
 */
export function planEffect(
  dp: DecisionPoint,
  names: Map<string, string>,
  slotMinutes: number,
  plan?: Plan,
): string {
  const label = (id: string) => shortName(names.get(id) ?? id)
  const startsAt = (id: string): string => {
    if (!plan || plan.planId !== dp.planId) return ''
    const b = plan.blocks.find((x) => x.targetId === id)
    return b ? ` at ${hhmm(Date.parse(b.startsAt))}` : ''
  }

  const { addedTargets: added, droppedTargets: dropped } = dp
  const parts: string[] = []
  if (added.length === 1) parts.push(`${label(added[0]!)} is in${startsAt(added[0]!)}`)
  else if (added.length > 1) parts.push(`${added.length} targets are in`)
  if (dropped.length === 1) parts.push(`${label(dropped[0]!)} is out`)
  else if (dropped.length > 1) parts.push(`${dropped.length} targets are out`)

  // Slots whose content differs from the previous plan: some swapped target,
  // some emptied. "Planned differently" covers both; "moved" would not.
  const moved = dp.changedSlots * slotMinutes
  const shift = moved > 0 ? `About ${duration(moved)} of the night is planned differently.` : ''
  if (parts.length === 0) return shift || 'The plan is unchanged.'
  return `${capitalise(parts.join(', '))}.${shift ? ` ${shift}` : ''}`
}

/** "45 min", "1 h", "2 h 30 min" -- the unit is repeated on purpose, since
 *  a bare "2 h 30" reads as a clock time in a sentence about a night. */
function duration(minutes: number): string {
  const m = Math.round(minutes)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  const rest = m % 60
  return rest === 0 ? `${h} h` : `${h} h ${rest} min`
}

export const changed = (dp: DecisionPoint): boolean =>
  dp.changedSlots > 0 || dp.addedTargets.length > 0 || dp.droppedTargets.length > 0

export function targetNames(session: Session): Map<string, string> {
  return new Map(session.targets.map((t) => [t.id, t.name]))
}

/**
 * One alert per update that actually moved the plan.
 *
 * Silence on the opening plan, and silence when nothing moved: an alert that
 * fires for every forecast run is one people learn to ignore, and then the one
 * that matters is ignored too.
 */
export function newsAlerts(session: Session, plan?: Plan): Alert[] {
  const names = targetNames(session)
  const slotMinutes = session.grid.slotMinutes
  return session.decisionPoints
    .filter((dp) => dp.index > 0 && changed(dp))
    .map((dp) => {
      const t = Date.parse(dp.at)
      const category = decisionCategory(dp)
      return {
        id: `${session.id}:dp-${dp.index}`,
        category,
        severity: dp.droppedTargets.length ? 'warn' : 'info',
        tMs: t,
        untilMs: t,
        title: dp.kind === 'amend' ? 'Target added' : (NEWS_TITLE[category] ?? 'Plan updated'),
        summary: planEffect(dp, names, slotMinutes, plan),
        detail: `${capitalise(dp.reason)}.`,
        seekMs: t,
      } satisfies Alert
    })
}

/** The warnings the server attached to each block of the plan in force. */
export function hazardAlerts(session: Session, plan: Plan): Alert[] {
  return plan.blocks.flatMap((b) =>
    b.warnings.map(
      (w) =>
        ({
          // No plan id: the same hazard on the same block under a newer plan is
          // the same alert, and must not pop a second time.
          id: `${session.id}:${b.targetId}:${b.slotStart}:${w.category}`,
          category: w.category,
          severity: (w.severity as AlertSeverity) ?? 'info',
          tMs: Date.parse(b.startsAt),
          untilMs: Date.parse(b.endsAt),
          title: capitalise(w.text),
          summary: blockLabel(b),
          detail: `${capitalise(w.action)}.`,
          block: { targetId: b.targetId, slotStart: b.slotStart },
        }) satisfies Alert,
    ),
  )
}

export function blockLabel(b: Block): string {
  return `${shortName(b.name)} · ${b.startsAt.slice(11, 16)}–${b.endsAt.slice(11, 16)}`
}

// ------------------------------------------------------------- satellites

type Satellites = components['schemas']['SatellitesOut']
type Geometry = components['schemas']['GeometryOut']
type Vec = [number, number, number]

const D2R = Math.PI / 180

/** Local east, north, up. Only angles between vectors are ever taken. */
const enu = (altDeg: number, azDeg: number): Vec => {
  const alt = altDeg * D2R
  const az = azDeg * D2R
  return [Math.cos(alt) * Math.sin(az), Math.cos(alt) * Math.cos(az), Math.sin(alt)]
}
const dot = (a: Vec, b: Vec) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
const cross = (a: Vec, b: Vec): Vec => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
]
const angle = (a: Vec, b: Vec) => Math.acos(Math.max(-1, Math.min(1, dot(a, b))))

/**
 * Closest approach of point `t` to the great-circle arc a→b, in radians.
 *
 * Samples alone are not enough: a satellite in low orbit covers about a degree
 * a second, so between two samples it can jump clean over a two-degree field.
 */
function arcDistance(t: Vec, a: Vec, b: Vec): number {
  const n = cross(a, b)
  const len = Math.hypot(...n)
  if (len < 1e-9) return angle(t, a)
  const u: Vec = [n[0] / len, n[1] / len, n[2] / len]
  const within = dot(cross(a, t), u) >= 0 && dot(cross(t, b), u) >= 0
  return within
    ? Math.asin(Math.min(1, Math.abs(dot(t, u))))
    : Math.min(angle(t, a), angle(t, b))
}

/**
 * Sunlit satellites that cross a block's field while it is integrating.
 *
 * Display only, like the payload it reads: nothing here reaches the plan. The
 * elements are the CURRENT set, so for a replay days in the past the timing is
 * an estimate, and the alert says so rather than letting it pass as exact.
 */
export function crossingAlerts(
  session: Session,
  plan: Plan,
  geometry: Geometry,
  sats: Satellites | undefined,
): Alert[] {
  if (!sats?.available || !plan.blocks.length) return []
  const startMs = Date.parse(session.grid.start)
  const slotMs = session.grid.slotMinutes * 60_000
  const nSlots = session.grid.nSlots
  const eq = session.equipment
  // Half the field's diagonal: a trail anywhere in the frame counts.
  const radius = (Math.hypot(eq.fovWidthDeg, eq.fovHeightDeg) / 2) * D2R

  const bySlot: (Block | undefined)[] = new Array(nSlots)
  for (const b of plan.blocks) for (let s = b.slotStart; s < b.slotEnd; s++) bySlot[s] = b
  const rows = new Map(geometry.rows.map((r) => [r.targetId, r]))

  /** Target direction at an instant, interpolated between slot midpoints. */
  const targetAt = (targetId: string, tMs: number): Vec | null => {
    const row = rows.get(targetId)
    if (!row) return null
    const u = (tMs - startMs) / slotMs - 0.5
    const k = Math.max(0, Math.min(nSlots - 1, Math.floor(u)))
    const k1 = Math.min(nSlots - 1, k + 1)
    const f = Math.max(0, Math.min(1, u - k))
    const a = enu(row.altitudeDeg[k] ?? 0, row.azimuthDeg[k] ?? 0)
    const b = enu(row.altitudeDeg[k1] ?? 0, row.azimuthDeg[k1] ?? 0)
    const v: Vec = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]
    const len = Math.hypot(...v) || 1
    return [v[0] / len, v[1] / len, v[2] / len]
  }

  const stale = sats.epochSpreadDays > 1
  const found = new Map<string, Alert>()

  for (const p of sats.passes) {
    const t0 = Date.parse(p.startsAt)
    const stepMs = p.stepSeconds * 1000
    for (let i = 0; i + 1 < p.altitudeDeg.length; i++) {
      const m0 = p.magnitude[i]
      const m1 = p.magnitude[i + 1]
      // In Earth's shadow a satellite is invisible and leaves no trail.
      if (m0 == null || m1 == null) continue
      const tMs = t0 + i * stepMs
      const slot = Math.floor((tMs - startMs) / slotMs)
      const block = bySlot[slot]
      if (!block || tMs < Date.parse(block.integratingFrom)) continue
      const target = targetAt(block.targetId, tMs)
      if (!target) continue
      const a = enu(p.altitudeDeg[i] ?? 0, p.azimuthDeg[i] ?? 0)
      const b = enu(p.altitudeDeg[i + 1] ?? 0, p.azimuthDeg[i + 1] ?? 0)
      if (arcDistance(target, a, b) > radius) continue

      // One alert per satellite per block, however many samples it spans.
      const id = `${session.id}:sat-${p.noradId}-${block.targetId}-${block.slotStart}`
      if (found.has(id)) continue
      const mag = Math.min(m0, m1)
      found.set(id, {
        id,
        category: 'satellite',
        severity: mag < 2 ? 'warn' : 'info',
        tMs,
        untilMs: tMs,
        title: `${prettySatellite(p.name)} crosses the field`,
        summary: `${shortName(block.name)} · ${hhmm(tMs)} · mag ${mag.toFixed(1)}`,
        detail:
          `Expect a trail in the frame exposing at ${hhmm(tMs)} UTC. Keep the frame: ` +
          'sigma-clipping removes the trail pixels from the stack.' +
          (stale
            ? ` Predicted from orbital elements ${sats.epochSpreadDays.toFixed(0)} days from ` +
              'this night, so the time may be off by minutes.'
            : ''),
        block: { targetId: block.targetId, slotStart: block.slotStart },
        seekMs: tMs,
      })
    }
  }
  return [...found.values()]
}

/** "ISS (ZARYA)" reads better than "ISS (ZARYA)                ". */
const prettySatellite = (name: string): string => name.trim().replace(/\s+/g, ' ')
