/**
 * The rig: a telescope preset, a camera preset, and every figure the exposure
 * calculator reads, all editable.
 *
 * Four rules shape this file.
 *
 *  1. This file holds inputs and nothing else. It derives no optics figures
 *     and shows none: resolving power, pixel scale, field of view and the
 *     rest are the scheduler's business, not something to read while you are
 *     still typing. The focal-ratio field is an input like any other, and
 *     typing f/6.3 sets focal length = 6.3 x aperture.
 *  2. People type freely. A field keeps its own string while it is being
 *     edited, so "", "0." and "8" all survive mid-keystroke. Only a valid,
 *     in-range number (the pydantic bounds in api/schemas.py) reaches the
 *     request. A field left invalid stays marked, and the request keeps its
 *     last good value.
 *  3. A modified preset stops claiming to be one. telescopeId and cameraId
 *     are sent only while that part still matches its preset, because the
 *     server names the rig's parts from those ids.
 *  4. A part can have no preset at all. "Custom" empties every field in its
 *     half and leaves them empty, which is the one honest starting point for
 *     a telescope this list has never heard of.
 *
 * Rule 4 is the awkward one, because there is no blank number. The request
 * type has a `number` in every slot and always will, so an empty field exists
 * ONLY as a draft string of "" sitting over a rig that still carries whatever
 * numbers were last committed. Two consequences are handled explicitly rather
 * than left to luck, because both would otherwise put a figure from the
 * discarded preset on screen with nothing to say where it came from:
 *
 *  - `blanks` remembers which fields are empty by design, so Escape in one of
 *    them empties it again instead of "reverting" it to the stale number.
 *  - a derived field (the focal ratio) stays empty until everything it is
 *    derived from has been filled in; see LINKS and DERIVED_FROM.
 *
 * Every empty field reads as `required` and is reported through
 * `onInvalidChange`, so a half-built custom rig cannot be submitted and
 * cannot be ranked against -- the parent holds the target list back too.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  type CameraPreset,
  type EquipmentRequest,
  type Presets,
  type TelescopePreset,
} from '../../api/client'
import '../../styles/equipment.css'

type Mount = Presets['defaultMount']
/** A request whose mount is always present, which is what this form holds. */
type Rig = EquipmentRequest & { mount: Mount }

// --------------------------------------------------------------------------
// presets -> requests. Setup builds its initial state with these.
// --------------------------------------------------------------------------

/** The two halves separately: either one can be absent, for a custom part. */
const opticsFrom = (tel: TelescopePreset): Rig['optics'] => ({
  apertureMm: tel.apertureMm,
  focalLengthMm: tel.focalLengthMm,
  reducer: tel.reducer,
  centralObstructionMm: tel.centralObstructionMm,
  throughput: tel.throughput,
})

const cameraFrom = (cam: CameraPreset): Rig['camera'] => ({
  pixelSizeUm: cam.pixelSizeUm,
  sensorWidthPx: cam.sensorWidthPx,
  sensorHeightPx: cam.sensorHeightPx,
  quantumEfficiency: cam.quantumEfficiency,
  readNoiseE: cam.readNoiseE,
  darkCurrentEPerS: cam.darkCurrentEPerS,
  readoutS: cam.readoutS,
})

export function requestFrom(tel: TelescopePreset, cam: CameraPreset, mount: Mount): Rig {
  return {
    name: `${tel.name} + ${cam.name}`,
    telescopeId: tel.id,
    cameraId: cam.id,
    optics: opticsFrom(tel),
    camera: cameraFrom(cam),
    mount: {
      switchMinutes: mount.switchMinutes,
      minBlockMinutes: mount.minBlockMinutes,
    },
  }
}

/**
 * The rig each half is measured against. A half with no preset is measured
 * against itself, so nothing in it is ever marked as differing from a
 * telescope that was never chosen.
 */
const baseline = (r: Rig, tel: TelescopePreset | null, cam: CameraPreset | null): Rig => ({
  ...r,
  ...(tel ? { optics: opticsFrom(tel) } : {}),
  ...(cam ? { camera: cameraFrom(cam) } : {}),
})

export function defaultEquipment(presets: Presets): EquipmentRequest {
  const tel = pick(presets.telescopes, presets.defaultTelescopeId, presets.defaultTelescopeId)
  const cam = pick(presets.cameras, presets.defaultCameraId, presets.defaultCameraId)
  return requestFrom(tel, cam, presets.defaultMount)
}

function pick<T extends { id: string }>(xs: T[], id: string | null | undefined, fallback: string): T {
  return xs.find((x) => x.id === id) ?? xs.find((x) => x.id === fallback) ?? xs[0]!
}

// --------------------------------------------------------------------------
// fields
// --------------------------------------------------------------------------

type Key =
  | 'aperture'
  | 'focal'
  | 'ratio'
  | 'reducer'
  | 'obstruction'
  | 'throughput'
  | 'pixel'
  | 'width'
  | 'height'
  | 'qe'
  | 'readNoise'
  | 'dark'
  | 'readout'
  | 'switch'
  | 'minBlock'

/** Strings being typed, per field. A field without one shows its committed value. */
type Drafts = Partial<Record<Key, string>>

/**
 * Whether a field currently holds a value at all.
 *
 * Only ever false on a custom part, where "Custom" emptied the box and
 * nothing has been typed into it since. Every cross-field rule below consults
 * it, because the rig underneath an empty box still carries the discarded
 * preset's number: without this, an 80 mm refractor is rejected for having an
 * aperture smaller than a Celestron C8's secondary, and the figure it is
 * measured against is nowhere on the page.
 */
type Known = (k: Key) => boolean

interface Spec {
  label: string
  /** The committed value, in the units the field shows (percent, not fraction). */
  get: (r: Rig) => number
  /** Write a valid shown-units value back into the rig. */
  set: (r: Rig, n: number) => Rig
  /**
   * Why `n` cannot be sent, or null. Mirrors the pydantic bounds exactly, and
   * for a rule that reads another field, asks `known` before reading it.
   */
  check: (n: number, r: Rig, known: Known) => string | null
  /** Decimals shown once the field is no longer being typed in. */
  digits: number
  int?: boolean
}

const withOptics = (r: Rig, o: Partial<Rig['optics']>): Rig => ({
  ...r,
  optics: { ...r.optics, ...o },
})
const withCamera = (r: Rig, c: Partial<Rig['camera']>): Rig => ({
  ...r,
  camera: { ...r.camera, ...c },
})
const withMount = (r: Rig, m: Partial<Mount>): Rig => ({ ...r, mount: { ...r.mount, ...m } })

/** The bound as pydantic enforces it: `gt` is ">", `ge` is "≥". */
function bounds(lo: number, hi: number, unit: string, loOpen = false) {
  const said =
    hi === Infinity
      ? `must be ≥ ${lo}${unit}`
      : loOpen
        ? `must be > ${lo} and ≤ ${hi}${unit}`
        : `must be ${lo} to ${hi}${unit}`
  return (n: number) => ((loOpen ? n > lo : n >= lo) && n <= hi ? null : said)
}

const FOCAL_MIN = 20 // exclusive, as OpticsRequest.focal_length_mm
const FOCAL_MAX = 30_000
const round1 = (x: number) => Math.round(x * 10) / 10

const SPECS: Record<Key, Spec> = {
  aperture: {
    label: 'Aperture',
    get: (r) => r.optics.apertureMm,
    set: (r, n) => withOptics(r, { apertureMm: n }),
    // Cross-field as well as bounds: the server rejects an obstruction that
    // fills the aperture, and a rejected rig would 422 the ranking and Create.
    check: (n, r, known) =>
      bounds(10, 2000, ' mm', true)(n) ??
      (!known('obstruction') || n > r.optics.centralObstructionMm
        ? null
        : `must be larger than the ${r.optics.centralObstructionMm} mm obstruction`),
    digits: 1,
  },
  focal: {
    label: 'Focal length',
    get: (r) => r.optics.focalLengthMm,
    set: (r, n) => withOptics(r, { focalLengthMm: n }),
    check: bounds(FOCAL_MIN, FOCAL_MAX, ' mm', true),
    digits: 1,
  },
  ratio: {
    // The one number this file derives, and only because it is an INPUT.
    label: 'Focal ratio',
    get: (r) => r.optics.focalLengthMm / r.optics.apertureMm,
    set: (r, n) => withOptics(r, { focalLengthMm: round1(n * r.optics.apertureMm) }),
    check: (n, r, known) => {
      if (!(n > 0)) return 'must be > 0'
      // f/ SETS the focal length, as aperture x ratio. With no aperture there
      // is nothing to multiply, and the only number available is the one
      // belonging to the preset "Custom" replaced -- so this is refused
      // rather than committed from it.
      if (!known('aperture')) return 'needs an aperture first'
      const fl = round1(n * r.optics.apertureMm)
      return fl > FOCAL_MIN && fl <= FOCAL_MAX
        ? null
        : `focal length would leave ${FOCAL_MIN} to ${FOCAL_MAX} mm`
    },
    digits: 2,
  },
  reducer: {
    label: 'Reducer/Barlow',
    get: (r) => r.optics.reducer,
    set: (r, n) => withOptics(r, { reducer: n }),
    check: bounds(0.2, 5, ''),
    digits: 3,
  },
  obstruction: {
    label: 'Obstruction',
    get: (r) => r.optics.centralObstructionMm,
    set: (r, n) => withOptics(r, { centralObstructionMm: n }),
    check: (n, r, known) =>
      bounds(0, Infinity, ' mm')(n) ??
      (!known('aperture') || n < r.optics.apertureMm
        ? null
        : `must be smaller than the ${r.optics.apertureMm} mm aperture`),
    digits: 1,
  },
  throughput: {
    label: 'Throughput',
    get: (r) => r.optics.throughput * 100,
    set: (r, n) => withOptics(r, { throughput: n / 100 }),
    check: bounds(0, 100, '%', true),
    digits: 1,
  },
  pixel: {
    label: 'Pixel size',
    get: (r) => r.camera.pixelSizeUm,
    set: (r, n) => withCamera(r, { pixelSizeUm: n }),
    check: bounds(0.5, 30, ' µm', true),
    digits: 2,
  },
  width: {
    label: 'Sensor width',
    get: (r) => r.camera.sensorWidthPx,
    set: (r, n) => withCamera(r, { sensorWidthPx: n }),
    check: bounds(64, 50_000, ' px'),
    digits: 0,
    int: true,
  },
  height: {
    label: 'Sensor height',
    get: (r) => r.camera.sensorHeightPx,
    set: (r, n) => withCamera(r, { sensorHeightPx: n }),
    check: bounds(64, 50_000, ' px'),
    digits: 0,
    int: true,
  },
  qe: {
    label: 'Effective QE',
    get: (r) => r.camera.quantumEfficiency * 100,
    set: (r, n) => withCamera(r, { quantumEfficiency: n / 100 }),
    check: bounds(0, 100, '%', true),
    digits: 1,
  },
  readNoise: {
    label: 'Read noise',
    get: (r) => r.camera.readNoiseE,
    set: (r, n) => withCamera(r, { readNoiseE: n }),
    check: bounds(0, 50, ' e⁻'),
    digits: 2,
  },
  dark: {
    label: 'Dark current',
    get: (r) => r.camera.darkCurrentEPerS,
    set: (r, n) => withCamera(r, { darkCurrentEPerS: n }),
    check: bounds(0, 5, ' e⁻/s'),
    digits: 5,
  },
  readout: {
    label: 'Download',
    get: (r) => r.camera.readoutS,
    set: (r, n) => withCamera(r, { readoutS: n }),
    check: bounds(0, 120, ' s'),
    digits: 3,
  },
  switch: {
    label: 'Target change',
    get: (r) => r.mount.switchMinutes,
    set: (r, n) => withMount(r, { switchMinutes: n }),
    check: bounds(0, 60, ' min'),
    digits: 1,
  },
  minBlock: {
    label: 'Minimum block',
    get: (r) => r.mount.minBlockMinutes,
    set: (r, n) => withMount(r, { minBlockMinutes: n }),
    check: bounds(1, 240, ' min'),
    digits: 1,
  },
}

/** The tube itself. The reducer is part of the telescope too, but named apart. */
const TUBE_KEYS: readonly Key[] = ['aperture', 'focal', 'obstruction', 'throughput']
const TEL_KEYS: readonly Key[] = [...TUBE_KEYS, 'ratio', 'reducer']
const CAM_KEYS: readonly Key[] = ['pixel', 'width', 'height', 'qe', 'readNoise', 'dark', 'readout']

/** What "Custom" empties: every measurement, but not the reducer -- see below. */
const BLANK_TEL_KEYS: readonly Key[] = [...TUBE_KEYS, 'ratio']

const emptied = (keys: readonly Key[]): Drafts =>
  Object.fromEntries(keys.map((k) => [k, ''])) as Drafts

const marked = (keys: readonly Key[]): Partial<Record<Key, true>> =>
  Object.fromEntries(keys.map((k) => [k, true])) as Partial<Record<Key, true>>

/** Typing a valid value here discards a stale draft there. */
const LINKS: Partial<Record<Key, readonly Key[]>> = {
  aperture: ['ratio'],
  focal: ['ratio'],
  ratio: ['focal'],
}

/**
 * What a linked field is computed from, for the one case where discarding its
 * draft would be wrong: an EMPTY one.
 *
 * On a custom telescope every box starts empty, and f/ is empty because it
 * cannot be anything else -- there is no aperture and no focal length yet.
 * Typing an aperture alone must not fill it in, because the only number
 * available then is `stale focal length / new aperture`, which belongs to the
 * preset the user just walked away from. So an empty derived field is left
 * empty until every field it is derived from has been given a value.
 */
const DERIVED_FROM: Partial<Record<Key, readonly Key[]>> = {
  ratio: ['aperture', 'focal'],
  focal: ['aperture', 'ratio'],
}

const NUMBER = /^\+?(?:\d+\.?\d*|\.\d+)$/
const INTEGER = /^\+?\d+$/

type Read = { ok: true; n: number } | { ok: false; why: string }

function read(raw: string, spec: Spec, r: Rig, known: Known): Read {
  const s = raw.trim()
  if (s === '') return { ok: false, why: 'required' }
  if (!(spec.int ? INTEGER : NUMBER).test(s))
    return { ok: false, why: spec.int ? 'a whole number' : 'not a number' }
  const n = Number(s)
  const why = spec.check(n, r, known)
  return why === null ? { ok: true, n } : { ok: false, why }
}

/** A committed value as the field shows it: at most `digits` decimals. */
const show = (n: number, digits: number) => String(Number(n.toFixed(digits)))

const near = (a: number, b: number) =>
  Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b))

function without<T extends Partial<Record<Key, unknown>>>(d: T, keys: readonly Key[]): T {
  if (!keys.some((k) => k in d)) return d
  const next = { ...d }
  for (const k of keys) delete next[k]
  return next
}

// --------------------------------------------------------------------------
// provenance
// --------------------------------------------------------------------------

function differs(r: Rig, ref: Rig, keys: readonly Key[]): boolean {
  return keys.some((k) => !near(SPECS[k].get(r), SPECS[k].get(ref)))
}

/** What a part with no preset behind it is called, here and in Specifications. */
const CUSTOM_TELESCOPE = 'Custom telescope'
const CUSTOM_CAMERA = 'Custom camera'

/**
 * Ids and a name that tell the truth about how far the rig has moved from its
 * presets. The server names the parts from the ids, so a part that no longer
 * matches its preset loses its id rather than keep borrowing the preset's name
 * -- and a part that never had one loses it from the start. A null preset here
 * is not "unchanged": it is "there was nothing to change".
 */
function finish(r: Rig, tel: TelescopePreset | null, cam: CameraPreset | null): Rig {
  const ref = baseline(r, tel, cam)
  const tube = tel !== null && differs(r, ref, TUBE_KEYS)
  const reducer = tel !== null && differs(r, ref, ['reducer'])
  const camera = cam !== null && differs(r, ref, CAM_KEYS)
  // A preset row that already names its reducer ("... + 0.85× reducer f/4.9")
  // is named from its tube once the reducer changes, or the label would
  // carry two reducers.
  const scope =
    tel === null
      ? CUSTOM_TELESCOPE + (near(r.optics.reducer, 1) ? '' : reducerPhrase(r.optics.reducer))
      : reducer
        ? tubeName(tel) + (tube ? ' (modified)' : '') + reducerPhrase(r.optics.reducer)
        : tel.name + (tube ? ' (modified)' : '')
  const camera_ = cam === null ? CUSTOM_CAMERA : cam.name + (camera ? ' (modified)' : '')
  return {
    ...r,
    telescopeId: tel === null || tube || reducer ? null : tel.id,
    cameraId: cam === null || camera ? null : cam.id,
    telescopeName: tel === null || tube || reducer ? scope : null,
    cameraName: cam === null || camera ? camera_ : null,
    name: `${scope} + ${camera_}`,
  }
}

/** "Sky-Watcher Evostar 72ED + 0.85× reducer f/4.9" -> "Sky-Watcher Evostar 72ED". */
const tubeName = (t: TelescopePreset) =>
  near(t.reducer, 1) ? t.name : t.name.replace(/\s*\+\s*[\d.]+×.*$/, '')

const reducerPhrase = (x: number) =>
  near(x, 1) ? ', no reducer' : ` with ×${show(x, 3)} ${x < 1 ? 'reducer' : 'Barlow'}`

// --------------------------------------------------------------------------
// preset lists
// --------------------------------------------------------------------------

/** Optgroup label and a singular, in the order the picker lists them. */
const DESIGNS: Record<string, readonly [string, string]> = {
  refractor: ['Refractors', 'refractor'],
  sct: ['Schmidt-Cassegrains', 'Schmidt-Cassegrain'],
  'aplanatic-sct': ['Aplanatic SCTs', 'aplanatic SCT'],
  rasa: ['Rowe-Ackermann astrographs', 'RASA astrograph'],
  newtonian: ['Newtonians', 'Newtonian'],
  'ritchey-chretien': ['Ritchey-Chrétiens', 'Ritchey-Chrétien'],
  other: ['Other designs', 'telescope'],
}
const DESIGN_ORDER = Object.keys(DESIGNS)

const titleCase = (s: string) => {
  const t = s.replace(/[-_]+/g, ' ')
  return t.charAt(0).toUpperCase() + t.slice(1)
}
const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

const designGroup = (d: string) => DESIGNS[d]?.[0] ?? titleCase(d)
const designName = (d: string) => DESIGNS[d]?.[1] ?? d.replace(/[-_]+/g, ' ')

/** Grouped by design, known designs in a fixed order, unknown ones before "other". */
function byDesign(ts: TelescopePreset[]): [string, TelescopePreset[]][] {
  const groups = new Map<string, TelescopePreset[]>()
  for (const t of ts) {
    const g = groups.get(t.design)
    if (g) g.push(t)
    else groups.set(t.design, [t])
  }
  const rank = (d: string) => {
    const i = DESIGN_ORDER.indexOf(d)
    return i >= 0 ? i : DESIGN_ORDER.indexOf('other') - 0.5
  }
  return [...groups.entries()].sort(([a], [b]) => rank(a) - rank(b))
}

function bySensor(cs: CameraPreset[]): [string, CameraPreset[]][] {
  const groups: [string, CameraPreset[]][] = [
    ['Mono', cs.filter((c) => !c.color)],
    ['Colour (one-shot)', cs.filter((c) => c.color)],
  ]
  return groups.filter(([, xs]) => xs.length > 0)
}

const REDUCERS: readonly { x: number; label: string; kind: 'none' | 'reducer' | 'barlow' }[] = [
  { x: 1, label: 'None ×1', kind: 'none' },
  { x: 0.8, label: '×0.8 reducer', kind: 'reducer' },
  { x: 0.7, label: '×0.7 reducer', kind: 'reducer' },
  { x: 0.63, label: '×0.63 reducer', kind: 'reducer' },
  { x: 0.5, label: '×0.5 reducer', kind: 'reducer' },
  { x: 2, label: '×2 Barlow', kind: 'barlow' },
  { x: 3, label: '×3 Barlow', kind: 'barlow' },
]
const CUSTOM = 'custom'
/** A preset's own factor when it is not one of the common ones (0.845, 0.733...). */
const PRESET = 'preset'

/**
 * The telescope and camera selects' "no preset at all" option.
 *
 * Deliberately not the reducer's `CUSTOM` above, and deliberately not a plain
 * word: these three selects are read by different code, and an id that could
 * one day belong to a real preset would silently become unselectable.
 */
const NO_PRESET = '__custom__'

// --------------------------------------------------------------------------
// the editor
// --------------------------------------------------------------------------

/**
 * The presets a rig's parts started from. The request cannot say, once a part
 * is modified, so a parent that re-mounts this editor on a rig it kept (Setup,
 * after "New night") hands this back with it.
 *
 * `null` is a third answer and not a missing one: that part is custom, and
 * came from no preset. Without it a rebuilt editor would reopen a custom
 * telescope showing the default preset selected, every field flagged as
 * differing from it, and a "reset to preset" button that would erase the lot.
 */
export type RigOrigin = { telescopeId: string | null; cameraId: string | null }

export function EquipmentEditor({
  presets,
  value,
  origin,
  onChange,
  onInvalidChange,
}: {
  presets: Presets
  value: EquipmentRequest
  /** Read once, on mount, for a part whose id `value` no longer carries. */
  origin?: RigOrigin
  onChange: (next: EquipmentRequest, origin: RigOrigin) => void
  /** The labels of the fields showing an error, whenever that set changes. */
  onInvalidChange?: (labels: string[]) => void
}) {
  const uid = useId()
  const rig: Rig = { ...value, mount: value.mount ?? presets.defaultMount }

  // The preset each part STARTED from. Kept here because the request drops
  // the id once the part is modified, and "reset to preset" still needs it.
  // Without `origin`, a modified part would come back measured against the
  // default preset: "edited" everywhere, and renamed after the wrong one.
  // An `origin` that exists and carries a null is the custom case, and is the
  // only way to tell it apart from "this part was never touched": `value`
  // drops the id for a modified preset and for a custom part alike.
  const [telId, setTelId] = useState(() =>
    value.telescopeId ?? (origin ? (origin.telescopeId ?? NO_PRESET) : presets.defaultTelescopeId),
  )
  const [camId, setCamId] = useState(() =>
    value.cameraId ?? (origin ? (origin.cameraId ?? NO_PRESET) : presets.defaultCameraId),
  )
  const tel =
    telId === NO_PRESET ? null : pick(presets.telescopes, telId, presets.defaultTelescopeId)
  const cam = camId === NO_PRESET ? null : pick(presets.cameras, camId, presets.defaultCameraId)

  const [drafts, setDrafts] = useState<Drafts>({})
  // Which fields are empty BY DESIGN -- "Custom" emptied them and nothing has
  // been typed into them since. Not derivable from `drafts`, which cannot tell
  // an untouched empty box from one the user just cleared, and the two have to
  // behave differently under Escape.
  const [blanks, setBlanks] = useState<Partial<Record<Key, true>>>({})
  const [customReducer, setCustomReducer] = useState(false)

  const ref = baseline(rig, tel, cam)
  const edited = (k: Key) => !near(SPECS[k].get(rig), SPECS[k].get(ref))
  const telEdited = tel !== null && TEL_KEYS.some(edited)
  const camEdited = cam !== null && CAM_KEYS.some(edited)

  const commit = (next: Rig, t = tel, c = cam) =>
    onChange(finish(next, t, c), { telescopeId: t?.id ?? null, cameraId: c?.id ?? null })

  /**
   * Which fields hold a value. Empty means empty: a box that "Custom" cleared
   * and nobody has filled in has NO number, whatever the rig underneath still
   * carries from the preset that was discarded. Every cross-field rule asks
   * this before reading a sibling -- see `Known`.
   */
  // Trimmed, because `read` trims: a box holding only spaces is refused as
  // `required`, and a field that cannot be read is not a field another rule
  // may measure against. No draft at all means the committed value is showing,
  // which is always a real number.
  const knownIn =
    (d: Drafts): Known =>
    (k) =>
      d[k]?.trim() !== ''
  const known = knownIn(drafts)

  const whyNot = (k: Key): string | null => {
    const raw = drafts[k]
    if (raw === undefined) return null
    const got = read(raw, SPECS[k], rig, known)
    return got.ok ? null : got.why
  }

  const typeInto = (k: Key, raw: string) => {
    // Against the drafts as they will be once this keystroke lands: typing
    // into an empty box fills it, so a rule that reads this field must see it
    // as filled rather than as the blank it was a moment ago.
    const after: Drafts = { ...drafts, [k]: raw }
    const got = read(raw, SPECS[k], rig, knownIn(after))
    if (!got.ok) {
      setDrafts(after)
      return
    }
    // Aperture and obstruction are checked against each other, so this value
    // can make another field's rejected draft valid. Apply those as well:
    // otherwise that field shows a number, unmarked, that the request (and
    // every figure computed from it) does not carry.
    const stale = LINKS[k] ?? []
    let next = SPECS[k].set(rig, got.n)
    const applied: Key[] = []
    for (const j of Object.keys(SPECS) as Key[]) {
      const pending = drafts[j]
      if (j === k || stale.includes(j) || pending === undefined) continue
      const r = read(pending, SPECS[j], next, knownIn(after))
      if (r.ok && !near(SPECS[j].get(next), r.n)) {
        next = SPECS[j].set(next, r.n)
        applied.push(j)
      }
    }
    // Every field that now holds a value stops being empty by design: this
    // one, the ones the loop above just applied, and a derived field once its
    // LAST input has been filled.
    const filled = { ...blanks }
    delete filled[k]
    for (const j of applied) delete filled[j]
    const dropped = stale.filter(
      (j) => filled[j] === undefined || (DERIVED_FROM[j] ?? []).every((f) => filled[f] === undefined),
    )
    for (const j of dropped) delete filled[j]
    setBlanks(filled)
    setDrafts(without(after, [...dropped, ...applied]))
    commit(next)
  }

  /** Leaving a valid field tidies it to its committed value; an invalid one stays marked. */
  const settle = (k: Key) =>
    setDrafts((d) => {
      const raw = d[k]
      return raw !== undefined && read(raw, SPECS[k], rig, knownIn(d)).ok ? without(d, [k]) : d
    })

  /**
   * Escape: back to the committed value -- except in a field that is empty by
   * design, where there is no committed value worth going back to. The number
   * under it belongs to the preset "Custom" replaced, and quietly filling it
   * in is exactly the borrowed figure this editor exists to avoid.
   */
  const revert = (k: Key) =>
    setDrafts((d) => (blanks[k] ? { ...d, [k]: '' } : without(d, [k])))

  const chooseTelescope = (t: TelescopePreset) => {
    setTelId(t.id)
    setCustomReducer(false)
    setDrafts((d) => without(d, TEL_KEYS))
    setBlanks((b) => without(b, TEL_KEYS))
    commit({ ...rig, optics: opticsFrom(t) }, t, cam)
  }

  const chooseCamera = (c: CameraPreset) => {
    setCamId(c.id)
    setDrafts((d) => without(d, CAM_KEYS))
    setBlanks((b) => without(b, CAM_KEYS))
    commit({ ...rig, camera: cameraFrom(c) }, tel, c)
  }

  /**
   * A telescope this list has never heard of: every figure emptied, and the
   * reducer back to none.
   *
   * The reducer is the one field not emptied, because it is a choice from a
   * list rather than a measurement -- "none" is a real answer and an empty
   * dropdown is not. The rig underneath keeps the numbers it had; they are
   * simply no longer shown, cannot be typed over accidentally (see `revert`),
   * and cannot be submitted, because every empty field reads as `required`.
   */
  const chooseCustomTelescope = () => {
    setTelId(NO_PRESET)
    setCustomReducer(false)
    setDrafts((d) => ({ ...without(d, ['reducer']), ...emptied(BLANK_TEL_KEYS) }))
    setBlanks((b) => ({ ...b, ...marked(BLANK_TEL_KEYS) }))
    commit(SPECS.reducer.set(rig, 1), null, cam)
  }

  const chooseCustomCamera = () => {
    setCamId(NO_PRESET)
    setDrafts((d) => ({ ...d, ...emptied(CAM_KEYS) }))
    setBlanks((b) => ({ ...b, ...marked(CAM_KEYS) }))
    commit(rig, tel, null)
  }

  const chooseReducer = (v: string) => {
    if (v === CUSTOM) {
      setCustomReducer(true)
      return
    }
    setCustomReducer(false)
    setDrafts((d) => without(d, ['reducer']))
    // PRESET only renders while `ownReducer` is non-null, which requires a
    // preset; the fallback is unreachable and is here so the type says so.
    commit(SPECS.reducer.set(rig, v === PRESET ? (tel?.reducer ?? 1) : Number(v)))
  }

  const box = (
    k: Key,
    o: { prefix?: string; suffix?: string; label?: string; describedBy?: string },
  ) => {
    const id = `${uid}-${k}`
    const why = whyNot(k)
    return (
      <NumBox
        id={id}
        text={drafts[k] ?? show(SPECS[k].get(rig), SPECS[k].digits)}
        invalid={why !== null}
        integer={SPECS[k].int}
        prefix={o.prefix}
        suffix={o.suffix}
        label={o.label}
        describedBy={o.describedBy}
        onText={(s) => typeInto(k, s)}
        onSettle={() => settle(k)}
        onRevert={() => revert(k)}
      />
    )
  }

  const field = (k: Key, o: { prefix?: string; suffix?: string } = {}) => (
    <Field id={`${uid}-${k}`} label={SPECS[k].label} edited={edited(k)} error={whyNot(k)}>
      {box(k, {
        prefix: o.prefix,
        suffix: o.suffix,
        // Only ever the error node now: describing a field by an id nothing
        // renders is worse for a screen reader than describing it by nothing.
        describedBy: whyNot(k) ? `${uid}-${k}-hint` : undefined,
      })}
    </Field>
  )

  const standard = REDUCERS.find((o) => near(o.x, rig.optics.reducer))
  const ownReducer =
    tel === null || REDUCERS.some((o) => near(o.x, tel.reducer)) ? null : tel.reducer
  const reducerMode = customReducer
    ? CUSTOM
    : standard
      ? String(standard.x)
      : ownReducer !== null && near(rig.optics.reducer, ownReducer)
        ? PRESET
        : CUSTOM
  const sensorWhy = whyNot('width') ?? whyNot('height')

  const invalid = (Object.keys(drafts) as Key[]).filter((k) => whyNot(k) !== null)
  const invalidLabels = invalid.map((k) => SPECS[k].label)

  // The request keeps a rejected field's last good value, so the parent
  // cannot see the error from `value`; it is told, once per change of the set.
  // Held in a ref so an inline callback cannot turn this into a render loop.
  const reportInvalid = useRef(onInvalidChange)
  useEffect(() => {
    reportInvalid.current = onInvalidChange
  })
  const invalidKey = invalidLabels.join('\n')
  useEffect(() => {
    reportInvalid.current?.(invalidKey ? invalidKey.split('\n') : [])
  }, [invalidKey])

  return (
    <div className="eq">
      <div className="eq__pair">
        {/* -- telescope ------------------------------------------------ */}
        <div className="eq__group eq__group--tel">
          <div className="field">
            <div className="eq__head">
              <label htmlFor={`${uid}-tel`}>Telescope</label>
              {telEdited && tel && (
                <>
                  <span className="chip eq__edited">edited</span>
                  <button type="button" className="eq__reset" onClick={() => chooseTelescope(tel)}>
                    reset to preset
                  </button>
                </>
              )}
            </div>
            <select
              id={`${uid}-tel`}
              value={tel ? tel.id : NO_PRESET}
              onChange={(e) => {
                if (e.target.value === NO_PRESET) return chooseCustomTelescope()
                const t = presets.telescopes.find((x) => x.id === e.target.value)
                if (t) chooseTelescope(t)
              }}
            >
              {byDesign(presets.telescopes).map(([design, ts]) => (
                <optgroup key={design} label={designGroup(design)}>
                  {ts.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </optgroup>
              ))}
              {/* Outside every optgroup, as the reducer's own Custom is. */}
              <option value={NO_PRESET}>Custom&hellip;</option>
            </select>
            {tel && <PresetNote lead={capital(designName(tel.design))} notes={tel.notes} />}
          </div>

          <div className="eq__fields">
            {field('aperture', { suffix: 'mm' })}
            {field('focal', { suffix: 'mm' })}
            {field('ratio', { prefix: 'f/' })}

            <Field
              id={`${uid}-reducer-pick`}
              label={SPECS.reducer.label}
              edited={edited('reducer')}
              error={whyNot('reducer')}
            >
              <div className="eq-reducer">
                <select
                  id={`${uid}-reducer-pick`}
                  value={reducerMode}
                  aria-describedby={whyNot('reducer') ? `${uid}-reducer-pick-hint` : undefined}
                  onChange={(e) => chooseReducer(e.target.value)}
                >
                  {REDUCERS.filter((o) => o.kind === 'none').map((o) => (
                    <option key={o.x} value={String(o.x)}>
                      {o.label}
                    </option>
                  ))}
                  <optgroup label="Reducers">
                    {REDUCERS.filter((o) => o.kind === 'reducer').map((o) => (
                      <option key={o.x} value={String(o.x)}>
                        {o.label}
                      </option>
                    ))}
                  </optgroup>
                  <optgroup label="Barlows">
                    {REDUCERS.filter((o) => o.kind === 'barlow').map((o) => (
                      <option key={o.x} value={String(o.x)}>
                        {o.label}
                      </option>
                    ))}
                  </optgroup>
                  {ownReducer !== null && (
                    <optgroup label="This preset">
                      <option value={PRESET}>×{show(ownReducer, 3)}</option>
                    </optgroup>
                  )}
                  <option value={CUSTOM}>Custom…</option>
                </select>
                {reducerMode === CUSTOM &&
                  box('reducer', {
                    prefix: '×',
                    label: 'Custom focal-length factor',
                    describedBy: whyNot('reducer') ? `${uid}-reducer-pick-hint` : undefined,
                  })}
              </div>
            </Field>

            {field('obstruction', { suffix: 'mm' })}
            {field('throughput', { suffix: '%' })}
          </div>
        </div>

        {/* -- camera --------------------------------------------------- */}
        <div className="eq__group eq__group--cam">
          <div className="field">
            <div className="eq__head">
              <label htmlFor={`${uid}-cam`}>Camera</label>
              {camEdited && cam && (
                <>
                  <span className="chip eq__edited">edited</span>
                  <button type="button" className="eq__reset" onClick={() => chooseCamera(cam)}>
                    reset to preset
                  </button>
                </>
              )}
            </div>
            <select
              id={`${uid}-cam`}
              value={cam ? cam.id : NO_PRESET}
              onChange={(e) => {
                if (e.target.value === NO_PRESET) return chooseCustomCamera()
                const c = presets.cameras.find((x) => x.id === e.target.value)
                if (c) chooseCamera(c)
              }}
            >
              {bySensor(presets.cameras).map(([group, cs]) => (
                <optgroup key={group} label={group}>
                  {cs.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </optgroup>
              ))}
              <option value={NO_PRESET}>Custom&hellip;</option>
            </select>
            {cam && <PresetNote lead={cam.sensor} notes={cam.notes} />}
          </div>

          <div className="eq__fields">
            {field('pixel', { suffix: 'µm' })}
            <Field
              id={`${uid}-width`}
              label="Sensor"
              edited={edited('width') || edited('height')}
              error={sensorWhy}
              wide
            >
              <div className="eq-by">
                {box('width', {
                  label: 'Sensor width in pixels',
                  describedBy: sensorWhy ? `${uid}-width-hint` : undefined,
                })}
                <span className="eq-by__x" aria-hidden="true">
                  ×
                </span>
                {box('height', {
                  suffix: 'px',
                  label: 'Sensor height in pixels',
                  describedBy: sensorWhy ? `${uid}-width-hint` : undefined,
                })}
              </div>
            </Field>

            {field('qe', { suffix: '%' })}
            {field('readNoise', { suffix: 'e⁻' })}
            {field('dark', { suffix: 'e⁻/s' })}
            {field('readout', { suffix: 's' })}
          </div>
        </div>

        {/* -- mount ---------------------------------------------------- */}
        <div className="eq__group eq__group--mount">
          <div className="eq__sub">Mount and workflow</div>
          <div className="eq__fields">
            {field('switch', { suffix: 'min' })}
            {field('minBlock', { suffix: 'min' })}
          </div>
        </div>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------
// pieces
// --------------------------------------------------------------------------

/**
 * A labelled box, and under it the one thing worth saying: why this value
 * cannot be sent. The line under a field used to carry a gloss as well
 * ("clear diameter", "RMS at imaging gain"); the labels already say what the
 * fields are, and fifteen of those turned a rig into a page of reading.
 */
function Field({
  id,
  label,
  edited,
  error,
  wide,
  children,
}: {
  id: string
  label: string
  edited: boolean
  error: string | null
  wide?: boolean
  children: ReactNode
}) {
  return (
    <div className={`field eq-field${wide ? ' eq-field--wide' : ''}`}>
      <label htmlFor={id}>
        {label}
        {edited && (
          <span className="eq-dot" role="img" aria-label="edited" title="Differs from the preset" />
        )}
      </label>
      {children}
      {error && (
        <div className="field__hint eq-err" id={`${id}-hint`} role="status">
          {error}
        </div>
      )}
    </div>
  )
}

/**
 * A number the user types as text. `type="number"` would hand back "" for a
 * half-typed "0." in some browsers, spin on the scroll wheel, and hide what
 * is wrong; a text input with a decimal keypad does none of that.
 */
function NumBox({
  id,
  text,
  invalid,
  integer,
  prefix,
  suffix,
  label,
  describedBy,
  onText,
  onSettle,
  onRevert,
}: {
  id: string
  text: string
  invalid: boolean
  integer?: boolean
  prefix?: string
  suffix?: string
  label?: string
  describedBy?: string
  onText: (s: string) => void
  onSettle: () => void
  onRevert: () => void
}) {
  return (
    <div className={`eq-num${invalid ? ' eq-num--invalid' : ''}`}>
      {prefix && <span className="eq-num__affix eq-num__affix--pre">{prefix}</span>}
      <input
        id={id}
        className="num"
        type="text"
        inputMode={integer ? 'numeric' : 'decimal'}
        autoComplete="off"
        spellCheck={false}
        value={text}
        aria-label={label}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        onChange={(e) => onText(e.target.value)}
        onBlur={onSettle}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onRevert()
          else if (e.key === 'Enter') onSettle()
        }}
      />
      {suffix && <span className="eq-num__affix">{suffix}</span>}
    </div>
  )
}

/** Where a preset's figures came from. One line; click for the rest. */
function PresetNote({ lead, notes }: { lead: string; notes: string }) {
  const [open, setOpen] = useState(false)
  const text = [lead, notes].filter(Boolean).join(' · ')
  if (!text) return null
  return (
    <button
      type="button"
      className="field__hint eq-note"
      title={notes || undefined}
      aria-expanded={open}
      onClick={() => setOpen((o) => !o)}
    >
      <span className="eq-note__i" aria-hidden="true">
        ⓘ
      </span>
      <span className="eq-note__text">{text}</span>
    </button>
  )
}
