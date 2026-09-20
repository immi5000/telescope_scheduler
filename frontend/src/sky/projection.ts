/**
 * The stereographic projection: what makes this look like standing outside.
 *
 * A perspective camera cannot show more than about 100 degrees before the
 * edges smear, and a person standing in a field sees far more sky than that.
 * The stereographic projection maps the sphere onto the screen from the point
 * directly BEHIND the viewer, which keeps small shapes true (a constellation
 * looks like itself anywhere on screen) and lets the field open past 180
 * degrees -- where the horizon bends into the circle that says "you are
 * looking up".
 *
 * Every vertex shader in the scene ends in `stereoProject`, and the labels and
 * the picking use the CPU mirror below, so the two cannot disagree about where
 * anything is. For a unit view vector `d` (camera looking down -z):
 *
 *     p = 2 * d.xy / (1 - d.z)          radius |p| = 2 tan(theta / 2)
 *     ndc = p / (2 tan(fov / 4))        fov across the SMALLER screen side
 *
 * The one hazard is the point directly behind the viewer, which maps to
 * infinity. Anything more than CULL_DEG from the view direction is flagged, and
 * every fragment shader discards on the flag -- a line whose two ends straddle
 * that point would otherwise be drawn clean across the screen.
 */

import { Vector3 } from 'three'

/** Largest field anyone can zoom out to, across the smaller screen side. */
export const MAX_FOV_DEG = 185
export const MIN_FOV_DEG = 0.6

/**
 * Beyond this angle from the view direction a vertex is culled. At the widest
 * field on a 21:9 screen the far corner is about 137 degrees out, so 150 never
 * clips anything visible.
 */
export const CULL_DEG = 150
const CULL_COS = Math.cos((CULL_DEG * Math.PI) / 180)

export const STEREO_UNIFORMS = /* glsl */ `
  uniform float uStereoK;   // 1 / (2 tan(fov/4))
  uniform float uAspect;    // screen width / height
  uniform float uLandscape; // 1 when the height is the smaller side
`

/**
 * `vCull` must be declared as a varying by the caller. It is 1 for a vertex
 * too close to the point behind the viewer, and fragments discard on it.
 */
export const STEREO_FN = /* glsl */ `
  vec4 stereoProject(vec3 viewPos, out float cull) {
    vec3 d = normalize(viewPos);
    // d.z = -cos(theta): cull when theta is past CULL_DEG.
    cull = step(${(-CULL_COS).toFixed(6)}, d.z);
    float den = max(1.0 - d.z, 0.02);
    vec2 p = 2.0 * d.xy / den;
    vec2 ndc = p * uStereoK;
    ndc.x = uLandscape > 0.5 ? ndc.x / uAspect : ndc.x;
    ndc.y = uLandscape > 0.5 ? ndc.y : ndc.y * uAspect;
    return vec4(ndc, 0.0, 1.0);
  }
`

/**
 * Inverse, for full-screen passes: NDC to a unit view vector.
 *
 * Solving p = 2 d.xy / (1 - d.z) with |d| = 1 gives, with r2 = |p|^2,
 * d = (4 p, r2 - 4) / (r2 + 4) -- which at the screen centre is (0, 0, -1),
 * straight ahead.
 */
export const STEREO_INVERSE_FN = /* glsl */ `
  vec3 stereoUnproject(vec2 ndc) {
    vec2 n = ndc;
    n.x = uLandscape > 0.5 ? n.x * uAspect : n.x;
    n.y = uLandscape > 0.5 ? n.y : n.y / uAspect;
    vec2 p = n / uStereoK;
    float r2 = dot(p, p);
    return vec3(4.0 * p, r2 - 4.0) / (r2 + 4.0);
  }
`

/** The projection's parameters for one frame, shared by GPU and CPU. */
export interface Projection {
  /** Field of view across the smaller screen side, degrees. */
  fovDeg: number
  width: number
  height: number
}

export const stereoK = (fovDeg: number): number =>
  1 / (2 * Math.tan((fovDeg * Math.PI) / 180 / 4))

/**
 * View-space unit vector to CSS pixels. `visible` is false behind the cull
 * cone; off-screen points are still returned so labels can test their boxes.
 */
export function projectView(
  d: Vector3,
  proj: Projection,
  out: { x: number; y: number; visible: boolean } = { x: 0, y: 0, visible: false },
): { x: number; y: number; visible: boolean } {
  const len = d.length() || 1
  const dz = d.z / len
  out.visible = dz < -CULL_COS
  const den = Math.max(1 - dz, 0.02)
  const k = stereoK(proj.fovDeg)
  let nx = ((2 * d.x) / len / den) * k
  let ny = ((2 * d.y) / len / den) * k
  const aspect = proj.width / proj.height
  if (aspect >= 1) nx /= aspect
  else ny *= aspect
  out.x = (nx * 0.5 + 0.5) * proj.width
  out.y = (-ny * 0.5 + 0.5) * proj.height
  return out
}

/** CSS pixels to a view-space unit vector. The exact inverse of `projectView`. */
export function unprojectView(x: number, y: number, proj: Projection, out = new Vector3()): Vector3 {
  let nx = (x / proj.width) * 2 - 1
  let ny = 1 - (y / proj.height) * 2
  const aspect = proj.width / proj.height
  if (aspect >= 1) nx *= aspect
  else ny /= aspect
  const k = stereoK(proj.fovDeg)
  const px = nx / k
  const py = ny / k
  const r2 = px * px + py * py
  return out.set((4 * px) / (r2 + 4), (4 * py) / (r2 + 4), (r2 - 4) / (r2 + 4))
}

/** Pixels per degree at the centre of the screen, for sizing things in degrees. */
export function pixelsPerDegree(proj: Projection): number {
  const side = Math.min(proj.width, proj.height)
  // d(ndc)/d(theta) at theta = 0 is k (since r = 2 tan(theta/2) ~ theta).
  return ((stereoK(proj.fovDeg) * side) / 2) * (Math.PI / 180)
}
