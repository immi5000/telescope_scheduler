/**
 * The skyline: what makes the view read as standing in a field.
 *
 * We have no panorama of anyone's actual site, so this is a generated
 * silhouette -- and it is labelled as one wherever that matters. It is a
 * profile, not a picture: for each azimuth, how high the far hills reach and
 * how high the near trees and roofs reach, in degrees. The landscape shader
 * compares a pixel's altitude against it.
 *
 * Seeded from the site, so a site keeps its skyline from session to session
 * and two sites do not share one. Heights are kept low (under ~7 degrees)
 * because the altitude floor for imaging is typically 30: the skyline is
 * scenery, never a claim about what is observable.
 */

import { DataTexture, DataUtils, HalfFloatType, LinearFilter, RGFormat, RepeatWrapping } from 'three'

export const PROFILE_SAMPLES = 4096

/** Mulberry32: tiny, fast, deterministic. Not for anything but scenery. */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function seedFor(lat: number, lon: number, name: string): number {
  let h = 2166136261
  const s = `${lat.toFixed(3)},${lon.toFixed(3)},${name}`
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619)
  return h >>> 0
}

/** Periodic value noise over [0, 1): octaves of random harmonics. */
function periodicNoise(rand: () => number, harmonics: number[], amps: number[]) {
  const terms = harmonics.map((k, i) => ({ k, a: amps[i] ?? 0, p: rand() * Math.PI * 2 }))
  return (u: number) => terms.reduce((s, t) => s + t.a * Math.sin(2 * Math.PI * t.k * u + t.p), 0)
}

export interface Skyline {
  /** RG float texture: R = far hills, G = near trees and buildings, degrees. */
  texture: DataTexture
  far: Float32Array
  near: Float32Array
}

/**
 * Build the profile. Azimuth runs 0..360 across the texture, North first,
 * East at a quarter -- the same convention as everything else in the scene.
 */
export function buildSkyline(seed: number): Skyline {
  const rand = rng(seed)
  const n = PROFILE_SAMPLES
  const far = new Float32Array(n)
  const near = new Float32Array(n)

  // Far hills: low, broad, and never quite flat.
  const hills = periodicNoise(rand, [1, 2, 3, 5, 8, 13], [0.55, 0.45, 0.3, 0.2, 0.12, 0.06])
  for (let i = 0; i < n; i++) far[i] = Math.max(0.15, 0.9 + hills(i / n))

  // Near trees: clumps of crowns along a few hedgerows, with open gaps.
  const put = (azDeg: number, halfWidthDeg: number, fn: (x: number) => number) => {
    const c = Math.round((azDeg / 360) * n)
    const w = Math.ceil((halfWidthDeg / 360) * n)
    for (let j = -w; j <= w; j++) {
      const idx = (((c + j) % n) + n) % n
      const h = fn(j / w)
      if (h > (near[idx] ?? 0)) near[idx] = h
    }
  }

  const rows = 5 + Math.floor(rand() * 3)
  for (let r = 0; r < rows; r++) {
    const start = rand() * 360
    const span = 12 + rand() * 45
    const baseH = 1.6 + rand() * 2.6
    let az = start
    while (az < start + span) {
      const h = baseH * (0.65 + rand() * 0.6)
      const w = 0.7 + rand() * 1.9
      // A crown: a rounded dome with a trunk-width shoulder under it.
      put(az, w, (x) => h * Math.sqrt(Math.max(0, 1 - x * x)) ** 0.8)
      az += w * (0.9 + rand() * 0.9)
    }
  }

  // A few lone trees, tall and narrow.
  for (let t = 0; t < 9; t++) {
    const h = 3 + rand() * 4.5
    const w = 0.9 + rand() * 1.4
    put(rand() * 360, w, (x) => h * Math.sqrt(Math.max(0, 1 - x * x)) ** 0.7)
  }

  // One farmhouse and a barn: straight lines are what the eye reads as "here".
  const house = rand() * 360
  put(house, 2.2, (x) => (Math.abs(x) < 1 ? 1.5 + 0.9 * (1 - Math.abs(x)) : 0))
  put(house + 3.1, 0.12, () => 2.7) // chimney
  const barn = house + 20 + rand() * 60
  put(barn, 3.2, (x) => (Math.abs(x) < 1 ? 1.9 + 0.7 * Math.max(0, 1 - Math.abs(x) * 1.6) : 0))

  // Leafy edges: fine noise on anything tall enough to be foliage.
  const leaf = periodicNoise(
    rand,
    [97, 151, 233, 389, 601],
    [0.09, 0.07, 0.05, 0.035, 0.025],
  )
  for (let i = 0; i < n; i++) {
    const v = near[i] ?? 0
    if (v > 0.6) near[i] = v + leaf(i / n) * Math.min(1, v / 2)
  }

  // Half float, not float: linear filtering of 32-bit float textures is an
  // optional extension, and without it the skyline renders as steps.
  const data = new Uint16Array(n * 2)
  for (let i = 0; i < n; i++) {
    data[i * 2] = DataUtils.toHalfFloat(far[i] ?? 0)
    data[i * 2 + 1] = DataUtils.toHalfFloat(near[i] ?? 0)
  }
  const texture = new DataTexture(data, n, 1, RGFormat, HalfFloatType)
  texture.wrapS = RepeatWrapping
  texture.magFilter = LinearFilter
  texture.minFilter = LinearFilter
  texture.needsUpdate = true
  return { texture, far, near }
}

/** The skyline's height at an azimuth, for hiding labels behind it. */
export function skylineAt(s: Skyline, azDeg: number): number {
  const n = PROFILE_SAMPLES
  const x = ((((azDeg / 360) * n) % n) + n) % n
  const i = Math.floor(x)
  const f = x - i
  const j = (i + 1) % n
  const h = (arr: Float32Array) => (arr[i] ?? 0) * (1 - f) + (arr[j] ?? 0) * f
  return Math.max(h(s.far), h(s.near))
}
