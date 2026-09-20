/**
 * The palette, in one place, in the form the GPU wants.
 *
 * This is TypeScript and not CSS because **custom properties do not reach
 * shaders**. A `var(--ink)` is a string that lives in the cascade; a uniform is
 * three floats uploaded to the card. Keeping the source of truth in CSS would
 * mean either `getComputedStyle` during a render loop -- which forces layout,
 * on every frame -- or a second hand-maintained copy of every colour, which is
 * the same thing as having no source of truth at all.
 *
 * Colours are stored as **linear-sRGB float triples**, because that is what
 * shaders blend in. Deriving the hex form from linear is exact; going the other
 * way invites someone to skip the transfer function, and a skipped transfer
 * function looks like a washed-out, slightly milky sky that nobody can quite
 * diagnose.
 *
 * Two exports, deliberately not one: `toCssVars()` writes `:root` at boot so
 * ordinary CSS keeps saying `var(--ink)`, and `toGlUniforms()` feeds the scene.
 */

/** A colour in linear sRGB, components in [0, 1] and occasionally beyond. */
export type Linear = readonly [number, number, number]

const srgbToLinear = (c: number): number =>
  c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)

const linearToSrgb = (c: number): number =>
  c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055

/** Authoring helper: write colours the way a designer reads them. */
export function hex(s: string): Linear {
  const v = s.replace('#', '')
  const n = parseInt(
    v.length === 3
      ? v
          .split('')
          .map((c) => c + c)
          .join('')
      : v,
    16,
  )
  return [
    srgbToLinear(((n >> 16) & 255) / 255),
    srgbToLinear(((n >> 8) & 255) / 255),
    srgbToLinear((n & 255) / 255),
  ]
}

export function toHex(c: Linear): string {
  const f = (x: number) =>
    Math.round(Math.max(0, Math.min(1, linearToSrgb(x))) * 255)
      .toString(16)
      .padStart(2, '0')
  return `#${f(c[0])}${f(c[1])}${f(c[2])}`
}

export function rgba(c: Linear, alpha: number): string {
  const f = (x: number) => Math.round(Math.max(0, Math.min(1, linearToSrgb(x))) * 255)
  return `rgba(${f(c[0])}, ${f(c[1])}, ${f(c[2])}, ${alpha})`
}

/**
 * Deep space under a planetarium dome.
 *
 * Deliberately absent, and each for a reason rather than a preference:
 *
 *   - **No saturated red.** The previous build had a monochrome red night
 *     mode. Red is the correct choice for preserving dark adaptation at an
 *     eyepiece and the wrong one for a screen someone is planning on, because
 *     it collapses every severity distinction onto luminance alone.
 *   - **No global brightness filter.** A CSS `filter` on `html` creates a
 *     containing block, which silently re-anchors every `position: fixed`
 *     descendant to that element instead of the viewport. That is what made
 *     the old toast layer drift.
 *   - **No pure black.** `#000` on an OLED panel makes every edge in the UI
 *     ring. The ground here is a very dark blue-violet, which reads as a dome
 *     rather than as a void.
 */
export const palette = {
  /** Page ground, and the two ends of its vertical gradient. */
  voidDeep: hex('#05060f'),
  voidNear: hex('#0b0d1d'),
  dome: hex('#111427'),

  /** Panel fills. Translucent in use; these are the solid bases. */
  panel: hex('#141830'),
  panelRaised: hex('#1b2040'),
  line: hex('#2a3054'),

  /** Type. Warm off-white against the cold ground, so it reads as lit.
   *
   *  `inkFaint` was #5f6785 and measured 2.9:1 against a panel over the sky --
   *  below AA for small text, and it is what every eyebrow, every column head
   *  and every fold label is set in, including the disclosure controls that
   *  hide the explanations. Lifted until it clears 4.5:1 (4.6:1 measured) while
   *  staying a visible step below `inkDim`, which measures 6.2:1. Do not raise
   *  it further: the three levels stop being three. */
  ink: hex('#ece7dd'),
  inkDim: hex('#9aa0bb'),
  inkFaint: hex('#7d88b0'),

  /** The plan itself: the slew path, the selected target, the cursor. */
  accent: hex('#7fd4e8'),
  accentDeep: hex('#2a8fa8'),

  /** The Moon, and warnings -- the same warm amber on purpose, because at 2am
   *  "the Moon is involved" and "be careful here" are usually the same fact. */
  moon: hex('#e8c88a'),
  warn: hex('#e0a552'),

  /** Critical only. Muted rose, never a fire-alarm red. */
  crit: hex('#d4788f'),

  /** Alert categories. Hue says WHAT kind of alert it is -- a forecast, a
   *  transient, a satellite -- and severity is carried separately, by weight
   *  and texture, so the two never compete for the same channel. Moonlight
   *  reuses `moon`; the rest are neutral. */
  alertWeather: hex('#7aa7ff'),
  alertTransient: hex('#c08cff'),
  alertSatellite: hex('#5fd6ae'),

  /** Sky-quality ramp, dark to bright. Used for the nightbar track and the
   *  twilight bands, so the control is coloured by the night's own shape. */
  twilightNight: hex('#0a0d1e'),
  twilightAstro: hex('#141a38'),
  twilightNautical: hex('#1d2a55'),
  twilightCivil: hex('#2e4276'),
  twilightDay: hex('#4a6ba8'),

  /** Below the horizon. Not black: the far side of the globe stays visible. */
  belowHorizon: hex('#070812'),
  ground: hex('#0d1020'),
} as const

export type PaletteKey = keyof typeof palette

/** CSS custom property name for a palette key: `accentDeep` -> `--accent-deep`. */
const cssName = (k: string) => '--' + k.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase())

/**
 * Publish the palette to CSS once, at boot.
 *
 * Every colour in `base.css` and `components.css` is a `var()`. Those files
 * contain no colour literals at all, which is what makes this module the
 * single source of truth rather than merely the largest copy of it.
 */
export function toCssVars(root: HTMLElement = document.documentElement): void {
  for (const [k, v] of Object.entries(palette)) {
    root.style.setProperty(cssName(k), toHex(v))
  }
  // A handful of derived values the stylesheets want but should not compute.
  //
  // Surfaces are near-black GLASS and edges are translucent INK, not a lighter
  // fill and a solid line. That is the whole elevation system: a card is a
  // pane over the sky plus a hairline, and nothing on this page casts a
  // shadow. Ink rather than pure white keeps the warm-on-cold relation the
  // palette is built on -- a white hairline over a blue-black ground reads as
  // a seam.
  //
  // These alphas are deliberately low. Contrast is bought with the heavy
  // backdrop blur in components.css, not with opacity, because a blurred
  // panel still carries the sky's own brightness and colour through it: a
  // card over the galactic centre is warmer than one over empty sky, which is
  // what makes the dock read as glass on the view rather than a sidebar over
  // a photograph. Raise these and that goes away.
  root.style.setProperty('--surface', rgba(palette.voidDeep, 0.58))
  root.style.setProperty('--surface-raised', rgba(palette.panel, 0.46))
  root.style.setProperty('--surface-solid', toHex(palette.voidNear))
  root.style.setProperty('--edge', rgba(palette.ink, 0.2))
  root.style.setProperty('--edge-soft', rgba(palette.ink, 0.11))
  root.style.setProperty('--edge-faint', rgba(palette.ink, 0.06))

  // Controls and fields. A control sits a STEP ABOVE the card it is on and a
  // field a step below it, because a row of hairline outlines on near-black
  // is elegant and unusable -- nothing looks pressable and nothing looks
  // fillable. Lift on hover, settle on press; the accent appears only when
  // something is actually on.
  root.style.setProperty('--control', rgba(palette.panelRaised, 0.55))
  root.style.setProperty('--control-hover', rgba(palette.panelRaised, 0.92))
  root.style.setProperty('--control-active', rgba(palette.panel, 0.95))
  root.style.setProperty('--control-on', rgba(palette.accent, 0.14))
  root.style.setProperty('--well', rgba(palette.voidDeep, 0.55))
  root.style.setProperty('--well-hover', rgba(palette.voidDeep, 0.75))

  // The same wash as `--control-on`, in ink rather than accent. The planning
  // sheet highlights with ink, because a form is not telemetry: a chip that is
  // on there means "you picked this", not "this reading is good", and a
  // coloured status light is the wrong word for a choice you just made.
  root.style.setProperty('--ink-wash', rgba(palette.ink, 0.12))

  root.style.setProperty('--panel-glass', rgba(palette.panel, 0.72))
  root.style.setProperty('--panel-raised-glass', rgba(palette.panelRaised, 0.78))
  root.style.setProperty('--line-soft', rgba(palette.line, 0.6))
  root.style.setProperty('--accent-glow', rgba(palette.accent, 0.28))
}

/** The subset the scene uploads as uniforms. */
export function toGlUniforms() {
  return {
    accent: palette.accent,
    accentDeep: palette.accentDeep,
    moon: palette.moon,
    warn: palette.warn,
    ink: palette.ink,
    inkDim: palette.inkDim,
    belowHorizon: palette.belowHorizon,
    ground: palette.ground,
    dome: palette.dome,
  } as const
}

/**
 * Twilight band colours, keyed by the backend's own band names.
 *
 * The nightbar track is painted with these, which is what lets it carry no
 * legend and no border: the shape of the night IS the control.
 */
export const twilightColor: Record<string, Linear> = {
  night: palette.twilightNight,
  astronomical: palette.twilightAstro,
  nautical: palette.twilightNautical,
  civil: palette.twilightCivil,
  day: palette.twilightDay,
}
