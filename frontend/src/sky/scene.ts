/**
 * The sky, from the ground. Imperative three.js behind one React boundary.
 *
 * The observer stands at the centre of the celestial sphere and looks OUT,
 * through a stereographic projection, over a skyline. Two frames, one rule:
 *
 *   ICRS      stars, constellation figures, the Milky Way, the targets, the
 *             observing route. Static buffers, rebuilt only when the plan changes.
 *   HORIZON   the ground, the grid, the Moon, the planets, the satellites --
 *             everything the backend describes in altitude and azimuth. These
 *             are children of one `rig` whose rotation is the backend's own
 *             per-slot frame quaternion.
 *
 * The camera rides the rig: its orientation is `rig * view`, where `view` is
 * the azimuth and altitude the person is looking at. So the landscape never
 * moves, the stars wheel past as time runs, and the client still performs no
 * astronomy of its own -- it rotates what the server computed, by the rotation
 * the server published, and `validateFrame` checks that rotation first.
 *
 * Rendering is on demand: nothing is drawn unless time, the view or the data
 * changed. A still sky at a dark site costs no battery at all.
 */

import {
  BufferAttribute,
  BufferGeometry,
  Color,
  Group,
  LineLoop,
  LineSegments,
  Matrix3,
  Matrix4,
  Mesh,
  PerspectiveCamera,
  Points,
  Quaternion,
  Scene,
  ShaderMaterial,
  SRGBColorSpace,
  Texture,
  Vector3,
  WebGLRenderer,
} from 'three'

import type { Geometry, Plan, Satellites, Session, Sky } from '../api/client'
import type { components } from '../api/schema'
import { cursor } from '../state/cursorStore'
import { bvToLinearRgb } from '../theme/starColor'
import { palette, type Linear } from '../theme/tokens'
import { midpointSpan, slotClock, type SlotClock } from '../time/slots'
import type { SkyAssets } from './data'
import { frameQuaternion } from './frames/frame'
import { LabelLayer, type LabelCandidate } from './labels'
import { buildSkyline, seedFor, skylineAt, type Skyline } from './landscape'
import {
  bodyMaterial,
  catalogMaterial,
  landscapeMaterial,
  lineMaterial,
  makeShared,
  markerMaterial,
  moonMaterial,
  routeMaterial,
  skyMaterial,
  starMaterial,
  type Shared,
} from './materials'
import { compassPoint } from './pointing'
import { pixelsPerDegree, projectView, stereoK, type Projection } from './projection'
import { routeGeometry, routeStops, type RouteStop } from './route'
import { prepareTracks, sampleTrack, type SatSample, type SatTrack } from './satellites'
import { horizonVector, toAltAz, ViewController, type Aim } from './view'
import type { PlannableSelection, SkyOptions, SkySelection } from './layers'

const D2R = Math.PI / 180
const R2D = 180 / Math.PI

type Catalog = components['schemas']['CatalogOut']
type CatalogObject = components['schemas']['CatalogObjectOut']

export interface SceneData {
  session: Session
  geometry: Geometry
  plan: Plan | undefined
  sky: Sky | undefined
  satellites: Satellites | undefined
  /** Every catalogue object, so anything can be picked and added. */
  catalog: Catalog | undefined
  selectedTarget: string | null
}

// Both moved to `./layers`, which imports no three.js, and re-exported here so
// the modules that import the TYPE from this file keep working. A value import
// of `sameSelection` from HERE still drags the renderer into the main bundle --
// import it from `./layers` instead.
export type { PlannableSelection, SkySelection } from './layers'
export { sameSelection } from './layers'

export interface FrameInfo {
  fovDeg: number
  /** Where the selection is on screen this frame, if anywhere. */
  reticle: { x: number; y: number; r: number } | null
  following: boolean
}

export interface InfoRow {
  label: string
  value: string
  hint?: string
}

export interface ObjectDescription {
  title: string
  subtitle: string
  rows: InfoRow[]
  note?: string
  /** False when the object is not in the sky at this instant (a pass ended). */
  present: boolean
}

export interface SearchHit {
  selection: SkySelection
  label: string
  kind: string
}

/** Display-sRGB colour from a linear palette entry. */
function display(c: Linear): Color {
  const f = (x: number) => (x <= 0.0031308 ? x * 12.92 : 1.055 * Math.pow(x, 1 / 2.4) - 0.055)
  return new Color(f(c[0]), f(c[1]), f(c[2]))
}

const toSrgb1 = (x: number) =>
  x <= 0.0031308 ? x * 12.92 : 1.055 * Math.pow(Math.max(0, x), 1 / 2.4) - 0.055

/** Planet tints in display sRGB. Chosen by eye, not taken from anywhere. */
const PLANET_COLOR: Record<string, [number, number, number]> = {
  mercury: [0.93, 0.88, 0.82],
  venus: [1.0, 0.97, 0.87],
  mars: [1.0, 0.68, 0.47],
  jupiter: [1.0, 0.95, 0.87],
  saturn: [0.98, 0.9, 0.68],
  uranus: [0.74, 0.94, 1.0],
  neptune: [0.58, 0.68, 1.0],
}
const SAT_COLOR: [number, number, number] = [0.74, 1.0, 0.84]
const MAX_SATS = 128

const CARDINALS = [
  { name: 'N', az: 0 },
  { name: 'E', az: 90 },
  { name: 'S', az: 180 },
  { name: 'W', az: 270 },
] as const
const INTERCARDINALS = [
  { name: 'NE', az: 45 },
  { name: 'SE', az: 135 },
  { name: 'SW', az: 225 },
  { name: 'NW', az: 315 },
] as const

const FULLSCREEN = new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0])

const lerp = (a: number, b: number, f: number) => a + (b - a) * f

function icrsDir(raDeg: number, decDeg: number, out = new Vector3()): Vector3 {
  const ra = raDeg * D2R
  const dec = decDeg * D2R
  return out.set(Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec))
}

function sexagesimalRa(raDeg: number): string {
  const h = (((raDeg % 360) + 360) % 360) / 15
  const hh = Math.floor(h)
  const m = (h - hh) * 60
  const mm = Math.floor(m)
  const ss = (m - mm) * 60
  return `${hh}h ${String(mm).padStart(2, '0')}m ${ss.toFixed(1).padStart(4, '0')}s`
}

function sexagesimalDec(decDeg: number): string {
  const sign = decDeg < 0 ? '−' : '+'
  const d = Math.abs(decDeg)
  const dd = Math.floor(d)
  const m = (d - dd) * 60
  const mm = Math.floor(m)
  const ss = (m - mm) * 60
  return `${sign}${dd}° ${String(mm).padStart(2, '0')}′ ${ss.toFixed(0).padStart(2, '0')}″`
}

function spectralHint(bv: number): string {
  if (bv < -0.02) return 'blue-white, B-type'
  if (bv < 0.3) return 'white, A-type'
  if (bv < 0.58) return 'yellow-white, F-type'
  if (bv < 0.81) return 'yellow, G-type like the Sun'
  if (bv < 1.4) return 'orange, K-type'
  return 'red, M-type'
}

const altAz = (alt: number, az: number) =>
  `${alt.toFixed(1)}° up · ${az.toFixed(1)}° ${compassPoint(az)}`

const utcHhmm = (ms: number) => new Date(ms).toISOString().slice(11, 16)

export class SkyRenderer {
  readonly view = new ViewController()

  private readonly renderer: WebGLRenderer
  private readonly scene = new Scene()
  /** Only a carrier for `modelViewMatrix`; its projection matrix is unused. */
  private readonly camera = new PerspectiveCamera(60, 1, 0.01, 10)
  private readonly rig = new Group()
  private readonly shared: Shared = makeShared()
  private readonly labels: LabelLayer

  private data: SceneData | null = null
  private clock: SlotClock | null = null
  private assets: SkyAssets | null = null
  private options: SkyOptions = { focus: false, grid: false }
  private selection: SkySelection | null = null
  private skyline: Skyline | null = null
  private tracks: SatTrack[] = []
  private targetDirs: Vector3[] = []
  private starNameByIndex = new Map<number, { name: string; desig: string }>()

  // layers
  private skyQuad: Mesh | null = null
  private landQuad: Mesh | null = null
  private stars: Points | null = null
  private constellations: LineSegments | null = null
  private grid: LineSegments | null = null
  private floorRing: LineSegments | null = null
  private planets: Points | null = null
  private moon: Points | null = null
  private sats: Points | null = null
  private markers: Points | null = null
  /** The observing route for the plan in force (see route.ts). */
  private route: Mesh | null = null
  /** What the route was built from, so setData only rebuilds on a real change. */
  private routeKey: string | null = null
  private routeGeometrySrc: Geometry | null = null
  /** The superseded route, fading out, so a re-plan is SEEN rather than swapped. */
  private routeGhost: Mesh | null = null
  private ghostBornMs = 0
  private stops: RouteStop[] = []
  /** Per target: its stops, and the label text to show while each is ahead. */
  private stopLabels = new Map<string, { stops: RouteStop[]; ahead: string[]; done: string }>()
  private sensor: LineLoop | null = null
  private sensorFor: string | null = null
  private moonRing: LineLoop | null = null
  /** Catalogue objects that are not targets: drawn faint, pickable, addable. */
  private catalogPoints: Points | null = null
  private catalogObjs: CatalogObject[] = []
  private catalogDirs: Vector3[] = []
  private catalogIndex = new Map<string, number>()

  // per-frame state, in the frames it is used in
  private readonly rigQ = new Quaternion()
  private readonly rigQInv = new Quaternion()
  private readonly viewQ = new Quaternion()
  private readonly viewQInv = new Quaternion()
  private readonly camQ = new Quaternion()
  private readonly camQInv = new Quaternion()
  private readonly sunLocal = new Vector3(0, 0, -1)
  private readonly moonLocal = new Vector3(0, 0, -1)
  private sunAlt = -90
  private moonAlt = -90
  private moonIllum = 0
  private planetLocal: Vector3[] = []
  private satNow: SatSample[] = []
  private readonly satPool: SatSample[] = []
  private nelm = 6.2
  private limit = 6.2
  private contextTarget = 1
  private lastT = NaN
  private lastViewVersion = -1
  private lastFrameMs = 0

  private raf = 0
  private dirty = true
  private disposed = false
  private unsubscribe: (() => void) | null = null
  private readonly _v = new Vector3()
  private readonly _w = new Vector3()
  private readonly _m3 = new Matrix3()
  private readonly _p = { x: 0, y: 0, visible: false }

  constructor(
    private readonly canvas: HTMLCanvasElement,
    labelHost: HTMLElement,
    private readonly onFrame: (info: FrameInfo) => void,
  ) {
    this.renderer = new WebGLRenderer({
      canvas,
      antialias: true,
      alpha: false,
      powerPreference: 'high-performance',
    })
    this.renderer.outputColorSpace = SRGBColorSpace
    this.renderer.setClearColor(0x000000, 1)
    this.renderer.autoClear = true
    this.labels = new LabelLayer(labelHost)

    this.camera.position.set(0, 0, 0)
    this.scene.add(this.rig)
    this.buildSkyQuad()
    this.unsubscribe = cursor.subscribeFrame(this.markDirty)
    canvas.addEventListener('webglcontextlost', this.onContextLost)
    canvas.addEventListener('webglcontextrestored', this.onContextRestored)
  }

  // ------------------------------------------------------------------ public

  setAssets(assets: SkyAssets): void {
    this.assets = assets
    this.starNameByIndex = new Map(assets.starNames.map(([i, name, desig]) => [i, { name, desig }]))
    this.buildSkyQuad()
    this.buildStars()
    this.buildConstellations()
    this.markDirty()
  }

  /**
   * Feed the scene a session, or null for the preview.
   *
   * The preview is the real sky with no ground under it: until a session
   * exists there is no site and no instant, so there is no horizon to draw --
   * and inventing one would be a confident fiction. The ground rises into
   * place the moment the session's frame arrives.
   */
  setData(data: SceneData | null): void {
    const prev = this.data
    this.data = data
    if (!data) {
      this.clock = null
      this.clearSession()
      this.markDirty()
      return
    }
    this.clock = slotClock(data.session.grid)
    const sessionChanged = prev?.session.id !== data.session.id
    const geometryChanged = prev?.geometry !== data.geometry

    if (sessionChanged) {
      const site = data.session.site
      this.skyline = buildSkyline(seedFor(site.latitudeDeg, site.longitudeDeg, site.name))
      this.buildLandscape()
      this.buildGrid()
      this.buildFloorRing()
      this.buildMoon()
      this.buildMoonRing()
    }
    if (geometryChanged) {
      this.targetDirs = data.geometry.rows.map((r) => icrsDir(r.raDeg, r.decDeg))
      this.buildMarkers()
    }
    if (prev?.sky !== data.sky) this.buildPlanets()
    if (sessionChanged || geometryChanged || prev?.catalog !== data.catalog) this.buildCatalog()
    if (prev?.satellites !== data.satellites) {
      this.tracks = prepareTracks(data.satellites)
      this.buildSatellites()
    }
    this.updateScheduled()
    this.buildRoute(sessionChanged)
    this.applyOptions()
    // Face the plan's first target -- once the plan exists, and only if the
    // person has not already started looking around on their own.
    if (sessionChanged || (!prev?.plan && data.plan && !this.view.touched)) this.frameInitialView()
    this.markDirty()
  }

  setOptions(options: SkyOptions): void {
    this.options = options
    this.contextTarget = options.focus ? 0.3 : 1
    this.applyOptions()
    this.markDirty()
  }

  setSelection(sel: SkySelection | null): void {
    this.selection = sel
    this.applyCatalogSelection()
    this.markDirty()
  }

  /** Messier objects show at every field; the rest only once zoomed in. */
  private catalogBright(i: number): boolean {
    return this.catalogObjs[i]?.designation.startsWith('M ') ?? false
  }

  /** The field below which every catalogue object is shown (and pickable). */
  private static readonly CATALOG_ALL_FOV = 55

  private applyCatalogSelection(): void {
    if (!this.catalogPoints) return
    const sel = this.selection
    const i = sel?.kind === 'catalog' ? (this.catalogIndex.get(sel.id) ?? -1) : -1
    ;(this.catalogPoints.material as ShaderMaterial).uniforms.uSelected!.value = i
  }

  /**
   * The catalogue, minus what is already a target (those are drawn as
   * targets). A fixed ICRS buffer like the stars: built when the catalogue or
   * the target list changes -- adding a target to the night takes it out of
   * here and into the plan -- and never touched per frame.
   */
  private buildCatalog(): void {
    if (this.catalogPoints) {
      this.scene.remove(this.catalogPoints)
      this.catalogPoints.geometry.dispose()
      ;(this.catalogPoints.material as ShaderMaterial).dispose()
      this.catalogPoints = null
    }
    const d = this.data
    const all = d?.catalog?.objects ?? []
    const targets = new Set(d?.geometry.rows.map((r) => r.targetId) ?? [])
    this.catalogObjs = all.filter((o) => !targets.has(o.id))
    this.catalogDirs = this.catalogObjs.map((o) => icrsDir(o.raDeg, o.decDeg))
    this.catalogIndex = new Map(this.catalogObjs.map((o, i) => [o.id, i]))
    const n = this.catalogObjs.length
    if (!n) return
    const pos = new Float32Array(n * 3)
    const idx = new Float32Array(n)
    const bright = new Float32Array(n)
    this.catalogDirs.forEach((v, i) => {
      pos.set([v.x, v.y, v.z], i * 3)
      idx[i] = i
      bright[i] = this.catalogBright(i) ? 1 : 0
    })
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(pos, 3))
    g.setAttribute('aIndex', new BufferAttribute(idx, 1))
    g.setAttribute('aBright', new BufferAttribute(bright, 1))
    this.catalogPoints = new Points(
      g,
      catalogMaterial(this.shared, display(palette.inkDim), display(palette.accent)),
    )
    this.catalogPoints.frustumCulled = false
    // Over the route (25), under the targets (30): context, not the plan.
    this.catalogPoints.renderOrder = 28
    this.scene.add(this.catalogPoints)
    this.applyCatalogSelection()
  }

  projection(): Projection {
    return {
      fovDeg: this.view.state.fovDeg,
      width: this.canvas.clientWidth || 1,
      height: this.canvas.clientHeight || 1,
    }
  }

  resize(): void {
    const w = this.canvas.clientWidth || 1
    const h = this.canvas.clientHeight || 1
    // Cap the pixel ratio at 2: beyond that the star field is fill-rate bound
    // for detail nobody can see, and a 3x phone loses half its frame rate.
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    this.renderer.setPixelRatio(dpr)
    this.renderer.setSize(w, h, false)
    this.shared.uAspect.value = w / h
    this.shared.uLandscape.value = w >= h ? 1 : 0
    this.shared.uPixelRatio.value = dpr
    this.shared.uResolution.value.set(w * dpr, h * dpr)
    this.shared.uPointScale.value = dpr * Math.max(0.85, Math.min(1.4, Math.min(w, h) / 800))
    this.markDirty()
  }

  start(): void {
    if (this.raf) return
    const tick = (now: number) => {
      this.raf = requestAnimationFrame(tick)
      this.step(now)
    }
    this.raf = requestAnimationFrame(tick)
  }

  markDirty = (): void => {
    this.dirty = true
  }

  dispose(): void {
    this.disposed = true
    cancelAnimationFrame(this.raf)
    this.raf = 0
    this.unsubscribe?.()
    this.canvas.removeEventListener('webglcontextlost', this.onContextLost)
    this.canvas.removeEventListener('webglcontextrestored', this.onContextRestored)
    this.labels.clear()
    this.scene.traverse((o) => {
      const any = o as unknown as { geometry?: { dispose(): void }; material?: { dispose(): void } }
      any.geometry?.dispose()
      any.material?.dispose()
    })
    this.skyline?.texture.dispose()
    this.renderer.dispose()
  }

  // ------------------------------------------------------------ frame loop

  private onContextLost = (e: Event): void => {
    // Without preventDefault the context is gone for good. A laptop sleeping
    // at 2 a.m. is the normal case, not the edge case.
    e.preventDefault()
    cancelAnimationFrame(this.raf)
    this.raf = 0
  }

  private onContextRestored = (): void => {
    this.buildSkyQuad()
    this.buildStars()
    this.buildConstellations()
    const d = this.data
    this.data = null
    this.setData(d)
    this.resize()
    this.start()
  }

  private step(now: number): void {
    if (this.disposed) return
    const proj = this.projection()
    const moving = this.view.update(now, proj)
    const easing = this.easeContext(now)
    const fading = this.fadeGhost(now)
    const t = cursor.live.tMs
    const timeMoved = t !== this.lastT
    const viewMoved = this.view.version !== this.lastViewVersion
    const alive = cursor.live.playing
    if (!moving && !easing && !fading && !this.dirty && !timeMoved && !viewMoved && !alive) {
      this.lastFrameMs = now
      return
    }
    this.dirty = false
    this.lastT = t
    this.lastViewVersion = this.view.version
    this.lastFrameMs = now

    this.updateTime(t)
    this.updateCamera(proj)
    this.updateUniforms(proj, t, now)
    this.renderer.render(this.scene, this.camera)
    this.labels.update(this.labelCandidates(proj), proj.width, proj.height)
    this.onFrame({
      fovDeg: this.view.state.fovDeg,
      reticle: this.reticle(proj),
      following: this.view.following,
    })
  }

  /** How long a superseded route takes to fade out, in ms. */
  private static readonly GHOST_MS = 1600

  /**
   * Fade the superseded route. Returns true while it is fading, so the
   * render-on-demand loop keeps drawing until it is gone -- otherwise it would
   * freeze half-faded on a still sky.
   */
  private fadeGhost(now: number): boolean {
    const g = this.routeGhost
    if (!g) return false
    if (!this.ghostBornMs) this.ghostBornMs = now
    const a = 1 - (now - this.ghostBornMs) / SkyRenderer.GHOST_MS
    if (a <= 0) {
      this.disposeMesh(g)
      this.routeGhost = null
      return true
    }
    ;(g.material as ShaderMaterial).uniforms.uAlpha!.value = 0.85 * a
    return true
  }

  private disposeMesh(m: Mesh): void {
    this.scene.remove(m)
    m.geometry.dispose()
    ;(m.material as ShaderMaterial).dispose()
  }

  private easeContext(now: number): boolean {
    const cur = this.shared.uContext.value
    if (Math.abs(cur - this.contextTarget) < 1e-3) {
      this.shared.uContext.value = this.contextTarget
      return false
    }
    // ~250 ms to settle, independent of frame rate.
    const dt = Math.min(0.1, Math.max(0.001, (now - this.lastFrameMs) / 1000))
    const k = 1 - Math.exp(-dt / 0.08)
    this.shared.uContext.value = cur + (this.contextTarget - cur) * k
    return true
  }

  // ------------------------------------------------------------ time state

  private updateTime(t: number): void {
    const d = this.data
    const clock = this.clock
    if (!d || !clock) {
      this.rigQ.identity()
      this.rigQInv.identity()
      this.rig.quaternion.identity()
      this.nelm = 6.2
      return
    }
    const { a, b, f } = midpointSpan(clock, t)
    const geo = d.geometry

    frameQuaternion(geo, a, this.rigQ)
    if (f > 0 && b !== a) this.rigQ.slerp(frameQuaternion(geo, b, new Quaternion()), f)
    this.rigQInv.copy(this.rigQ).invert()
    this.rig.quaternion.copy(this.rigQ)

    // Interpolated as VECTORS: azimuth wraps, and a body interpolated as
    // scalars flies the long way round when it crosses north.
    const vec = (alt: ArrayLike<number>, az: ArrayLike<number>, out: Vector3) => {
      horizonVector(alt[a] ?? -90, az[a] ?? 0, out)
      if (f > 0 && b !== a) out.lerp(horizonVector(alt[b] ?? -90, az[b] ?? 0, this._w), f).normalize()
      return out
    }
    vec(geo.sunAltitudeDeg, geo.sunAzimuthDeg, this.sunLocal)
    this.sunAlt = lerp(geo.sunAltitudeDeg[a] ?? -90, geo.sunAltitudeDeg[b] ?? -90, f)
    const moon = d.session.moon
    vec(moon.altitudeDeg, moon.azimuthDeg, this.moonLocal)
    this.moonAlt = lerp(moon.altitudeDeg[a] ?? -90, moon.altitudeDeg[b] ?? -90, f)
    this.moonIllum = lerp(moon.illumination[a] ?? 0, moon.illumination[b] ?? 0, f)

    const planets = d.sky?.planets ?? []
    while (this.planetLocal.length < planets.length) this.planetLocal.push(new Vector3())
    planets.forEach((p, i) => vec(p.altitudeDeg, p.azimuthDeg, this.planetLocal[i]!))

    const lim = d.sky?.nakedEyeLimitMag
    this.nelm = lim && lim.length ? lerp(lim[a] ?? 6, lim[b] ?? 6, f) : 6

    // Satellites: every pass in progress at this instant, into pooled samples
    // so a running clock allocates nothing per frame.
    this.satNow.length = 0
    for (const track of this.tracks) {
      if (track.t0 > t) break
      if (track.t1 < t) continue
      const slot = (this.satPool[this.satNow.length] ??= {
        track,
        dir: new Vector3(),
        mag: NaN,
        nearest: 0,
      })
      if (sampleTrack(track, t, slot)) this.satNow.push(slot)
      if (this.satNow.length >= MAX_SATS) break
    }
  }

  private updateCamera(proj: Projection): void {
    this.view.orientation(this.viewQ)
    this.viewQInv.copy(this.viewQ).invert()
    this.camQ.copy(this.rigQ).multiply(this.viewQ)
    this.camQInv.copy(this.camQ).invert()
    this.camera.quaternion.copy(this.camQ)
    this.camera.updateMatrixWorld(true)
    this.shared.uStereoK.value = stereoK(proj.fovDeg)
  }

  /**
   * The effective limiting magnitude: the backend's naked-eye limit for this
   * slot, deepened as the field narrows. Zooming is looking through
   * binoculars, and binoculars show fainter stars.
   */
  private limitFor(fovDeg: number): number {
    return this.nelm + 2.6 * Math.log10(Math.max(1, 110 / fovDeg))
  }

  private updateUniforms(proj: Projection, t: number, now: number): void {
    const d = this.data
    const hasFrame = d !== null
    const fov = proj.fovDeg
    this.limit = this.limitFor(fov)

    // -- sky
    if (this.skyQuad) {
      const m = this.skyQuad.material as ShaderMaterial
      const uni = m.uniforms
      uni.uViewToWorld!.value.setFromMatrix4(this.camera.matrixWorld)
      uni.uViewToLocal!.value.setFromMatrix4(this._mat4FromQuat(this.viewQ))
      uni.uHasFrame!.value = hasFrame ? 1 : 0
      const mu = this.zenithMu(t)
      // Display luminance from the backend's zenith sky brightness, on a
      // compressive curve: every magnitude of sky brightness is 0.22 dex of
      // screen luminance. Pinned so a Bortle 5 night reads as Stellarium's
      // familiar deep navy rather than as black.
      // A soft shoulder above that, so twilight deepens toward blue instead of
      // blowing out: a screen cannot show the sky's real dynamic range, and a
      // white dusk hides Venus, which is the one thing you look for then.
      const y = 0.016 * Math.pow(10, -0.22 * (mu - 20.7))
      uni.uSkyY!.value = y / (1 + y / 0.3)
      uni.uSunLocal!.value.copy(this.sunLocal)
      uni.uSunAlt!.value = this.sunAlt
      uni.uMoonLocal!.value.copy(this.moonLocal)
      uni.uMoonAlt!.value = this.moonAlt
      uni.uMoonIllum!.value = this.moonIllum
      uni.uLpWarm!.value = this.lightPollutionFraction()
      // The Milky Way needs a dark sky, and a picture of it only holds up to
      // about ten degrees of field before its pixels show.
      const dark = hasFrame ? smooth(4.2, 6.8, this.nelm) : 1
      uni.uMwGain!.value = 0.62 * Math.pow(dark, 1.2) * smooth(2.5, 10, fov)
    }

    // -- ground
    if (this.landQuad && d) {
      const uni = (this.landQuad.material as ShaderMaterial).uniforms
      uni.uViewToLocal!.value.setFromMatrix4(this._mat4FromQuat(this.viewQ))
      const lit =
        0.006 +
        0.02 * this.moonIllum * smooth(-3, 30, this.moonAlt) +
        0.25 * Math.pow(smooth(-14, 4, this.sunAlt), 2) +
        0.0025 * this.lightPollutionFraction()
      uni.uGroundY!.value = lit
      const skyY = ((this.skyQuad?.material as ShaderMaterial).uniforms.uSkyY!.value as number) * 2.6
      uni.uHorizonCol!.value.set(skyY * 0.8, skyY * 1.0, skyY * 1.45)
      // Zoomed right in on a target near the trees, the trees get out of the way.
      uni.uAlpha!.value = 0.25 + 0.75 * smooth(2, 12, fov)
    }

    // -- stars
    if (this.stars) {
      const uni = (this.stars.material as ShaderMaterial).uniforms
      uni.uLimit!.value = this.limit
      uni.uHasFrame!.value = hasFrame ? 1 : 0
      uni.uZenith!.value.set(0, 0, 1).applyQuaternion(this.rigQ)
      uni.uExtK!.value = d?.session.site.extinctionK ?? 0.2
    }

    // -- planets
    if (this.planets && d?.sky) {
      const pos = this.planets.geometry.getAttribute('position') as BufferAttribute
      const vis = this.planets.geometry.getAttribute('aVisible') as BufferAttribute
      d.sky.planets.forEach((_, i) => {
        const v = this.planetLocal[i]!
        pos.setXYZ(i, v.x, v.y, v.z)
        vis.setX(i, v.z > -0.05 ? 1 : 0)
      })
      pos.needsUpdate = true
      vis.needsUpdate = true
      const uni = (this.planets.material as ShaderMaterial).uniforms
      uni.uLimit!.value = this.limit
      uni.uExtK!.value = d.session.site.extinctionK
    }

    // -- satellites
    if (this.sats) {
      const g = this.sats.geometry
      const pos = g.getAttribute('position') as BufferAttribute
      const mag = g.getAttribute('aMag') as BufferAttribute
      const vis = g.getAttribute('aVisible') as BufferAttribute
      for (let i = 0; i < MAX_SATS; i++) {
        const s = this.satNow[i]
        if (s && !Number.isNaN(s.mag)) {
          pos.setXYZ(i, s.dir.x, s.dir.y, s.dir.z)
          mag.setX(i, s.mag)
          vis.setX(i, 1)
        } else {
          vis.setX(i, 0)
        }
      }
      pos.needsUpdate = true
      mag.needsUpdate = true
      vis.needsUpdate = true
      const uni = (this.sats.material as ShaderMaterial).uniforms
      uni.uLimit!.value = this.limit
      uni.uExtK!.value = d?.session.site.extinctionK ?? 0.2
    }

    // -- moon
    if (this.moon && d) {
      const pos = this.moon.geometry.getAttribute('position') as BufferAttribute
      pos.setXYZ(0, this.moonLocal.x, this.moonLocal.y, this.moonLocal.z)
      pos.needsUpdate = true
      this.moon.visible = this.moonAlt > -3
      const uni = (this.moon.material as ShaderMaterial).uniforms
      uni.uIllum!.value = this.moonIllum
      // Drawn larger than life at wide fields, or the phase is two pixels.
      const ppd = pixelsPerDegree(proj)
      uni.uDiscPx!.value = Math.max(5, 0.26 * Math.max(1, fov / 25) * ppd)
      // Which way is the Sun, on screen, from the Moon? Project a point a
      // hair toward the Sun and take the difference.
      const tangent = this._w.copy(this.sunLocal).addScaledVector(this.moonLocal, -this.moonLocal.dot(this.sunLocal))
      if (tangent.lengthSq() > 1e-10) {
        tangent.normalize()
        const p0 = this.projectLocal(this.moonLocal, proj)
        const x0 = p0.x
        const y0 = p0.y
        const p1 = this.projectLocal(this._v.copy(this.moonLocal).addScaledVector(tangent, 0.01), proj)
        const dx = p1.x - x0
        const dy = -(p1.y - y0)
        const n = Math.hypot(dx, dy) || 1
        uni.uSunDir2!.value.set(dx / n, dy / n)
      }
    }

    if (this.moonRing && d) this.updateMoonRing()

    // -- targets and plan
    if (this.markers && d) {
      const uni = (this.markers.material as ShaderMaterial).uniforms
      uni.uSelected!.value = this.targetIndex(d.selectedTarget)
      uni.uCurrent!.value = this.targetIndex(this.currentTargetId())
      uni.uTime!.value = now / 1000
    }
    if (this.catalogPoints) {
      ;(this.catalogPoints.material as ShaderMaterial).uniforms.uFov!.value = fov
    }
    if (this.clock) {
      const span = this.clock.endMs - this.clock.startMs
      const ct = span > 0 ? (t - this.clock.startMs) / span : 0
      for (const m of [this.route, this.routeGhost]) {
        if (!m) continue
        const uni = (m.material as ShaderMaterial).uniforms
        uni.uCursorT!.value = ct
        uni.uTime!.value = now / 1000
      }
    }
    this.updateSensor()
  }

  private readonly _mat4 = new Matrix4()

  /** Matrix3.setFromMatrix4 wants a Matrix4; reuse one rather than allocate. */
  private _mat4FromQuat(q: Quaternion): Matrix4 {
    return this._mat4.makeRotationFromQuaternion(q)
  }

  private zenithMu(t: number): number {
    const d = this.data
    const clock = this.clock
    const mu = d?.sky?.zenithSkyMagArcsec2
    if (!d || !clock || !mu || !mu.length) return 21.5
    const { a, b, f } = midpointSpan(clock, t)
    return lerp(mu[a] ?? 21, mu[b] ?? 21, f)
  }

  private lightPollutionFraction(): number {
    const site = this.data?.session.site
    if (!site) return 0
    // A pristine zenith is about 54 nL; the rest is the site's artificial glow.
    return site.artificialZenithNl / (site.artificialZenithNl + 54)
  }

  // -------------------------------------------------------------- building

  private buildSkyQuad(): void {
    if (this.skyQuad) {
      this.scene.remove(this.skyQuad)
      this.skyQuad.geometry.dispose()
      ;(this.skyQuad.material as ShaderMaterial).dispose()
    }
    const mat = skyMaterial(this.shared)
    const bitmap = this.assets?.background
    if (bitmap) {
      const tex = new Texture(bitmap as unknown as HTMLImageElement)
      tex.colorSpace = SRGBColorSpace
      tex.flipY = false // the shader's v computation assumes it
      tex.anisotropy = Math.min(8, this.renderer.capabilities.getMaxAnisotropy())
      tex.needsUpdate = true
      const bg = this.assets?.manifest?.background
      mat.uniforms.uMap!.value = tex
      mat.uniforms.uHasMap!.value = 1
      mat.uniforms.uRaAtLeft!.value = bg?.raAtLeftEdgeDeg ?? 180
      mat.uniforms.uRaDir!.value = bg?.raIncreasesTo === 'right' ? 1 : -1
    }
    this.skyQuad = this.fullscreen(mat, 0)
    this.scene.add(this.skyQuad)
  }

  private fullscreen(mat: ShaderMaterial, order: number): Mesh {
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(FULLSCREEN.slice(), 3))
    const mesh = new Mesh(g, mat)
    mesh.frustumCulled = false
    mesh.renderOrder = order
    return mesh
  }

  private buildLandscape(): void {
    if (this.landQuad) {
      this.scene.remove(this.landQuad)
      this.landQuad.geometry.dispose()
      ;(this.landQuad.material as ShaderMaterial).dispose()
      this.landQuad = null
    }
    if (!this.skyline) return
    this.landQuad = this.fullscreen(landscapeMaterial(this.shared, this.skyline.texture), 50)
    this.scene.add(this.landQuad)
  }

  private buildStars(): void {
    if (this.stars) {
      this.scene.remove(this.stars)
      this.stars.geometry.dispose()
      ;(this.stars.material as ShaderMaterial).dispose()
      this.stars = null
    }
    const cat = this.assets?.stars
    if (!cat) return
    const color = new Float32Array(cat.count * 3)
    for (let i = 0; i < cat.count; i++) {
      const [r, g, b] = bvToLinearRgb(cat.bv[i] ?? 0.65)
      // Pastel, as the eye sees them: a star's colour is a tint, not a paint.
      color[i * 3] = 0.3 + 0.7 * toSrgb1(r)
      color[i * 3 + 1] = 0.3 + 0.7 * toSrgb1(g)
      color[i * 3 + 2] = 0.3 + 0.7 * toSrgb1(b)
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(cat.position, 3))
    g.setAttribute('aColor', new BufferAttribute(color, 3))
    g.setAttribute('aMag', new BufferAttribute(cat.magnitude, 1))
    this.stars = new Points(g, starMaterial(this.shared))
    this.stars.frustumCulled = false
    this.stars.renderOrder = 10
    this.scene.add(this.stars)
  }

  /**
   * Constellation figures, subdivided so they bow along great circles.
   *
   * A stereographic projection maps a great circle to a CIRCLE on screen, so a
   * long straight chord between two stars would visibly miss the sky's own
   * curve at wide fields. Every segment is cut into pieces of at most 1.5
   * degrees, and trimmed back from the stars at each end so a line never
   * draws across the star it points to.
   */
  private buildConstellations(): void {
    if (this.constellations) {
      this.scene.remove(this.constellations)
      this.constellations.geometry.dispose()
      ;(this.constellations.material as ShaderMaterial).dispose()
      this.constellations = null
    }
    const lines = this.assets?.constellations
    if (!lines) return
    const src = lines.position
    const out: number[] = []
    const a = new Vector3()
    const b = new Vector3()
    const p = new Vector3()
    const q = new Vector3()
    const trim = 0.45 * D2R
    for (let i = 0; i + 5 < src.length; i += 6) {
      a.set(src[i] ?? 0, src[i + 1] ?? 0, src[i + 2] ?? 0).normalize()
      b.set(src[i + 3] ?? 0, src[i + 4] ?? 0, src[i + 5] ?? 0).normalize()
      const ang = Math.acos(Math.max(-1, Math.min(1, a.dot(b))))
      if (ang < 3 * trim) continue
      const steps = Math.max(1, Math.ceil((ang - 2 * trim) / (1.5 * D2R)))
      const slerpTo = (t: number, o: Vector3) => {
        const s = Math.sin(ang)
        return o
          .copy(a)
          .multiplyScalar(Math.sin((1 - t) * ang) / s)
          .addScaledVector(b, Math.sin(t * ang) / s)
      }
      const t0 = trim / ang
      const t1 = 1 - trim / ang
      for (let k = 0; k < steps; k++) {
        slerpTo(t0 + ((t1 - t0) * k) / steps, p)
        slerpTo(t0 + ((t1 - t0) * (k + 1)) / steps, q)
        out.push(p.x, p.y, p.z, q.x, q.y, q.z)
      }
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(out), 3))
    this.constellations = new LineSegments(
      g,
      lineMaterial(this.shared, new Color(0.42, 0.62, 0.78), 0.32, { contextual: true }),
    )
    this.constellations.frustumCulled = false
    this.constellations.renderOrder = 5
    this.scene.add(this.constellations)
  }

  /** Alt-az grid in the HORIZON frame: altitude every 15, azimuth every 30. */
  private buildGrid(): void {
    if (this.grid) {
      this.rig.remove(this.grid)
      this.grid.geometry.dispose()
      ;(this.grid.material as ShaderMaterial).dispose()
    }
    const pts: number[] = []
    const v = new Vector3()
    const w = new Vector3()
    const seg = (alt0: number, az0: number, alt1: number, az1: number) => {
      horizonVector(alt0, az0, v)
      horizonVector(alt1, az1, w)
      pts.push(v.x, v.y, v.z, w.x, w.y, w.z)
    }
    for (let az = 0; az < 360; az += 30) {
      for (let alt = 0; alt < 88; alt += 2) seg(alt, az, Math.min(88, alt + 2), az)
    }
    for (let alt = 15; alt < 90; alt += 15) {
      for (let az = 0; az < 360; az += 2) seg(alt, az, alt, az + 2)
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(pts), 3))
    this.grid = new LineSegments(g, lineMaterial(this.shared, new Color(0.62, 0.5, 0.42), 0.34))
    this.grid.frustumCulled = false
    this.grid.renderOrder = 6
    this.rig.add(this.grid)
  }

  /** The altitude floor: below this ring the scheduler will not point. Dashed. */
  private buildFloorRing(): void {
    if (this.floorRing) {
      this.rig.remove(this.floorRing)
      this.floorRing.geometry.dispose()
      ;(this.floorRing.material as ShaderMaterial).dispose()
      this.floorRing = null
    }
    const d = this.data
    if (!d) return
    const alt = d.session.site.minAltitudeDeg
    const pts: number[] = []
    const v = new Vector3()
    const w = new Vector3()
    for (let az = 0; az < 360; az += 3) {
      horizonVector(alt, az, v)
      horizonVector(alt, az + 1.8, w)
      pts.push(v.x, v.y, v.z, w.x, w.y, w.z)
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(pts), 3))
    this.floorRing = new LineSegments(g, lineMaterial(this.shared, display(palette.warn), 0.45))
    this.floorRing.frustumCulled = false
    this.floorRing.renderOrder = 6
    this.rig.add(this.floorRing)
  }

  private buildPlanets(): void {
    if (this.planets) {
      this.rig.remove(this.planets)
      this.planets.geometry.dispose()
      ;(this.planets.material as ShaderMaterial).dispose()
      this.planets = null
    }
    const list = this.data?.sky?.planets ?? []
    if (!list.length) return
    const n = list.length
    const color = new Float32Array(n * 3)
    const mag = new Float32Array(n)
    list.forEach((p, i) => {
      const c = PLANET_COLOR[p.id] ?? [1, 1, 1]
      color.set(c, i * 3)
      mag[i] = p.magnitude
    })
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(n * 3), 3))
    g.setAttribute('aColor', new BufferAttribute(color, 3))
    g.setAttribute('aMag', new BufferAttribute(mag, 1))
    g.setAttribute('aVisible', new BufferAttribute(new Float32Array(n), 1))
    this.planets = new Points(g, bodyMaterial(this.shared, { contextual: false }))
    this.planets.frustumCulled = false
    this.planets.renderOrder = 12
    this.rig.add(this.planets)
  }

  private buildSatellites(): void {
    if (this.sats) {
      this.rig.remove(this.sats)
      this.sats.geometry.dispose()
      ;(this.sats.material as ShaderMaterial).dispose()
      this.sats = null
    }
    if (!this.tracks.length) return
    const color = new Float32Array(MAX_SATS * 3)
    for (let i = 0; i < MAX_SATS; i++) color.set(SAT_COLOR, i * 3)
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(MAX_SATS * 3), 3))
    g.setAttribute('aColor', new BufferAttribute(color, 3))
    g.setAttribute('aMag', new BufferAttribute(new Float32Array(MAX_SATS), 1))
    g.setAttribute('aVisible', new BufferAttribute(new Float32Array(MAX_SATS), 1))
    this.sats = new Points(g, bodyMaterial(this.shared, { contextual: true }))
    this.sats.frustumCulled = false
    this.sats.renderOrder = 13
    this.rig.add(this.sats)
  }

  private buildMoon(): void {
    if (this.moon) {
      this.rig.remove(this.moon)
      this.moon.geometry.dispose()
      ;(this.moon.material as ShaderMaterial).dispose()
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(3), 3))
    this.moon = new Points(g, moonMaterial(this.shared))
    this.moon.frustumCulled = false
    this.moon.renderOrder = 14
    this.rig.add(this.moon)
  }

  /**
   * The lunar exclusion zone as a real circle on the sky, at its true angle.
   * A target inside it is why a target was dropped, and this makes that visible.
   */
  private buildMoonRing(): void {
    if (this.moonRing) {
      this.rig.remove(this.moonRing)
      this.moonRing.geometry.dispose()
      ;(this.moonRing.material as ShaderMaterial).dispose()
      this.moonRing = null
    }
    const d = this.data
    if (!d || d.session.site.minMoonSeparationDeg <= 0) return
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(120 * 3), 3))
    this.moonRing = new LineLoop(g, lineMaterial(this.shared, display(palette.moon), 0.28))
    this.moonRing.frustumCulled = false
    this.moonRing.renderOrder = 15
    this.rig.add(this.moonRing)
  }

  private updateMoonRing(): void {
    const ring = this.moonRing
    const d = this.data
    if (!ring || !d) return
    ring.visible = this.moonAlt > 0
    const dir = this.moonLocal
    const theta = d.session.site.minMoonSeparationDeg * D2R
    const seed = Math.abs(dir.z) < 0.9 ? new Vector3(0, 0, 1) : new Vector3(1, 0, 0)
    const a = seed.cross(dir).normalize()
    const b = new Vector3().copy(dir).cross(a).normalize()
    const pos = ring.geometry.getAttribute('position') as BufferAttribute
    const c = Math.cos(theta)
    const s = Math.sin(theta)
    for (let i = 0; i < 120; i++) {
      const phi = (i / 120) * Math.PI * 2
      pos.setXYZ(
        i,
        dir.x * c + (a.x * Math.cos(phi) + b.x * Math.sin(phi)) * s,
        dir.y * c + (a.y * Math.cos(phi) + b.y * Math.sin(phi)) * s,
        dir.z * c + (a.z * Math.cos(phi) + b.z * Math.sin(phi)) * s,
      )
    }
    pos.needsUpdate = true
  }

  private buildMarkers(): void {
    if (this.markers) {
      this.scene.remove(this.markers)
      this.markers.geometry.dispose()
      ;(this.markers.material as ShaderMaterial).dispose()
      this.markers = null
    }
    const d = this.data
    if (!d || !d.geometry.rows.length) return
    const n = d.geometry.rows.length
    const pos = new Float32Array(n * 3)
    const idx = new Float32Array(n)
    this.targetDirs.forEach((v, i) => {
      pos.set([v.x, v.y, v.z], i * 3)
      idx[i] = i
    })
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(pos, 3))
    g.setAttribute('aIndex', new BufferAttribute(idx, 1))
    g.setAttribute('aScheduled', new BufferAttribute(new Float32Array(n), 1))
    this.markers = new Points(
      g,
      markerMaterial(this.shared, display(palette.accent), display(palette.inkDim)),
    )
    this.markers.frustumCulled = false
    this.markers.renderOrder = 30
    this.scene.add(this.markers)
  }

  private updateScheduled(): void {
    const d = this.data
    if (!d || !this.markers) return
    const planned = new Set((d.plan?.blocks ?? []).map((b) => b.targetId))
    const attr = this.markers.geometry.getAttribute('aScheduled') as BufferAttribute
    d.geometry.rows.forEach((r, i) => attr.setX(i, planned.has(r.targetId) ? 1 : 0))
    attr.needsUpdate = true
  }

  /**
   * The observing route for the plan in force: stops in observing order,
   * joined by legs whose look follows the cursor (see route.ts and
   * `routeMaterial`). Rebuilt only when the plan or the geometry changes;
   * when a NEW plan replaces the old one mid-night, the old route stays a
   * moment as a fading ghost, so the change is something you see happen.
   */
  private buildRoute(sessionChanged: boolean): void {
    const d = this.data
    if (!d || !this.clock) return
    const key = `${d.session.id}:${d.plan?.planId ?? ''}`
    if (key === this.routeKey && this.routeGeometrySrc === d.geometry) return
    const replanned = !sessionChanged && this.routeKey !== null && key !== this.routeKey
    this.routeKey = key
    this.routeGeometrySrc = d.geometry

    if (this.routeGhost) {
      this.disposeMesh(this.routeGhost)
      this.routeGhost = null
    }
    if (this.route) {
      if (replanned) {
        this.routeGhost = this.route
        this.ghostBornMs = 0
        this.routeGhost.renderOrder = 24
      } else {
        this.disposeMesh(this.route)
      }
      this.route = null
    }

    const dirs = new Map(d.geometry.rows.map((r, i) => [r.targetId, this.targetDirs[i]!] as const))
    this.stops = routeStops(d.plan, dirs)
    this.stopLabels.clear()
    const names = new Map(d.geometry.rows.map((r) => [r.targetId, r.name]))
    for (const st of this.stops) {
      const e = this.stopLabels.get(st.targetId) ?? {
        stops: [],
        ahead: [],
        done: `✓ ${names.get(st.targetId) ?? st.targetId}`,
      }
      e.stops.push(st)
      e.ahead.push(`${st.order} · ${names.get(st.targetId) ?? st.targetId} · ${utcHhmm(st.startMs)}`)
      this.stopLabels.set(st.targetId, e)
    }

    const g = routeGeometry(this.stops, this.clock)
    if (!g) return
    const accent = display(palette.accent)
    this.route = new Mesh(
      g,
      routeMaterial(this.shared, {
        later: accent,
        next: accent.clone().lerp(new Color(1, 1, 1), 0.55),
        travelled: display(palette.inkDim),
      }),
    )
    this.route.frustumCulled = false
    // Under the landscape pass (50), which is what hides it below the skyline.
    this.route.renderOrder = 25
    this.scene.add(this.route)
  }

  /**
   * A scheduled target's label: its place in the order and its start time
   * while a visit is still ahead, a tick once every visit is done. The strings
   * are built with the route, so choosing one per frame allocates nothing.
   */
  private routeLabel(targetId: string, name: string, t: number): string {
    const e = this.stopLabels.get(targetId)
    if (!e) return name
    for (let i = 0; i < e.stops.length; i++) if (e.stops[i]!.endMs > t) return e.ahead[i]!
    return e.done
  }

  /**
   * The camera's field, outlined on the sky around the target being observed:
   * zoom in and you see what the sensor will see. North up, the way a camera
   * on an equatorial mount frames it.
   */
  private updateSensor(): void {
    const d = this.data
    const id = this.currentTargetId() ?? d?.selectedTarget ?? null
    if (id === this.sensorFor) return
    this.sensorFor = id
    if (this.sensor) {
      this.scene.remove(this.sensor)
      this.sensor.geometry.dispose()
      ;(this.sensor.material as ShaderMaterial).dispose()
      this.sensor = null
    }
    const row = d?.geometry.rows.find((r) => r.targetId === id)
    if (!d || !row) return
    const eq = d.session.equipment
    const c = icrsDir(row.raDeg, row.decDeg)
    const ra = row.raDeg * D2R
    const dec = row.decDeg * D2R
    const east = new Vector3(-Math.sin(ra), Math.cos(ra), 0)
    const north = new Vector3(-Math.sin(dec) * Math.cos(ra), -Math.sin(dec) * Math.sin(ra), Math.cos(dec))
    const hw = Math.tan((eq.fovWidthDeg * D2R) / 2)
    const hh = Math.tan((eq.fovHeightDeg * D2R) / 2)
    const corners: [number, number][] = [
      [-1, -1],
      [1, -1],
      [1, 1],
      [-1, 1],
    ]
    const pts: number[] = []
    for (let k = 0; k < 4; k++) {
      const [x0, y0] = corners[k]!
      const [x1, y1] = corners[(k + 1) % 4]!
      for (let s = 0; s < 8; s++) {
        const f = s / 8
        const x = lerp(x0, x1, f) * hw
        const y = lerp(y0, y1, f) * hh
        const v = c.clone().addScaledVector(east, x).addScaledVector(north, y).normalize()
        pts.push(v.x, v.y, v.z)
      }
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(pts), 3))
    this.sensor = new LineLoop(g, lineMaterial(this.shared, display(palette.accent), 0.85))
    this.sensor.frustumCulled = false
    this.sensor.renderOrder = 31
    this.scene.add(this.sensor)
  }

  private clearSession(): void {
    for (const obj of [
      this.landQuad,
      this.markers,
      this.route,
      this.routeGhost,
      this.sensor,
      this.catalogPoints,
    ]) {
      if (!obj) continue
      this.scene.remove(obj)
      obj.geometry.dispose()
      ;(obj.material as ShaderMaterial).dispose()
    }
    for (const obj of [this.grid, this.floorRing, this.planets, this.moon, this.sats, this.moonRing]) {
      if (!obj) continue
      this.rig.remove(obj)
      obj.geometry.dispose()
      ;(obj.material as ShaderMaterial).dispose()
    }
    this.landQuad = null
    this.markers = null
    this.route = null
    this.routeGhost = null
    this.catalogPoints = null
    this.catalogObjs = []
    this.catalogDirs = []
    this.catalogIndex.clear()
    this.routeKey = null
    this.routeGeometrySrc = null
    this.stops = []
    this.stopLabels.clear()
    this.sensor = null
    this.sensorFor = null
    this.grid = null
    this.floorRing = null
    this.planets = null
    this.moon = null
    this.sats = null
    this.moonRing = null
    this.tracks = []
    this.satNow = []
    this.targetDirs = []
    this.skyline?.texture.dispose()
    this.skyline = null
    this.labels.clear()
  }

  private applyOptions(): void {
    if (this.grid) this.grid.visible = this.options.grid
    if (this.floorRing) this.floorRing.visible = this.options.grid || this.options.focus
  }

  private frameInitialView(): void {
    const d = this.data
    if (!d) return
    // Open the way Stellarium does -- a wide field, pitched up thirty degrees,
    // horizon along the bottom -- facing whatever the plan observes first.
    const first = d.plan?.blocks[0]
    const row = first ? d.geometry.rows.find((r) => r.targetId === first.targetId) : undefined
    const slot = first?.slotStart ?? 0
    const az = row ? (row.azimuthDeg[slot] ?? 180) : 180
    this.view.cancelMotion()
    this.view.set({ azDeg: az, altDeg: 30, fovDeg: 120 })
  }

  // ----------------------------------------------------------- selection

  private currentTargetId(): string | null {
    return this.data?.plan?.slots[cursor.live.slot]?.targetId ?? null
  }

  private targetIndex(id: string | null): number {
    if (!id || !this.data) return -1
    return this.data.geometry.rows.findIndex((r) => r.targetId === id)
  }

  /** The horizon-frame direction of a selection, at the current instant. */
  private localDirOf(sel: SkySelection, out: Vector3): Vector3 | null {
    const d = this.data
    switch (sel.kind) {
      case 'target': {
        const i = this.targetIndex(sel.id)
        const v = this.targetDirs[i]
        return v ? out.copy(v).applyQuaternion(this.rigQInv) : null
      }
      case 'star': {
        const s = this.assets?.stars
        if (!s || sel.index >= s.count) return null
        const i = sel.index * 3
        return out
          .set(s.position[i] ?? 0, s.position[i + 1] ?? 0, s.position[i + 2] ?? 0)
          .applyQuaternion(this.rigQInv)
      }
      case 'planet': {
        const i = d?.sky?.planets.findIndex((p) => p.id === sel.id) ?? -1
        const v = this.planetLocal[i]
        return v ? out.copy(v) : null
      }
      case 'moon':
        return d ? out.copy(this.moonLocal) : null
      case 'satellite': {
        const s = this.satNow.find((x) => x.track.norad === sel.norad)
        return s ? out.copy(s.dir) : null
      }
      case 'catalog': {
        const v = this.catalogDirs[this.catalogIndex.get(sel.id) ?? -1]
        return v ? out.copy(v).applyQuaternion(this.rigQInv) : null
      }
    }
  }

  /** An aim for the view controller: re-evaluated every frame, so it tracks. */
  aimFor(sel: SkySelection): Aim {
    const v = new Vector3()
    return () => {
      const dir = this.localDirOf(sel, v)
      return dir ? toAltAz(dir) : null
    }
  }

  private projectLocal(local: Vector3, proj: Projection) {
    this._v.copy(local).applyQuaternion(this.viewQInv)
    return projectView(this._v, proj, this._p)
  }

  private radiusOf(sel: SkySelection, proj: Projection): number {
    const pointR = (m: number) =>
      Math.min(7, 0.7 * Math.pow(Math.pow(10, 0.4 * Math.max(0, this.limit - m)), 0.25)) *
      (this.shared.uPointScale.value / this.shared.uPixelRatio.value)
    switch (sel.kind) {
      case 'target':
        return 13
      case 'moon':
        return Math.max(5, 0.26 * Math.max(1, proj.fovDeg / 25) * pixelsPerDegree(proj))
      case 'planet': {
        const p = this.data?.sky?.planets.find((x) => x.id === sel.id)
        return Math.max(2.5, pointR(p?.magnitude ?? 0))
      }
      case 'satellite':
        return 4
      case 'catalog':
        return 6
      case 'star':
        return Math.max(2, pointR(this.assets?.stars?.magnitude[sel.index] ?? 6))
    }
  }

  private reticle(proj: Projection): FrameInfo['reticle'] {
    const sel = this.selection
    if (!sel) return null
    const dir = this.localDirOf(sel, new Vector3())
    if (!dir) return null
    const p = this.projectLocal(dir, proj)
    if (!p.visible || p.x < -50 || p.y < -50 || p.x > proj.width + 50 || p.y > proj.height + 50) {
      return null
    }
    return { x: p.x, y: p.y, r: this.radiusOf(sel, proj) }
  }

  /**
   * What is under the pointer, if anything.
   *
   * CPU picking against the same projection the shaders use: no GPU readback,
   * which would stall the pipeline. Targets win ties, then the Moon and the
   * planets, then satellites, then the brightest star in reach.
   */
  pick(x: number, y: number): SkySelection | null {
    const proj = this.projection()
    let best: SkySelection | null = null
    let bestScore = Infinity
    const consider = (sel: SkySelection, local: Vector3, reach: number, bias: number) => {
      if (this.data && this.hiddenByGround(local)) return
      const p = this.projectLocal(local, proj)
      if (!p.visible) return
      const dist = Math.hypot(p.x - x, p.y - y)
      if (dist > reach) return
      const score = dist + bias
      if (score < bestScore) {
        bestScore = score
        best = sel
      }
    }
    const v = new Vector3()
    const d = this.data
    if (d) {
      d.geometry.rows.forEach((r, i) => {
        const w = this.targetDirs[i]
        if (w) consider({ kind: 'target', id: r.targetId }, v.copy(w).applyQuaternion(this.rigQInv), 16, 0)
      })
      if (this.moonAlt > -3) {
        const r = this.radiusOf({ kind: 'moon' }, proj)
        consider({ kind: 'moon' }, v.copy(this.moonLocal), r + 8, 0)
      }
      d.sky?.planets.forEach((p, i) => {
        const w = this.planetLocal[i]
        if (w) consider({ kind: 'planet', id: p.id }, v.copy(w), 14, 1)
      })
      for (const s of this.satNow) {
        if (!Number.isNaN(s.mag)) consider({ kind: 'satellite', norad: s.track.norad }, v.copy(s.dir), 14, 2)
      }
      // Only what is drawn can be picked: at a wide field, the Messier objects.
      const all = proj.fovDeg <= SkyRenderer.CATALOG_ALL_FOV
      this.catalogObjs.forEach((o, i) => {
        const w = this.catalogDirs[i]
        if (!w || (!all && !this.catalogBright(i))) return
        consider({ kind: 'catalog', id: o.id }, v.copy(w).applyQuaternion(this.rigQInv), 10, 2.5)
      })
    }
    const stars = this.assets?.stars
    if (stars) {
      // world -> view in one matrix, applied by hand across ~40k stars.
      const m = this._m3.setFromMatrix4(this._mat4FromQuat(this.camQInv)).elements
      const pos = stars.position
      for (let i = 0; i < stars.count; i++) {
        const mag = stars.magnitude[i] ?? 9
        if (mag > this.limit - 0.3) continue
        const px = pos[i * 3] ?? 0
        const py = pos[i * 3 + 1] ?? 0
        const pz = pos[i * 3 + 2] ?? 0
        this._w.set(
          (m[0] ?? 0) * px + (m[3] ?? 0) * py + (m[6] ?? 0) * pz,
          (m[1] ?? 0) * px + (m[4] ?? 0) * py + (m[7] ?? 0) * pz,
          (m[2] ?? 0) * px + (m[5] ?? 0) * py + (m[8] ?? 0) * pz,
        )
        if (this._w.z > 0) continue
        const p = projectView(this._w, proj, this._p)
        const dist = Math.hypot(p.x - x, p.y - y)
        if (dist > 12) continue
        const score = dist + 3 + mag * 0.8
        if (score < bestScore) {
          if (d) {
            v.set(px, py, pz).applyQuaternion(this.rigQInv)
            if (this.hiddenByGround(v)) continue
          }
          bestScore = score
          best = { kind: 'star', index: i }
        }
      }
    }
    return best
  }

  private hiddenByGround(local: Vector3): boolean {
    if (!this.skyline) return false
    const { altDeg, azDeg } = toAltAz(local)
    return altDeg < skylineAt(this.skyline, azDeg) - 0.2
  }

  // ------------------------------------------------------------ describe

  /**
   * The numbers for the info card.
   *
   * Wherever the backend computed a value it is the backend's value for the
   * slot the cursor is in -- the same rule every other panel follows, so the
   * card never disagrees with the plan by a decimal. A star has no server
   * number; its altitude comes from the published frame and says so.
   */
  describe(sel: SkySelection): ObjectDescription | null {
    const d = this.data
    const slot = cursor.live.slot
    const t = cursor.live.tMs
    switch (sel.kind) {
      case 'target': {
        const target = d?.session.targets.find((x) => x.id === sel.id)
        const row = d?.geometry.rows.find((r) => r.targetId === sel.id)
        if (!d || !target || !row) return null
        const planned = d.plan?.blocks.filter((b) => b.targetId === sel.id) ?? []
        const now = this.currentTargetId() === sel.id
        const alt = row.altitudeDeg[slot] ?? 0
        const rows: InfoRow[] = [
          { label: 'Position now', value: altAz(alt, row.azimuthDeg[slot] ?? 0) },
          { label: 'Airmass', value: alt > 0 ? (row.airmass[slot] ?? 0).toFixed(2) : 'below horizon' },
          { label: 'Moon distance', value: `${(row.moonSeparationDeg[slot] ?? 0).toFixed(1)}°` },
          { label: 'RA', value: target.ra },
          { label: 'Dec', value: target.dec },
          // A star's magnitude is its TOTAL light, not light per square
          // arcsecond, and labelling it "surface brightness" states the exact
          // confusion `point_snr2_rate` exists to prevent -- on screen, to the
          // one person who could catch it.
          target.isPointSource
            ? { label: 'Magnitude', value: target.magnitude.toFixed(2), hint: 'total V' }
            : { label: 'Surface brightness', value: `${target.magnitude.toFixed(1)} mag/″²` },
        ]
        if (planned.length) {
          rows.push({
            label: 'Scheduled',
            value: planned.map((b) => `${b.startsAt.slice(11, 16)}–${b.endsAt.slice(11, 16)}`).join(', ') + ' UTC',
          })
        }
        return {
          title: target.name,
          subtitle: now ? 'Target · observing now' : planned.length ? 'Target · in tonight’s plan' : 'Target · not scheduled',
          rows,
          present: true,
        }
      }
      case 'planet': {
        const p = d?.sky?.planets.find((x) => x.id === sel.id)
        if (!p) return null
        const alt = p.altitudeDeg[slot] ?? 0
        return {
          title: p.name,
          subtitle: 'Planet',
          rows: [
            { label: 'Magnitude', value: p.magnitude.toFixed(1) },
            { label: 'Position now', value: altAz(alt, p.azimuthDeg[slot] ?? 0) },
            { label: 'Distance', value: `${p.distanceAu.toFixed(2)} AU` },
            { label: 'Phase angle', value: `${p.phaseAngleDeg.toFixed(0)}°` },
            { label: 'RA', value: p.ra, hint: 'apparent, mid-night' },
            { label: 'Dec', value: p.dec, hint: 'apparent, mid-night' },
          ],
          note: alt < 0 ? 'Below the horizon at this time.' : undefined,
          present: true,
        }
      }
      case 'moon': {
        if (!d) return null
        const m = d.session.moon
        const alt = m.altitudeDeg[slot] ?? 0
        return {
          title: 'Moon',
          subtitle: 'Earth’s satellite',
          rows: [
            { label: 'Illuminated', value: `${Math.round((m.illumination[slot] ?? 0) * 100)}%` },
            { label: 'Position now', value: altAz(alt, m.azimuthDeg[slot] ?? 0) },
            { label: 'Phase angle', value: `${(m.phaseAngleDeg[slot] ?? 0).toFixed(0)}°` },
            {
              label: 'Exclusion radius',
              value: `${d.session.site.minMoonSeparationDeg.toFixed(0)}°`,
              hint: 'targets inside the ring are not scheduled while it is up',
            },
          ],
          note: alt < 0 ? 'Below the horizon at this time.' : undefined,
          present: true,
        }
      }
      case 'satellite': {
        const sats = d?.satellites
        const live = this.satNow.find((s) => s.track.norad === sel.norad)
        const track = live?.track ?? this.tracks.find((x) => x.norad === sel.norad)
        if (!track) return null
        const i = live?.nearest ?? 0
        const mag = track.mag[i] ?? NaN
        const rows: InfoRow[] = [
          { label: 'NORAD', value: String(track.norad) },
          {
            label: 'Brightness',
            value: Number.isNaN(mag) ? 'in Earth’s shadow' : `≈ ${mag.toFixed(1)} mag`,
            hint: 'estimated from range and phase',
          },
          { label: 'Position now', value: live ? altAz(track.altDeg[i] ?? 0, track.azDeg[i] ?? 0) : '—' },
          { label: 'Range', value: live ? `${Math.round(track.rangeKm[i] ?? 0).toLocaleString()} km` : '—' },
          { label: 'This pass', value: `${utcHhmm(track.t0)}–${utcHhmm(track.t1)} UTC` },
        ]
        if (sats?.elementsPublishedAt) {
          rows.push({
            label: 'Elements',
            value: `${sats.source}, ${sats.elementsPublishedAt.slice(0, 16).replace('T', ' ')} UTC`,
          })
        }
        return {
          title: track.name,
          subtitle: 'Satellite',
          rows,
          note: live ? undefined : 'This pass is over; it is no longer above the horizon.',
          present: !!live && t >= track.t0 && t <= track.t1,
        }
      }
      case 'catalog': {
        const o = this.catalogObjs[this.catalogIndex.get(sel.id) ?? -1]
        if (!o) return null
        const rows: InfoRow[] = []
        const local = d ? this.localDirOf(sel, new Vector3()) : null
        if (local) {
          const aa = toAltAz(local)
          rows.push({ label: 'Position now', value: `≈ ${altAz(aa.altDeg, aa.azDeg)}`, hint: 'from the published frame' })
        }
        rows.push(
          { label: 'RA', value: o.ra },
          { label: 'Dec', value: o.dec },
          { label: 'Surface brightness', value: `${o.magnitude.toFixed(1)} mag/″²` },
        )
        if (o.majorArcmin != null) rows.push({ label: 'Size', value: `${o.majorArcmin.toFixed(1)}′` })
        if (o.vMag != null) rows.push({ label: 'Magnitude', value: o.vMag.toFixed(1) })
        return {
          title: o.name,
          subtitle: `${o.typeLabel} · ${o.constellation}`,
          rows,
          note: o.plannable ? 'Not in tonight’s plan.' : (o.whyNot ?? undefined),
          present: true,
        }
      }
      case 'star': {
        const s = this.assets?.stars
        if (!s || sel.index >= s.count) return null
        const i = sel.index
        const x = s.position[i * 3] ?? 0
        const y = s.position[i * 3 + 1] ?? 0
        const z = s.position[i * 3 + 2] ?? 0
        const ra = ((Math.atan2(y, x) * R2D) % 360 + 360) % 360
        const dec = Math.asin(Math.max(-1, Math.min(1, z))) * R2D
        const names = this.starNameByIndex.get(i)
        const bv = s.bv[i] ?? 0.65
        const rows: InfoRow[] = [
          { label: 'Magnitude', value: (s.magnitude[i] ?? 0).toFixed(2) },
          { label: 'Colour', value: `B−V ${bv.toFixed(2)} · ${spectralHint(bv)}` },
          { label: 'RA', value: sexagesimalRa(ra), hint: 'J2000' },
          { label: 'Dec', value: sexagesimalDec(dec), hint: 'J2000' },
        ]
        if (d) {
          const local = this.localDirOf(sel, new Vector3())
          if (local) {
            const aa = toAltAz(local)
            rows.push({ label: 'Position now', value: `≈ ${altAz(aa.altDeg, aa.azDeg)}`, hint: 'from the published frame' })
          }
        }
        return {
          title: names?.name || names?.desig || 'Star',
          subtitle: names?.name && names.desig ? `Star · ${names.desig}` : 'Star · Hipparcos',
          rows,
          present: true,
        }
      }
    }
  }

  /**
   * The coordinates an add needs, for a selection the server cannot name.
   *
   * Null for everything the server CAN name -- a target, a planet, a catalogue
   * object -- so the caller sends a name for those and coordinates only here.
   * The Moon and satellites get null too: they are unschedulable for reasons
   * no coordinate fixes.
   *
   * The id is built from the index into `stars.bin`, which is a generated
   * asset: rebuild the sky data and the same id could name a different star.
   * That is sound because the id is only ever used inside one session, which
   * stores the coordinates it was given rather than re-resolving the id.
   */
  plannable(sel: SkySelection): PlannableSelection | null {
    if (sel.kind !== 'star') return null
    const s = this.assets?.stars
    if (!s || sel.index >= s.count) return null
    const i = sel.index
    const x = s.position[i * 3] ?? 0
    const y = s.position[i * 3 + 1] ?? 0
    const z = s.position[i * 3 + 2] ?? 0
    const names = this.starNameByIndex.get(i)
    return {
      id: `star-${i}`,
      name: names?.name || names?.desig || `Star ${i}`,
      raDeg: (((Math.atan2(y, x) * R2D) % 360) + 360) % 360,
      decDeg: Math.asin(Math.max(-1, Math.min(1, z))) * R2D,
      magnitude: s.magnitude[i] ?? 0,
      // A star is THE point source. This is what stops the exposure model
      // reading its V magnitude as a surface brightness, which would be wrong
      // by a factor of about 25 in the optimistic direction.
      isPointSource: true,
    }
  }

  /** Targets, planets, the Moon, satellites in tonight's data, named stars. */
  search(query: string): SearchHit[] {
    const q = query.trim().toLowerCase()
    if (!q) return []
    const hits: SearchHit[] = []
    const d = this.data
    for (const r of d?.geometry.rows ?? []) {
      if (r.name.toLowerCase().includes(q) || r.targetId.toLowerCase().includes(q)) {
        hits.push({ selection: { kind: 'target', id: r.targetId }, label: r.name, kind: 'Target' })
      }
    }
    if (d && 'moon'.includes(q)) hits.push({ selection: { kind: 'moon' }, label: 'Moon', kind: 'Moon' })
    for (const p of d?.sky?.planets ?? []) {
      if (p.name.toLowerCase().includes(q)) {
        hits.push({ selection: { kind: 'planet', id: p.id }, label: p.name, kind: 'Planet' })
      }
    }
    const seen = new Set<number>()
    for (const tr of this.tracks) {
      if (seen.has(tr.norad) || !tr.name.toLowerCase().includes(q)) continue
      seen.add(tr.norad)
      hits.push({ selection: { kind: 'satellite', norad: tr.norad }, label: tr.name, kind: 'Satellite' })
    }
    let deep = 0
    for (const o of this.catalogObjs) {
      if (deep >= 6) break
      if (o.name.toLowerCase().includes(q) || o.designation.toLowerCase().includes(q) || o.id === q) {
        hits.push({ selection: { kind: 'catalog', id: o.id }, label: o.name, kind: o.typeLabel })
        deep++
      }
    }
    for (const [i, name, desig] of this.assets?.starNames ?? []) {
      if ((name && name.toLowerCase().includes(q)) || (desig && desig.toLowerCase() === q)) {
        hits.push({ selection: { kind: 'star', index: i }, label: name || desig, kind: 'Star' })
      }
      if (hits.length > 40) break
    }
    return hits.slice(0, 12)
  }

  /**
   * Fly the view to a selection. A satellite whose pass has not started is
   * met at its first moment above the horizon by moving TIME there too.
   */
  flyTo(sel: SkySelection, opts: { follow?: boolean; fovDeg?: number } = {}): void {
    if (sel.kind === 'satellite' && !this.satNow.some((s) => s.track.norad === sel.norad)) {
      const t = cursor.live.tMs
      const next = this.tracks.find((tr) => tr.norad === sel.norad && tr.t1 > t)
      if (next) cursor.setT(Math.max(next.t0 + 5000, t))
    }
    this.view.flyTo(this.aimFor(sel), { fovDeg: opts.fovDeg, thenFollow: opts.follow, ms: 900 })
    this.markDirty()
  }

  // --------------------------------------------------------------- labels

  private labelCandidates(proj: Projection): LabelCandidate[] {
    const out: LabelCandidate[] = []
    const d = this.data
    const v = new Vector3()
    const put = (
      id: string,
      text: string,
      kind: LabelCandidate['kind'],
      local: Vector3,
      r: number,
      priority: number,
      opts: { color?: string; centred?: boolean; ground?: boolean } = {},
    ) => {
      if (!opts.ground && d && this.hiddenByGround(local)) return
      const p = this.projectLocal(local, proj)
      if (!p.visible) return
      out.push({ id, text, kind, x: p.x, y: p.y, r, priority, color: opts.color, centred: opts.centred })
    }

    if (d) {
      // The compass, hanging just under the horizon like a sign on a fence.
      for (const c of CARDINALS) {
        put(`card-${c.name}`, c.name, 'cardinal', horizonVector(-2.2, c.az, v), 0, -100, { centred: true, ground: true })
      }
      for (const c of INTERCARDINALS) {
        put(`card-${c.name}`, c.name, 'grid', horizonVector(-2.2, c.az, v), 0, -90, { centred: true, ground: true })
      }
      if (this.options.grid) {
        const az = this.view.state.azDeg
        for (let alt = 15; alt < 90; alt += 15) {
          // After the targets: a grid label must never hide a target's name.
          put(`grid-alt-${alt}`, `${alt}°`, 'grid', horizonVector(alt, az, v), 0, -20, { centred: true })
        }
      }

      const current = this.currentTargetId()
      d.geometry.rows.forEach((r, i) => {
        const w = this.targetDirs[i]
        if (!w) return
        const isCurrent = r.targetId === current
        put(
          `t-${r.targetId}`,
          this.routeLabel(r.targetId, r.name, cursor.live.tMs),
          isCurrent ? 'target-active' : 'target',
          v.copy(w).applyQuaternion(this.rigQInv),
          isCurrent ? 18 : 12,
          isCurrent ? -60 : r.targetId === d.selectedTarget ? -55 : -50,
        )
      })

      if (this.moonAlt > -3) {
        put('moon', 'Moon', 'moon', this.moonLocal, this.radiusOf({ kind: 'moon' }, proj), -40)
      }
      d.sky?.planets.forEach((p, i) => {
        const w = this.planetLocal[i]
        if (!w || w.z < -0.05) return
        put(`pl-${p.id}`, p.name, 'planet', w, this.radiusOf({ kind: 'planet', id: p.id }, proj), -30 + p.magnitude)
      })

      // Satellites: the bright ones, and always the one you picked.
      for (const s of this.satNow) {
        if (Number.isNaN(s.mag)) continue
        const picked = this.selection?.kind === 'satellite' && this.selection.norad === s.track.norad
        if (!picked && s.mag > this.limit - 3.4 && s.track.norad !== 25544) continue
        put(`sat-${s.track.norad}`, prettySat(s.track.name), 'satellite', s.dir, 5, picked ? -58 : 5 + s.mag)
      }
    }

    // Catalogue objects: named once the field is narrow enough to tell them
    // apart, and the picked one always. At a whole-sky field there are
    // hundreds in view and their names would bury everything else.
    if (d && this.catalogObjs.length) {
      const zoomed = proj.fovDeg <= 40
      const picked = this.selection?.kind === 'catalog' ? this.selection.id : null
      this.catalogObjs.forEach((o, i) => {
        if (!zoomed && o.id !== picked) return
        const w = this.catalogDirs[i]
        if (!w) return
        put(`c-${o.id}`, o.name, 'catalog', v.copy(w).applyQuaternion(this.rigQInv), 6, o.id === picked ? -56 : 14)
      })
    }

    // Named stars, brightest first, down to a limit that deepens with zoom.
    const stars = this.assets?.stars
    if (stars) {
      const cut = this.limit - 4.6
      for (const [i, name] of this.assets?.starNames ?? []) {
        if (!name) continue
        const m = stars.magnitude[i] ?? 9
        const picked = this.selection?.kind === 'star' && this.selection.index === i
        if (m > cut && !picked) continue
        const local = v
          .set(stars.position[i * 3] ?? 0, stars.position[i * 3 + 1] ?? 0, stars.position[i * 3 + 2] ?? 0)
          .applyQuaternion(this.rigQInv)
        const [r, g, b] = bvToLinearRgb(stars.bv[i] ?? 0.65)
        const color = `rgb(${Math.round((0.55 + 0.45 * toSrgb1(r)) * 255)}, ${Math.round(
          (0.55 + 0.45 * toSrgb1(g)) * 255,
        )}, ${Math.round((0.55 + 0.45 * toSrgb1(b)) * 255)})`
        put(`st-${i}`, name, 'star', local, this.radiusOf({ kind: 'star', index: i }, proj), picked ? -58 : 10 + m, { color })
      }
    }

    // Constellation names last: they are the first thing to give way.
    for (const [k, c] of (this.assets?.constellationNames ?? []).entries()) {
      const local = icrsDir(c.raDeg, c.decDeg, v).applyQuaternion(this.rigQInv)
      put(`con-${k}`, c.name.toUpperCase(), 'constellation', local, 0, 40, { centred: true })
    }
    return out
  }
}

function smooth(e0: number, e1: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - e0) / (e1 - e0)))
  return t * t * (3 - 2 * t)
}

/** "ISS (ZARYA)" -> "ISS"; "CSS (TIANHE)" -> "Tiangong (Tianhe)"; rocket bodies as-is. */
function prettySat(name: string): string {
  const n = name.trim()
  if (n.startsWith('ISS')) return 'ISS'
  if (n.startsWith('CSS')) return 'Tiangong'
  if (n === 'HST') return 'Hubble'
  return n
}
