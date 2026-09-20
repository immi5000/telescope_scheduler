/**
 * The observing route: the plan's targets as STOPS in observing order, joined
 * by LEGS, drawn on the sky.
 *
 * A target is fixed on the celestial sphere, so in the ICRS frame the route is
 * a graph of fixed points and the whole of it wheels across the sky with the
 * stars. That makes it cheap: the geometry depends only on the plan, and the
 * cursor restyles it through one uniform (see `routeMaterial`).
 *
 * Legs are great-circle arcs, which is "where next" rather than the mount's
 * true motion: an alt-az mount drives two axes and its real path bows away
 * from the great circle. They are sampled at 1.5 degrees or finer, because a
 * stereographic projection draws a great circle as a CIRCLE, and a long leg
 * drawn as one straight chord visibly cuts the corner at wide fields.
 */

import { BufferAttribute, BufferGeometry, Vector3 } from 'three'
import type { Plan } from '../api/client'
import type { SlotClock } from '../time/slots'

export interface RouteStop {
  targetId: string
  /** 1-based position in the night's observing order. */
  order: number
  /** Unit vector, ICRS. */
  dir: Vector3
  startMs: number
  /** When the slew ends and the shutter opens. */
  arriveMs: number
  endMs: number
}

/**
 * The plan's blocks as stops. Consecutive blocks on the same target are one
 * stop: the telescope does not move between them.
 */
export function routeStops(plan: Plan | undefined, dirs: Map<string, Vector3>): RouteStop[] {
  const out: RouteStop[] = []
  for (const b of plan?.blocks ?? []) {
    const dir = dirs.get(b.targetId)
    if (!dir) continue
    const last = out[out.length - 1]
    const start = Date.parse(b.startsAt)
    const end = Date.parse(b.endsAt)
    if (last && last.targetId === b.targetId && last.endMs === start) {
      last.endMs = end
      continue
    }
    out.push({
      targetId: b.targetId,
      order: out.length + 1,
      dir,
      startMs: start,
      arriveMs: Date.parse(b.integratingFrom),
      endMs: end,
    })
  }
  return out
}

const MAX_STEP_RAD = (1.5 * Math.PI) / 180

/** Spherical interpolation of unit vectors, into `out`. */
function slerp(a: Vector3, b: Vector3, theta: number, f: number, out: Vector3): Vector3 {
  if (theta < 1e-6) return out.copy(a)
  const s = Math.sin(theta)
  const wa = Math.sin((1 - f) * theta) / s
  const wb = Math.sin(f * theta) / s
  return out.set(a.x * wa + b.x * wb, a.y * wa + b.y * wb, a.z * wa + b.z * wb)
}

/**
 * Thick-line geometry for every leg, or null when there is no leg to draw.
 *
 * Per vertex: the leg's instants (as night fractions, `aT` = when it becomes
 * NEXT, when the slew STARTS, when it ENDS), the position along the leg
 * (`aU`, 0 to 1) and the leg's length in degrees (`aLen`, for dashes).
 */
export function routeGeometry(stops: RouteStop[], clock: SlotClock): BufferGeometry | null {
  if (stops.length < 2) return null
  const span = clock.endMs - clock.startMs
  const frac = (ms: number) => (span > 0 ? (ms - clock.startMs) / span : 0)

  const pos: number[] = []
  const other: number[] = []
  const side: number[] = []
  const us: number[] = []
  const ts: number[] = []
  const lens: number[] = []
  const index: number[] = []
  const p = new Vector3()
  const q = new Vector3()

  for (let i = 0; i + 1 < stops.length; i++) {
    const from = stops[i]!
    const to = stops[i + 1]!
    const theta = from.dir.angleTo(to.dir)
    if (theta < 1e-6) continue
    const n = Math.max(1, Math.ceil(theta / MAX_STEP_RAD))
    const t0 = frac(from.startMs)
    const t1 = frac(to.startMs)
    const t2 = frac(to.arriveMs)
    const lenDeg = (theta * 180) / Math.PI

    for (let k = 0; k < n; k++) {
      const fa = k / n
      const fb = (k + 1) / n
      slerp(from.dir, to.dir, theta, fa, p)
      slerp(from.dir, to.dir, theta, fb, q)
      // Corners round the quad: A+, A-, then at the far end B with "other"
      // pointing BACK, which reverses the normal -- so +1/-1 there lands on
      // B-, B+. Getting this backwards crosses the triangles and the line
      // renders dotted.
      const corners: [Vector3, Vector3, number, number][] = [
        [p, q, 1, fa],
        [p, q, -1, fa],
        [q, p, 1, fb],
        [q, p, -1, fb],
      ]
      const o = pos.length / 3
      for (const [at, toward, sd, u] of corners) {
        pos.push(at.x, at.y, at.z)
        other.push(toward.x, toward.y, toward.z)
        side.push(sd)
        us.push(u)
        ts.push(t0, t1, t2)
        lens.push(lenDeg)
      }
      index.push(o, o + 1, o + 2, o, o + 2, o + 3)
    }
  }
  if (!index.length) return null

  const g = new BufferGeometry()
  g.setAttribute('position', new BufferAttribute(new Float32Array(pos), 3))
  g.setAttribute('aOther', new BufferAttribute(new Float32Array(other), 3))
  g.setAttribute('aSide', new BufferAttribute(new Float32Array(side), 1))
  g.setAttribute('aU', new BufferAttribute(new Float32Array(us), 1))
  g.setAttribute('aT', new BufferAttribute(new Float32Array(ts), 3))
  g.setAttribute('aLen', new BufferAttribute(new Float32Array(lens), 1))
  g.setIndex(index)
  return g
}
