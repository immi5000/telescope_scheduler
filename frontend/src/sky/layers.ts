/**
 * What the sky can be asked to do differently. Free of any three.js import.
 *
 * This lives apart from `scene.ts` because the app shell needs the defaults,
 * and a value import from `scene.ts` would drag the whole renderer -- about
 * 600 KB of three.js -- into the main bundle and silently undo the lazy split.
 * (Measured once: 480 KB to 914 KB, no error, nothing visibly wrong.)
 *
 * Everything real is always drawn: the Milky Way, the stars, the constellation
 * figures, the planets, the Moon, the satellites, the ground, the targets and
 * the slew path. A dozen per-layer toggles asked people to curate a sky they
 * came to look at. What is left are two ways to make it easier to SEE:
 */
export interface SkyOptions {
  /**
   * Focus on the plan: fade the context -- the Milky Way, the constellation
   * figures, the faint stars and the satellites -- to about a third, so the
   * targets, the slew path and the altitude floor stand out.
   */
  focus: boolean
  /**
   * The alt-azimuth grid, altitude every 15 degrees and azimuth every 30, plus
   * the altitude floor: the frame the instruction card's "point ENE, 45 up"
   * is written in.
   */
  grid: boolean
}

export const DEFAULT_OPTIONS: SkyOptions = { focus: false, grid: false }

/**
 * What is selected in the sky. It lives HERE rather than in `scene.ts` for the
 * reason at the top of this file: the app shell compares selections, and
 * `sameSelection` is a VALUE, so importing it from `scene.ts` pulls three.js
 * into the eager graph and undoes the lazy split. The type alone would be
 * safe -- `import type` is erased -- but keeping the pair together is what
 * stops the next reader from reuniting them in the module that costs 600 KB.
 *
 * `scene.ts` re-exports both, so the consumers that import the TYPE from there
 * keep working unchanged.
 */
export type SkySelection =
  | { kind: 'target'; id: string }
  | { kind: 'star'; index: number }
  | { kind: 'planet'; id: string }
  | { kind: 'moon' }
  | { kind: 'satellite'; norad: number }
  | { kind: 'catalog'; id: string }

export const sameSelection = (a: SkySelection | null, b: SkySelection | null): boolean =>
  JSON.stringify(a) === JSON.stringify(b)

/**
 * Everything an add needs for an object the SERVER CANNOT NAME.
 *
 * Only stars need this. They live entirely in `public/sky/stars.bin` -- 41,411
 * Hipparcos records keyed by position in that file, of which only those
 * brighter than 6.5 are named at all -- so the server has nothing to look up
 * and the client must send coordinates. Planets and catalogue objects resolve
 * by name and never come through here.
 */
export interface PlannableSelection {
  id: string
  name: string
  raDeg: number
  decDeg: number
  /** Total V magnitude when `isPointSource`, else mag/arcsec^2. */
  magnitude: number
  isPointSource: boolean
}
