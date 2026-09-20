/**
 * Slots, midpoints, and the 150-second lie.
 *
 * The backend evaluates all physics at slot **midpoints** -- `grid.mid_unix()`
 * is what astropy is handed -- while `grid.slotStarts` on the wire are slot
 * **boundaries**. At five-minute slots those differ by 150 seconds.
 *
 * Animate against `slotStarts` and everything is subtly, invisibly wrong: the
 * horizon leads the targets, the Moon leads both, and the instruction card
 * beside them disagrees with the sky by an amount too small to notice and too
 * large to be right. Nothing throws. Nothing looks broken.
 *
 * So the conversion lives here, once, and nowhere else. `slotStarts` is
 * permitted in exactly one place in this codebase -- drawing block boundaries
 * on the nightbar, where a boundary really is what is meant.
 *
 * The two questions this module distinguishes:
 *
 *   `containingSlot`  DISCRETE: "which five-minute block am I in?" Answers with
 *                     an index. Everything the backend indexes by slot -- the
 *                     plan, the warnings, the efficiency -- is asked this way.
 *
 *   `sampleSmooth`    CONTINUOUS: "where is the telescope pointing right now?"
 *                     Interpolates between midpoints. Its result is typed
 *                     `RenderOnly` and **cannot compile into a displayed
 *                     string**, because an interpolated altitude is a
 *                     rendering convenience and not a number the scheduler
 *                     ever saw.
 */

/** The grid, as the session payload carries it. */
export interface GridLike {
  readonly start: string
  readonly nSlots: number
  readonly slotSeconds: number
}

/**
 * A value that exists only to be drawn.
 *
 * Structurally opaque on purpose: a branded `number` would still be assignable
 * to `number` and would leak into `toFixed(1)` without complaint. This one has
 * to be unwrapped deliberately, which is the whole point -- the unwrap is the
 * moment someone decides an interpolated value is fit for the purpose.
 */
declare const RENDER_ONLY: unique symbol
export interface RenderOnly<T> {
  readonly [RENDER_ONLY]: T
}

/** Free at runtime; the cost is entirely in having to type it. */
export const forRender = <T,>(v: RenderOnly<T>): T => v as unknown as T
const asRenderOnly = <T,>(v: T): RenderOnly<T> => v as unknown as RenderOnly<T>

export interface SlotClock {
  readonly startMs: number
  readonly slotMs: number
  readonly nSlots: number
  readonly endMs: number
}

export function slotClock(grid: GridLike): SlotClock {
  const startMs = Date.parse(grid.start)
  const slotMs = grid.slotSeconds * 1000
  return { startMs, slotMs, nSlots: grid.nSlots, endMs: startMs + slotMs * grid.nSlots }
}

/** Epoch ms of slot `i`'s LEADING EDGE. Boundaries only -- see the module note. */
export const slotStartMs = (c: SlotClock, i: number): number => c.startMs + c.slotMs * i

/** Epoch ms of slot `i`'s MIDPOINT: the instant its numbers describe. */
export const slotMidMs = (c: SlotClock, i: number): number => c.startMs + c.slotMs * (i + 0.5)

export const clampSlot = (c: SlotClock, i: number): number =>
  i < 0 ? 0 : i >= c.nSlots ? c.nSlots - 1 : i

/**
 * Which slot contains `tMs`. Clamped, never out of range.
 *
 * Clamping rather than throwing is deliberate: the cursor is allowed to rest on
 * either end of the night, and a slider that throws at its own extremes is a
 * slider nobody can use.
 */
export function containingSlot(c: SlotClock, tMs: number): number {
  return clampSlot(c, Math.floor((tMs - c.startMs) / c.slotMs))
}

/** Fractional position along the night in [0, 1]. */
export function nightFraction(c: SlotClock, tMs: number): number {
  const span = c.endMs - c.startMs
  return span <= 0 ? 0 : Math.max(0, Math.min(1, (tMs - c.startMs) / span))
}

export const fractionToMs = (c: SlotClock, f: number): number =>
  c.startMs + (c.endMs - c.startMs) * Math.max(0, Math.min(1, f))

/**
 * The pair of midpoints bracketing `tMs`, and how far between them we are.
 *
 * Outside the first and last midpoints this pins to the end rather than
 * extrapolating. Extrapolating an altitude past the end of the night produces
 * numbers below the horizon and above the zenith, which then propagate into a
 * `Math.asin` and arrive as NaN several layers away from the cause.
 */
export function midpointSpan(c: SlotClock, tMs: number): { a: number; b: number; f: number } {
  const x = (tMs - c.startMs) / c.slotMs - 0.5
  if (x <= 0) return { a: 0, b: 0, f: 0 }
  if (x >= c.nSlots - 1) return { a: c.nSlots - 1, b: c.nSlots - 1, f: 0 }
  const a = Math.floor(x)
  return { a, b: a + 1, f: x - a }
}

/**
 * Linear interpolation of a per-slot array against the MIDPOINT axis.
 *
 * For continuous quantities only. Never use it for an angle that wraps --
 * azimuth interpolated from 359 to 1 sweeps the long way round the compass.
 * Interpolate those as unit vectors; `MoonMarker` and `SlewPath` both do.
 */
export function sampleSmooth(
  c: SlotClock,
  values: ArrayLike<number>,
  tMs: number,
): RenderOnly<number> {
  const { a, b, f } = midpointSpan(c, tMs)
  const va = values[a] ?? 0
  const vb = values[b] ?? va
  return asRenderOnly(va + (vb - va) * f)
}

/**
 * The backend's own value for the slot containing `tMs`, and which slot that
 * was. This is what displayed numbers come from.
 *
 * Returning the slot alongside the value is not decoration: a card reading
 * "64 degrees" is only checkable if you can also say which five minutes it
 * refers to, and the slot index is the join key every other panel uses.
 */
export function sampleSlot(
  c: SlotClock,
  values: ArrayLike<number>,
  tMs: number,
): { value: number; slot: number } {
  const slot = containingSlot(c, tMs)
  return { value: values[slot] ?? 0, slot }
}

/** Quantise an instant to its slot midpoint. The cursor's React-visible tick. */
export const quantiseToSlot = (c: SlotClock, tMs: number): number =>
  containingSlot(c, tMs)
