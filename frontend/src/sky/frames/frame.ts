/**
 * The frame, and the assertion that proves it.
 *
 * **The client computes no astronomy. It validates the server's.**
 *
 * The backend publishes, per slot, a quaternion taking the observer's horizon
 * basis (East, North, Up) into ICRS. Stars and targets are already ICRS, so
 * they go into one static buffer and never move; the horizon rig carries that
 * quaternion and the night advances by re-orienting a single object.
 *
 * The obvious alternative -- derive the frame in the browser from sidereal
 * time and latitude, `zenith = (RA = LST, Dec = lat)` -- is wrong, and wrong
 * in the worst possible way. It is valid only in apparent coordinates of date,
 * while everything we draw is ICRS/J2000, so in 2026 it rotates the entire sky
 * by 0.32 degrees. Nineteen arcminutes. It renders beautifully.
 *
 * So: `validateFrame` checks that rotating each target's ICRS unit vector by
 * the published quaternion reproduces the published altitude. That single dot
 * product catches every handedness error, every sign error, every axis swap
 * and every half-slot offset in the whole scene, and it fires the first time
 * you write it.
 */

import { Quaternion, Vector3 } from 'three'
import type { Geometry } from '../../api/client'

/** ICRS unit vector from right ascension and declination, both in degrees. */
export function icrsUnit(raDeg: number, decDeg: number, out = new Vector3()): Vector3 {
  const ra = (raDeg * Math.PI) / 180
  const dec = (decDeg * Math.PI) / 180
  const cd = Math.cos(dec)
  return out.set(cd * Math.cos(ra), cd * Math.sin(ra), Math.sin(dec))
}

/**
 * Read slot `i`'s rotation out of the payload.
 *
 * Sign is normalised to `w >= 0`. The backend already does this, but doing it
 * again here is three operations and removes a whole class of bug: a slerp
 * between q and -q -- which are the SAME rotation -- takes the long way round
 * the sphere, so the horizon would spin through 360 degrees during one
 * five-minute slot, once, somewhere in the middle of the night.
 */
export function frameQuaternion(geo: Geometry, i: number, out = new Quaternion()): Quaternion {
  const row = geo.frameQuat[Math.max(0, Math.min(geo.frameQuat.length - 1, i))]
  if (!row) return out.identity()
  const [x = 0, y = 0, z = 0, w = 1] = row
  out.set(x, y, z, w)
  if (out.w < 0) out.set(-out.x, -out.y, -out.z, -out.w)
  return out.normalize()
}

export interface FrameResidual {
  /** Worst altitude disagreement across every target and slot, in degrees. */
  worstDeg: number
  worstTarget: string
  worstSlot: number
  samples: number
  ok: boolean
}

/**
 * Tolerance, in degrees.
 *
 * Not zero, and not arbitrary. ICRS to horizontal is not exactly a rigid
 * rotation -- annual aberration is direction-dependent and reaches ~20 arcsec
 * -- so approximating it by the nearest true rotation leaves a residual of
 * about 30 arcsec. That approximation is what lets the client hold ONE
 * orientation per slot instead of transforming every object separately, and
 * 30 arcsec on a sphere a thousand pixels across is a twentieth of a pixel.
 *
 * 0.02 degrees is 72 arcsec: comfortably above the physical floor, and far
 * below the 0.32 degrees that the naive sidereal construction would introduce.
 * Anything real lands well outside it.
 */
export const FRAME_TOLERANCE_DEG = 0.02

/**
 * Does the published rotation reproduce the published altitudes?
 *
 * Runs once when geometry arrives. Cost is targets x slots dot products --
 * about 700 for a normal night, well under a millisecond.
 */
export function validateFrame(geo: Geometry): FrameResidual {
  const q = new Quaternion()
  const v = new Vector3()
  let worstDeg = 0
  let worstTarget = ''
  let worstSlot = -1
  let samples = 0

  for (const row of geo.rows) {
    const icrs = icrsUnit(row.raDeg, row.decDeg)
    for (let s = 0; s < geo.nSlots; s++) {
      const alt = row.altitudeDeg[s]
      if (alt === undefined) continue
      frameQuaternion(geo, s, q)
      // Local components are R^T v, which is v rotated by the CONJUGATE.
      // Getting this backwards is the single easiest mistake here, and it
      // produces a sky that is wrong by a rotation rather than obviously
      // broken -- which is why it is asserted rather than trusted.
      v.copy(icrs).applyQuaternion(q.conjugate())
      const got = (Math.asin(Math.max(-1, Math.min(1, v.z))) * 180) / Math.PI
      const d = Math.abs(got - alt)
      samples++
      if (d > worstDeg) {
        worstDeg = d
        worstTarget = row.targetId
        worstSlot = s
      }
    }
  }
  return {
    worstDeg,
    worstTarget,
    worstSlot,
    samples,
    ok: samples > 0 && worstDeg <= FRAME_TOLERANCE_DEG,
  }
}

export function describeResidual(r: FrameResidual): string {
  if (!r.samples) return 'frame: no samples'
  const arcsec = r.worstDeg * 3600
  const where = r.worstTarget ? ` (worst: ${r.worstTarget} @ slot ${r.worstSlot})` : ''
  return `frame residual ${arcsec.toFixed(1)}″${where}`
}
