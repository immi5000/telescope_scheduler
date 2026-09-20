/**
 * Where is the telescope pointing, at an arbitrary instant?
 *
 * The sky's own answer, kept out of the renderer: pointing is astronomy, not
 * drawing, so it is computed once here and read by whatever draws it.
 *
 * **Interpolate as unit vectors, never as (alt, az) scalars.** Azimuth wraps:
 * lerping 359 to 1 sweeps 358 degrees the wrong way round the compass, and a
 * target crossing due north visibly flies across the entire sky and back.
 * Worse, azimuth is ill-conditioned near the zenith, where a target moving a
 * tenth of a degree can swing its azimuth through 90. Vectors have neither
 * problem.
 */

import { Vector3 } from 'three'
import { midpointSpan, type SlotClock } from '../time/slots'

const D2R = Math.PI / 180
const R2D = 180 / Math.PI

/** Right-handed local basis: x = East, y = North, z = Up. */
export function altAzToVector(altDeg: number, azDeg: number, out = new Vector3()): Vector3 {
  const alt = altDeg * D2R
  const az = azDeg * D2R
  const ca = Math.cos(alt)
  return out.set(ca * Math.sin(az), ca * Math.cos(az), Math.sin(alt))
}

export interface Pointing {
  /** Unit vector in the local (East, North, Up) basis. */
  vector: Vector3
  altitudeDeg: number
  azimuthDeg: number
}

const _a = new Vector3()
const _b = new Vector3()

/**
 * Interpolated pointing at `tMs`, against the MIDPOINT axis.
 *
 * Normalised linear interpolation rather than true slerp: over one five-minute
 * slot a target moves at most ~1.25 degrees, where nlerp and slerp differ by
 * under an arcsecond. Slerp's `acos` and two `sin` calls per target per frame
 * would buy nothing and cost more than the rest of this function.
 */
export function pointingAt(
  clock: SlotClock,
  altitudeDeg: ArrayLike<number>,
  azimuthDeg: ArrayLike<number>,
  tMs: number,
  out: Pointing = { vector: new Vector3(), altitudeDeg: 0, azimuthDeg: 0 },
): Pointing {
  const { a, b, f } = midpointSpan(clock, tMs)
  altAzToVector(altitudeDeg[a] ?? 0, azimuthDeg[a] ?? 0, _a)
  if (f === 0 || b === a) {
    out.vector.copy(_a)
  } else {
    altAzToVector(altitudeDeg[b] ?? 0, azimuthDeg[b] ?? 0, _b)
    out.vector.copy(_a).lerp(_b, f).normalize()
  }
  out.altitudeDeg = Math.asin(Math.max(-1, Math.min(1, out.vector.z))) * R2D
  out.azimuthDeg = (Math.atan2(out.vector.x, out.vector.y) * R2D + 360) % 360
  return out
}

export const COMPASS = [
  'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW',
] as const

export const compassPoint = (azDeg: number): string =>
  COMPASS[Math.round((((azDeg % 360) + 360) % 360) / 22.5) % 16] ?? 'N'

/**
 * Project a local vector onto a zenith-centred polar dial.
 *
 * North up, East RIGHT -- the view looking UP at the sky with the map held
 * overhead, which is how an observer standing under it actually experiences
 * the compass. (A map of the ground would put East on the left. This is not
 * that, and the cardinal labels say so.)
 *
 * Returns radius as a fraction of the horizon circle: 0 at the zenith, 1 at
 * the horizon, and greater than 1 below it.
 */
export function polarProject(v: Vector3): { x: number; y: number; r: number } {
  const alt = Math.asin(Math.max(-1, Math.min(1, v.z))) * R2D
  const az = Math.atan2(v.x, v.y)
  const r = (90 - alt) / 90
  return { x: r * Math.sin(az), y: -r * Math.cos(az), r }
}
