/**
 * Star colour from B-V, via the Planckian locus.
 *
 * **This is data, not theme.** Vega is blue-white and Betelgeuse is orange
 * because of their photospheric temperatures, and no design decision gets a
 * vote. If someone later wants the sky to harmonise with the accent colour,
 * the thing to change is the exposure and saturation of the whole star layer,
 * never this function.
 *
 * Chain: B-V -> effective temperature (Ballesteros 2012, a closed-form fit to
 * the colour-temperature relation) -> CIE xy on the Planckian locus (Kim et
 * al. 2002 cubic approximations) -> XYZ -> linear sRGB.
 *
 * Deliberately NOT gamma-corrected here: the star shader works in linear space
 * like everything else, and applying the transfer function twice is what makes
 * a star field look like pastel confetti.
 */

import type { Linear } from './tokens'

/**
 * Ballesteros (2012), EPL 97, 34008. Valid across the range a magnitude-6
 * catalogue actually contains; the two extremes in ours are B-V = -0.34
 * (Spica, ~23000 K) and +3.46 (a carbon star, ~2000 K).
 */
export function bvToKelvin(bv: number): number {
  const b = Math.max(-0.4, Math.min(3.5, bv))
  return 4600 * (1 / (0.92 * b + 1.7) + 1 / (0.92 * b + 0.62))
}

/** Kim et al. (2002) cubic spline for the Planckian locus in CIE 1931 xy. */
function planckianXy(kelvin: number): [number, number] {
  const T = Math.max(1667, Math.min(25000, kelvin))
  const t = 1000 / T
  const t2 = t * t
  const t3 = t2 * t

  const x =
    T < 4000
      ? -0.2661239 * t3 - 0.2343589 * t2 + 0.8776956 * t + 0.179910
      : -3.0258469 * t3 + 2.1070379 * t2 + 0.2226347 * t + 0.240390

  const x2 = x * x
  const x3 = x2 * x
  let y: number
  if (T < 2222) y = -1.1063814 * x3 - 1.34811020 * x2 + 2.18555832 * x - 0.20219683
  else if (T < 4000) y = -0.9549476 * x3 - 1.37418593 * x2 + 2.09137015 * x - 0.16748867
  else y = 3.0817580 * x3 - 5.87338670 * x2 + 3.75112997 * x - 0.37001483

  return [x, y]
}

/** CIE xyY (Y = 1) to linear sRGB, sRGB primaries with a D65 white point. */
function xyToLinearRgb(x: number, y: number): Linear {
  if (y <= 1e-6) return [1, 1, 1]
  const X = x / y
  const Z = (1 - x - y) / y
  const r = 3.2404542 * X - 1.5371385 * 1 - 0.4985314 * Z
  const g = -0.969266 * X + 1.8760108 * 1 + 0.041556 * Z
  const b = 0.0556434 * X - 0.2040259 * 1 + 1.0572252 * Z

  // Clip to the sRGB gamut by desaturating toward white rather than by
  // clamping per channel. Clamping shifts hue: a deep-red star whose blue
  // channel goes negative becomes ORANGE, which is exactly the distinction
  // this function exists to preserve.
  const lowest = Math.min(r, g, b)
  const [r2, g2, b2] = lowest < 0 ? [r - lowest, g - lowest, b - lowest] : [r, g, b]
  const peak = Math.max(r2, g2, b2, 1e-6)
  return [r2 / peak, g2 / peak, b2 / peak]
}

const cache = new Map<number, Linear>()

/**
 * Linear-sRGB colour for a star of the given B-V, normalised to peak 1.
 *
 * Memoised on B-V rounded to 0.02 mag -- about 200 distinct colours for 5,044
 * stars, which is well under the resolution any display can show and turns
 * 5,044 cube roots into 200.
 */
export function bvToLinearRgb(bv: number): Linear {
  const key = Math.round(bv * 50)
  const hit = cache.get(key)
  if (hit) return hit
  const [x, y] = planckianXy(bvToKelvin(key / 50))
  const rgb = xyToLinearRgb(x, y)
  cache.set(key, rgb)
  return rgb
}
