/**
 * Where the observer is looking: an azimuth, an altitude and a field of view.
 *
 * The view lives in the observer's HORIZON frame (x East, y North, z Up), not
 * on the celestial sphere. That is the whole of "standing on the ground": the
 * landscape never moves, the stars wheel past, and looking at the southern
 * sky means looking at azimuth 180 however late it gets. There is no roll --
 * the horizon is always level, the way it is for a person.
 *
 * Panning GRABS the sky: the point under the pointer when it went down stays
 * under the pointer as it moves. It is solved by correction rather than in
 * closed form -- nudge azimuth and altitude by how far the grabbed point has
 * drifted, twice per event -- which converges in one or two steps and never
 * rolls the view the way a free trackball does.
 */

import { Matrix4, Quaternion, Vector3 } from 'three'
import { MAX_FOV_DEG, MIN_FOV_DEG, unprojectView, type Projection } from './projection'

const D2R = Math.PI / 180
const R2D = 180 / Math.PI

export interface ViewState {
  azDeg: number
  altDeg: number
  fovDeg: number
}

/** A target the view can fly to or follow, re-read every frame. */
export type Aim = () => { azDeg: number; altDeg: number } | null

const wrap180 = (a: number): number => ((((a + 180) % 360) + 360) % 360) - 180
const clampAlt = (a: number): number => Math.max(-90, Math.min(90, a))
export const clampFov = (f: number): number => Math.max(MIN_FOV_DEG, Math.min(MAX_FOV_DEG, f))

export const smoothstep = (x: number): number => {
  const t = Math.max(0, Math.min(1, x))
  return t * t * (3 - 2 * t)
}

/** Unit vector in the horizon frame (x East, y North, z Up). */
export function horizonVector(altDeg: number, azDeg: number, out = new Vector3()): Vector3 {
  const a = altDeg * D2R
  const z = azDeg * D2R
  return out.set(Math.cos(a) * Math.sin(z), Math.cos(a) * Math.cos(z), Math.sin(a))
}

export function toAltAz(v: Vector3): { altDeg: number; azDeg: number } {
  const n = v.length() || 1
  return {
    altDeg: Math.asin(Math.max(-1, Math.min(1, v.z / n))) * R2D,
    azDeg: ((Math.atan2(v.x, v.y) * R2D) % 360 + 360) % 360,
  }
}

export class ViewController {
  readonly state: ViewState = { azDeg: 180, altDeg: 30, fovDeg: 120 }

  /** Bumped on every change, so the renderer knows to draw. */
  version = 0

  /** True once the person has panned, zoomed or flown anywhere themselves. */
  touched = false

  private flight: {
    from: ViewState
    aim: Aim
    toFov: number | null
    t0: number
    ms: number
  } | null = null
  private follow: Aim | null = null
  private zoomTarget: number | null = null
  private zoomAnchor: { x: number; y: number; dir: Vector3 } | null = null
  private grab: Vector3 | null = null

  private readonly _m = new Matrix4()
  private readonly _f = new Vector3()
  private readonly _u = new Vector3()
  private readonly _r = new Vector3()
  private readonly _v = new Vector3()

  set(patch: Partial<ViewState>): void {
    if (patch.azDeg !== undefined) this.state.azDeg = ((patch.azDeg % 360) + 360) % 360
    if (patch.altDeg !== undefined) this.state.altDeg = clampAlt(patch.altDeg)
    if (patch.fovDeg !== undefined) this.state.fovDeg = clampFov(patch.fovDeg)
    this.version++
  }

  /** The camera's orientation in the horizon frame (three.js looks down -Z). */
  orientation(out: Quaternion): Quaternion {
    const { altDeg, azDeg } = this.state
    const a = altDeg * D2R
    const z = azDeg * D2R
    horizonVector(altDeg, azDeg, this._f)
    // d(forward)/d(alt): "up" on screen, well defined even at the zenith.
    this._u.set(-Math.sin(a) * Math.sin(z), -Math.sin(a) * Math.cos(z), Math.cos(a))
    this._r.copy(this._f).cross(this._u)
    this._m.makeBasis(this._r, this._u, this._v.copy(this._f).negate())
    return out.setFromRotationMatrix(this._m)
  }

  /** The horizon-frame direction under a screen point. */
  directionAt(x: number, y: number, proj: Projection, out = new Vector3()): Vector3 {
    const q = this.orientation(new Quaternion())
    return unprojectView(x, y, proj, out).applyQuaternion(q)
  }

  // -- panning ---------------------------------------------------------------

  beginGrab(x: number, y: number, proj: Projection): void {
    this.touched = true
    this.cancelMotion()
    this.grab = this.directionAt(x, y, proj)
  }

  dragTo(x: number, y: number, proj: Projection): void {
    if (!this.grab) return
    this.keepUnder(this.grab, x, y, proj)
  }

  endGrab(): void {
    this.grab = null
  }

  get grabbing(): boolean {
    return this.grab !== null
  }

  /** Turn the view so that horizon direction `dir` sits at screen point (x, y). */
  private keepUnder(dir: Vector3, x: number, y: number, proj: Projection): void {
    const want = toAltAz(dir)
    for (let i = 0; i < 3; i++) {
      const got = toAltAz(this.directionAt(x, y, proj, this._v))
      const dAz = wrap180(want.azDeg - got.azDeg)
      const dAlt = want.altDeg - got.altDeg
      // Near the zenith azimuth is ill-conditioned; damp it so the view does
      // not spin when the grabbed point passes overhead.
      const damp = Math.min(1, Math.cos(got.altDeg * D2R) * 4)
      this.state.azDeg = (((this.state.azDeg + dAz * damp) % 360) + 360) % 360
      this.state.altDeg = clampAlt(this.state.altDeg + dAlt)
    }
    this.version++
  }

  // -- zoom --------------------------------------------------------------------

  /**
   * Zoom by `factor` (> 1 narrows the field), keeping the sky under the
   * pointer where it is. Eased: the field approaches its target over a few
   * frames rather than jumping, and the anchor is re-applied every frame.
   */
  zoomAt(factor: number, x: number, y: number, proj: Projection): void {
    this.touched = true
    this.flight = null
    const from = this.zoomTarget ?? this.state.fovDeg
    this.zoomTarget = clampFov(from / factor)
    this.zoomAnchor = { x, y, dir: this.directionAt(x, y, proj) }
  }

  // -- flying and following ----------------------------------------------------

  /** Animate to wherever `aim` says, optionally changing the field. */
  flyTo(aim: Aim, opts: { fovDeg?: number; ms?: number; thenFollow?: boolean } = {}): void {
    this.touched = true
    this.zoomTarget = null
    this.zoomAnchor = null
    this.follow = opts.thenFollow ? aim : null
    this.flight = {
      from: { ...this.state },
      aim,
      toFov: opts.fovDeg === undefined ? null : clampFov(opts.fovDeg),
      t0: performance.now(),
      ms: opts.ms ?? 900,
    }
  }

  setFollow(aim: Aim | null): void {
    this.follow = aim
    this.version++
  }

  get following(): boolean {
    return this.follow !== null
  }

  cancelMotion(): void {
    this.flight = null
    this.follow = null
    this.zoomTarget = null
    this.zoomAnchor = null
  }

  /** Advance animations. Returns true while anything is still moving. */
  update(now: number, proj: Projection): boolean {
    let moving = false

    if (this.flight) {
      const f = this.flight
      const to = f.aim()
      const u = smoothstep((now - f.t0) / f.ms)
      if (to) {
        this.state.azDeg = (((f.from.azDeg + wrap180(to.azDeg - f.from.azDeg) * u) % 360) + 360) % 360
        this.state.altDeg = clampAlt(f.from.altDeg + (to.altDeg - f.from.altDeg) * u)
      }
      if (f.toFov !== null) {
        // Logarithmic in field, so zooming 120 -> 2 degrees does not spend
        // nine-tenths of the animation in the first few percent of the range.
        this.state.fovDeg = Math.exp(
          Math.log(f.from.fovDeg) + (Math.log(f.toFov) - Math.log(f.from.fovDeg)) * u,
        )
      }
      this.version++
      if (u >= 1) this.flight = null
      moving = true
    } else if (this.follow && !this.grab) {
      const to = this.follow()
      if (to) {
        const dAz = wrap180(to.azDeg - this.state.azDeg)
        const dAlt = to.altDeg - this.state.altDeg
        if (Math.abs(dAz) > 1e-6 || Math.abs(dAlt) > 1e-6) {
          this.state.azDeg = (((this.state.azDeg + dAz) % 360) + 360) % 360
          this.state.altDeg = clampAlt(this.state.altDeg + dAlt)
          this.version++
        }
      }
    }

    if (this.zoomTarget !== null) {
      const cur = this.state.fovDeg
      const next = Math.exp(Math.log(cur) + (Math.log(this.zoomTarget) - Math.log(cur)) * 0.28)
      const done = Math.abs(Math.log(next / this.zoomTarget)) < 0.002
      this.state.fovDeg = done ? this.zoomTarget : next
      if (this.zoomAnchor && !this.follow) {
        this.keepUnder(this.zoomAnchor.dir, this.zoomAnchor.x, this.zoomAnchor.y, proj)
      }
      this.version++
      if (done) {
        this.zoomTarget = null
        this.zoomAnchor = null
      } else {
        moving = true
      }
    }
    return moving
  }
}
