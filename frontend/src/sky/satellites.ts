/**
 * Satellites between their ten-second samples.
 *
 * The server sends each pass as alt/az every ten seconds. Between samples the
 * path is a great circle to well under a tenth of a degree, so interpolating
 * the two bracketing UNIT VECTORS (never the angles -- azimuth wraps, and a
 * satellite crossing north would fly the long way round) puts the dot on the
 * real track at any instant, including the fractional seconds between frames.
 */

import { Vector3 } from 'three'
import type { Satellites } from '../api/client'
import { horizonVector } from './view'

export interface SatTrack {
  norad: number
  name: string
  t0: number
  stepMs: number
  t1: number
  /** Horizon-frame unit vectors, xyz interleaved. */
  dirs: Float32Array
  /** Estimated magnitude per sample; NaN in Earth's shadow. */
  mag: Float32Array
  rangeKm: Float32Array
  altDeg: Float32Array
  azDeg: Float32Array
}

export function prepareTracks(sats: Satellites | undefined): SatTrack[] {
  if (!sats?.available) return []
  return sats.passes
    .map((p) => {
      const n = p.altitudeDeg.length
      const dirs = new Float32Array(n * 3)
      const v = new Vector3()
      for (let i = 0; i < n; i++) {
        horizonVector(p.altitudeDeg[i] ?? 0, p.azimuthDeg[i] ?? 0, v)
        dirs[i * 3] = v.x
        dirs[i * 3 + 1] = v.y
        dirs[i * 3 + 2] = v.z
      }
      const t0 = Date.parse(p.startsAt)
      const stepMs = p.stepSeconds * 1000
      return {
        norad: p.noradId,
        name: p.name,
        t0,
        stepMs,
        t1: t0 + stepMs * (n - 1),
        dirs,
        mag: Float32Array.from(p.magnitude, (m) => (m === null ? NaN : m)),
        rangeKm: Float32Array.from(p.rangeKm),
        altDeg: Float32Array.from(p.altitudeDeg),
        azDeg: Float32Array.from(p.azimuthDeg),
      }
    })
    .sort((a, b) => a.t0 - b.t0)
}

export interface SatSample {
  track: SatTrack
  dir: Vector3
  /** NaN while in shadow. Interpolated, so a satellite fades into eclipse. */
  mag: number
  /** Index of the nearest sample: the server's own numbers, for display. */
  nearest: number
}

/** Where a track is at `tMs`, or null outside its pass. */
export function sampleTrack(track: SatTrack, tMs: number, out: SatSample): SatSample | null {
  if (tMs < track.t0 || tMs > track.t1) return null
  const x = (tMs - track.t0) / track.stepMs
  const i = Math.min(Math.floor(x), track.mag.length - 2)
  const f = x - i
  const d = track.dirs
  out.track = track
  out.dir.set(
    (d[i * 3] ?? 0) * (1 - f) + (d[i * 3 + 3] ?? 0) * f,
    (d[i * 3 + 1] ?? 0) * (1 - f) + (d[i * 3 + 4] ?? 0) * f,
    (d[i * 3 + 2] ?? 0) * (1 - f) + (d[i * 3 + 5] ?? 0) * f,
  ).normalize()
  const ma = track.mag[i] ?? NaN
  const mb = track.mag[i + 1] ?? NaN
  // Entering or leaving the shadow takes a couple of seconds for real; fade
  // across the sample instead of blinking.
  if (Number.isNaN(ma) && Number.isNaN(mb)) out.mag = NaN
  else if (Number.isNaN(ma)) out.mag = mb + 4 * (1 - f)
  else if (Number.isNaN(mb)) out.mag = ma + 4 * f
  else out.mag = ma + (mb - ma) * f
  out.nearest = Math.min(track.mag.length - 1, Math.round(x))
  return out
}
