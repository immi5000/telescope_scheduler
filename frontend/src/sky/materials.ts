/**
 * Every material in the first-person sky.
 *
 * All of them end in the stereographic projection (see `projection.ts`), so
 * none of them uses three's projection matrix. The camera still exists -- it
 * is how three hands each object its `modelViewMatrix`, which carries the
 * horizon rig's rotation for free -- but where a vertex lands on screen is
 * decided here, by one function every layer shares.
 */

import {
  AdditiveBlending,
  Color,
  CustomBlending,
  Matrix3,
  NormalBlending,
  OneFactor,
  OneMinusSrcAlphaFactor,
  ShaderMaterial,
  Texture,
  Vector2,
  Vector3,
} from 'three'
import { STEREO_FN, STEREO_INVERSE_FN, STEREO_UNIFORMS } from './projection'
import { COLOR_FN, EQUIRECT_FN, EQUIRECT_UNIFORMS } from './shaders/common'

export type Uniform<T> = { value: T }
export const u = <T,>(value: T): Uniform<T> => ({ value })

/** Uniforms every material reads, shared by reference so one write updates all. */
export interface Shared {
  uStereoK: Uniform<number>
  uAspect: Uniform<number>
  uLandscape: Uniform<number>
  uPixelRatio: Uniform<number>
  /** Device pixels, for thick lines. */
  uResolution: Uniform<Vector2>
  /** 1 normally; eases toward 0.3 in "focus on plan" for context layers. */
  uContext: Uniform<number>
  /** CSS-to-device scale for point sizes, including a screen-size factor. */
  uPointScale: Uniform<number>
}

export function makeShared(): Shared {
  return {
    uStereoK: u(1),
    uAspect: u(1),
    uLandscape: u(1),
    uPixelRatio: u(1),
    uResolution: u(new Vector2(1, 1)),
    uContext: u(1),
    uPointScale: u(1),
  }
}

const sharedUniforms = (s: Shared) => ({ ...s })

const PROJECT = /* glsl */ `
  varying float vCull;
  ${STEREO_UNIFORMS}
  ${STEREO_FN}
`

/** Premultiplied "over" -- for layers that must hide what is behind them. */
const premultipliedOver = {
  blending: CustomBlending,
  blendSrc: OneFactor,
  blendDst: OneMinusSrcAlphaFactor,
} as const

// ---------------------------------------------------------------- full screen

const FULLSCREEN_VERTEX = /* glsl */ `
  varying vec2 vNdc;
  void main() {
    vNdc = position.xy;
    gl_Position = vec4(position.xy, 0.0, 1.0);
  }
`

/**
 * The sky itself: atmosphere and Milky Way, one pass, every pixel.
 *
 * Its BRIGHTNESS is the backend's own number: the cloud-free zenith sky
 * brightness per slot, from the same Krisciunas-Schaefer, twilight and
 * light-pollution model the scheduler uses, mapped to display luminance on a
 * compressive curve (the eye's, roughly a square root). Its SHAPE -- brighter
 * toward the horizon, warm toward the Sun at dusk, a glow around the Moon --
 * is a rendering of where that light is, and nothing reads a number off it.
 */
export function skyMaterial(s: Shared): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    blending: NormalBlending,
    transparent: false,
    uniforms: {
      ...sharedUniforms(s),
      uViewToWorld: u(new Matrix3()),
      uViewToLocal: u(new Matrix3()),
      uMap: u<Texture | null>(null),
      uHasMap: u(0),
      uRaAtLeft: u(180),
      uRaDir: u(-1),
      uMwGain: u(0.5),
      uMwFloor: u(0.009),
      uHasFrame: u(0),
      uSkyY: u(0.016),
      uSunLocal: u(new Vector3(0, 0, -1)),
      uSunAlt: u(-90),
      uMoonLocal: u(new Vector3(0, 0, -1)),
      uMoonAlt: u(-90),
      uMoonIllum: u(0),
      uLpWarm: u(0.3),
    },
    vertexShader: FULLSCREEN_VERTEX,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying vec2 vNdc;
      ${STEREO_UNIFORMS}
      ${STEREO_INVERSE_FN}
      ${EQUIRECT_UNIFORMS}
      ${EQUIRECT_FN}
      ${COLOR_FN}
      uniform mat3 uViewToWorld;
      uniform mat3 uViewToLocal;
      uniform float uHasMap;
      uniform float uMwGain;
      uniform float uMwFloor;
      uniform float uContext;
      uniform float uHasFrame;
      uniform float uSkyY;
      uniform vec3 uSunLocal;
      uniform float uSunAlt;
      uniform vec3 uMoonLocal;
      uniform float uMoonAlt;
      uniform float uMoonIllum;
      uniform float uLpWarm;

      void main() {
        vec3 dv = stereoUnproject(vNdc);
        vec3 w = normalize(uViewToWorld * dv);
        vec3 l = normalize(uViewToLocal * dv);
        float s = l.z;
        float up = clamp(s, 0.0, 1.0);

        vec3 col = vec3(0.0);
        if (uHasFrame > 0.5) {
          // Night: a cold blue-grey, warming toward the horizon where light
          // pollution concentrates. Twilight: deeper blue overhead.
          float twi = smoothstep(-19.0, -6.0, uSunAlt);
          float day = smoothstep(-6.0, 6.0, uSunAlt);
          vec3 night = vec3(0.68, 0.99, 1.95);
          vec3 lp = vec3(1.35, 1.05, 0.72);
          vec3 twilightTint = vec3(0.45, 0.85, 2.3);
          vec3 dayTint = vec3(0.55, 0.95, 1.75);
          float horizon = pow(1.0 - up, 3.0);
          vec3 tint = mix(night, lp, uLpWarm * horizon * (1.0 - twi));
          tint = mix(tint, twilightTint, twi * (1.0 - horizon * 0.5));
          tint = mix(tint, dayTint, day);

          // Brighter toward the horizon: a longer path through glowing air.
          float grad = 1.0 + 1.9 * horizon;
          col = uSkyY * grad * tint;

          // Toward the Sun at dusk and dawn, low and warm. A tint toward
          // orange rather than extra light, so the horizon glows without
          // clipping to white.
          float toSun = max(dot(l, uSunLocal), 0.0);
          float glow = twi * (1.0 - day * 0.6) * pow(toSun, 5.0) * pow(1.0 - up, 3.0);
          vec3 warm = vec3(1.0, 0.5, 0.2) * clamp(uSkyY * 3.2, 0.0, 0.85);
          col = mix(col, warm, clamp(glow * 0.85, 0.0, 0.85));
          col += twi * pow(toSun, 2.0) * pow(1.0 - up, 2.0) * uSkyY * vec3(0.9, 0.55, 0.35);

          // The Moon's aureole: forward-scattered moonlight close to it.
          float moonUp = smoothstep(-3.0, 4.0, uMoonAlt);
          float cm = max(dot(l, uMoonLocal), 0.0);
          float aureole = uMoonIllum * moonUp * (0.35 * pow(cm, 16.0) + 0.12 * pow(cm, 3.0));
          col += aureole * uSkyY * vec3(1.1, 1.3, 1.9);

          // A faint band of haze right at the horizon.
          col += uSkyY * 0.6 * smoothstep(0.16, 0.0, abs(s)) * tint;
        }

        if (uHasMap > 0.5) {
          vec3 mw = max(sampleSky(w) - uMwFloor, 0.0) * uMwGain * uContext;
          // Extinction: the Milky Way fades into the murk near the horizon.
          float ext = uHasFrame > 0.5 ? smoothstep(-0.02, 0.35, s) : 1.0;
          col += mw * ext;
        }

        gl_FragColor = vec4(toSrgb(col), 1.0);
      }
    `,
  })
}

/**
 * The ground: generated hills, trees and two roofs, opaque, drawn over
 * everything in the sky so that anything below the skyline is hidden -- the
 * way it is when you are standing in a field.
 */
export function landscapeMaterial(s: Shared, skyline: Texture): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    ...premultipliedOver,
    uniforms: {
      ...sharedUniforms(s),
      uViewToLocal: u(new Matrix3()),
      uSkyline: u(skyline),
      uGroundY: u(0.004),
      uHorizonCol: u(new Vector3(0.02, 0.03, 0.05)),
      uAlpha: u(1),
    },
    vertexShader: FULLSCREEN_VERTEX,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying vec2 vNdc;
      ${STEREO_UNIFORMS}
      ${STEREO_INVERSE_FN}
      ${COLOR_FN}
      uniform mat3 uViewToLocal;
      uniform sampler2D uSkyline;
      uniform float uGroundY;
      uniform vec3 uHorizonCol;
      uniform float uAlpha;

      float hash(vec2 p) {
        return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
      }

      void main() {
        vec3 l = normalize(uViewToLocal * stereoUnproject(vNdc));
        float alt = degrees(asin(clamp(l.z, -1.0, 1.0)));
        float az = atan(l.x, l.y);
        vec2 prof = texture2D(uSkyline, vec2(fract(az / 6.28318530718), 0.5)).rg;

        float aa = max(fwidth(alt) * 0.8, 1e-4);
        float nearM = 1.0 - smoothstep(prof.g - aa, prof.g + aa, alt);
        float farM = 1.0 - smoothstep(prof.r - aa, prof.r + aa, alt);
        float groundM = 1.0 - smoothstep(-aa, aa, alt);
        float cover = max(max(nearM, farM), groundM);
        if (cover < 0.003) discard;

        // Grass: darker toward your feet, with a little texture so the
        // ground reads as a surface rather than a hole.
        vec3 grass = vec3(0.55, 0.66, 0.42) * uGroundY;
        float depth = smoothstep(-60.0, 0.0, alt);
        float grain = hash(floor(vec2(az * 900.0, alt * 14.0)));
        vec3 ground = grass * (0.55 + 0.45 * depth) * (0.9 + 0.2 * grain);
        // Distant hills sit in the haze, so they take the sky's colour.
        vec3 far = mix(grass * 1.4, uHorizonCol, 0.55);
        // Trees and roofs: the darkest thing in the scene.
        vec3 trees = grass * 0.45;

        vec3 col = ground;
        col = mix(col, far, farM * (1.0 - groundM));
        col = mix(col, trees, nearM);
        float a = cover * uAlpha;
        gl_FragColor = vec4(toSrgb(col) * a, a);
      }
    `,
  })
}

// ------------------------------------------------------------------- points

/**
 * How big and how bright a point of magnitude m is, given the limit.
 *
 * `delta` is how far above the current limiting magnitude the object is, so
 * everything -- stars, planets, satellites -- shrinks and fades together as
 * the sky brightens, and grows as you zoom in. The radius follows a fourth
 * root of flux: a hundredfold brighter star is about three times wider, which
 * is what lets both Sirius and a fifth-magnitude star be legible at once.
 */
const MAG_FN = /* glsl */ `
  uniform float uLimit;
  uniform float uPointScale;
  uniform float uPixelRatio;
  void magPoint(float delta, out float radius, out float alpha) {
    float flux = pow(10.0, 0.4 * clamp(delta, -3.0, 12.0));
    radius = min(0.7 * pow(flux, 0.25), 7.0);
    alpha = clamp(0.2 + 0.3 * delta, 0.0, 1.0);
  }
`

const POINT_FRAGMENT = /* glsl */ `
  precision highp float;
  varying float vCull;
  varying vec3 vColor;
  varying float vAlpha;
  varying float vRadius;
  varying float vSize;
  varying float vHalo;
  void main() {
    if (vCull > 0.5 || vAlpha <= 0.0) discard;
    float d = length(gl_PointCoord - 0.5) * vSize;
    float core = 1.0 - smoothstep(vRadius - 0.7, vRadius + 0.7, d);
    float sig = max(vRadius * 1.8, 1.5);
    float halo = vHalo * exp(-d * d / (2.0 * sig * sig));
    float a = clamp(core + halo, 0.0, 1.0) * vAlpha;
    if (a < 0.003) discard;
    gl_FragColor = vec4(vColor * a, a);
  }
`

/**
 * Stars. Static buffer in ICRS; only uniforms change.
 *
 * Atmospheric extinction is applied per star from its altitude -- the site's
 * own extinction coefficient times the airmass -- which is why stars thin out
 * toward the horizon exactly as they do outside.
 */
export function starMaterial(s: Shared): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    blending: AdditiveBlending,
    uniforms: {
      ...sharedUniforms(s),
      uLimit: u(6),
      uZenith: u(new Vector3(0, 0, 1)),
      uExtK: u(0.2),
      uHorizonLoss: u(1.2),
      uHasFrame: u(0),
    },
    vertexShader: /* glsl */ `
      attribute float aMag;
      attribute vec3 aColor;
      varying vec3 vColor;
      varying float vAlpha;
      varying float vRadius;
      varying float vSize;
      varying float vHalo;
      uniform vec3 uZenith;
      uniform float uExtK;
      uniform float uHorizonLoss;
      uniform float uHasFrame;
      uniform float uContext;
      ${PROJECT}
      ${MAG_FN}
      void main() {
        vec3 dir = normalize(position);
        float m = aMag;
        float lim = uLimit;
        if (uHasFrame > 0.5) {
          float h = clamp(dot(dir, uZenith), 0.0, 1.0);
          // Kasten-Young-shaped airmass, finite at the horizon.
          float X = 1.0 / (h + 0.025 * exp(-11.0 * h));
          m += uExtK * (X - 1.0);
          lim -= uHorizonLoss * pow(1.0 - h, 3.0);
        }
        float delta = lim - m;
        float r; float a;
        magPoint(delta, r, a);
        // In focus mode the faint field recedes; the bright stars you steer by stay.
        a *= mix(uContext, 1.0, smoothstep(3.5, 1.5, aMag));
        vColor = aColor;
        vAlpha = delta < -0.4 ? 0.0 : a;
        vRadius = r * uPointScale;
        vHalo = 0.22 * smoothstep(2.5, 7.0, delta);
        vSize = (vRadius + 1.0) * 2.0 * (vHalo > 0.0 ? 3.2 : 1.4) + 2.0;
        gl_PointSize = vAlpha > 0.0 ? vSize : 0.0;
        gl_Position = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, vCull);
      }
    `,
    fragmentShader: POINT_FRAGMENT,
  })
}

/**
 * Planets and satellites: a handful of points in the HORIZON frame, each with
 * its own magnitude and colour, rewritten every frame.
 */
export function bodyMaterial(s: Shared, opts: { contextual: boolean }): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    blending: AdditiveBlending,
    uniforms: {
      ...sharedUniforms(s),
      uLimit: u(6),
      uExtK: u(0.2),
      uUseContext: u(opts.contextual ? 1 : 0),
    },
    vertexShader: /* glsl */ `
      attribute float aMag;
      attribute vec3 aColor;
      attribute float aVisible;
      varying vec3 vColor;
      varying float vAlpha;
      varying float vRadius;
      varying float vSize;
      varying float vHalo;
      uniform float uExtK;
      uniform float uUseContext;
      uniform float uContext;
      ${PROJECT}
      ${MAG_FN}
      void main() {
        vec3 dir = normalize(position);
        float h = clamp(dir.z, 0.0, 1.0);
        float X = 1.0 / (h + 0.025 * exp(-11.0 * h));
        float delta = uLimit - (aMag + uExtK * (X - 1.0));
        float r; float a;
        magPoint(delta, r, a);
        // Planets never vanish entirely: they are what people go outside to find.
        vAlpha = max(a, 0.35) * aVisible * (uUseContext > 0.5 ? uContext : 1.0);
        vRadius = max(r, 1.1) * uPointScale;
        vHalo = 0.28 * smoothstep(3.0, 8.0, delta);
        vColor = aColor;
        vSize = (vRadius + 1.0) * 2.0 * 3.2 + 2.0;
        gl_PointSize = vAlpha > 0.0 ? vSize : 0.0;
        gl_Position = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, vCull);
      }
    `,
    fragmentShader: POINT_FRAGMENT,
  })
}

/**
 * The Moon: a lit disc with the right phase, its bright limb turned toward
 * the Sun, drawn larger than life at wide fields so the phase is legible.
 */
export function moonMaterial(s: Shared): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    ...premultipliedOver,
    uniforms: {
      ...sharedUniforms(s),
      uIllum: u(0.5),
      uSunDir2: u(new Vector2(1, 0)),
      uDiscPx: u(12),
      uBright: u(1),
    },
    vertexShader: /* glsl */ `
      uniform float uDiscPx;
      uniform float uPixelRatio;
      varying float vSize;
      ${PROJECT}
      void main() {
        vSize = uDiscPx * uPixelRatio * 2.0 * 3.0;
        gl_PointSize = vSize;
        gl_Position = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, vCull);
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying float vCull;
      varying float vSize;
      uniform float uIllum;
      uniform vec2 uSunDir2;
      uniform float uBright;
      ${COLOR_FN}
      void main() {
        if (vCull > 0.5) discard;
        // Sprite spans 3 disc radii each way; p is in disc radii.
        vec2 p = (gl_PointCoord - 0.5) * 6.0;
        p.y = -p.y;
        float r = length(p);
        // Rotate so +x points at the Sun.
        vec2 q = vec2(dot(p, uSunDir2), dot(p, vec2(-uSunDir2.y, uSunDir2.x)));
        float k = clamp(uIllum, 0.0, 1.0);
        // The terminator is an ellipse of semi-axis (1 - 2k): a crescent below
        // half phase, gibbous above. A straight chord is right only at k = 0.5.
        float edge = (1.0 - 2.0 * k) * sqrt(max(0.0, 1.0 - q.y * q.y));
        float lit = smoothstep(edge - 0.06, edge + 0.06, q.x);
        float disc = 1.0 - smoothstep(0.96, 1.04, r);
        // Maria: a few soft dark patches, so it reads as the Moon and not a lamp.
        float maria = 0.18 * smoothstep(0.5, 0.0, length(p - vec2(-0.25, 0.3)))
                    + 0.14 * smoothstep(0.35, 0.0, length(p - vec2(0.2, 0.1)))
                    + 0.12 * smoothstep(0.3, 0.0, length(p - vec2(-0.1, -0.35)));
        vec3 surface = vec3(0.98, 0.96, 0.9) * (1.0 - maria);
        vec3 col = surface * mix(0.05, 1.0, lit) * uBright;
        float a = disc;
        // Glare around it, stronger the fuller it is.
        float glare = (1.0 - disc) * uIllum * 0.35 * exp(-(r - 1.0) * 2.2) * uBright;
        vec3 outc = toSrgb(col) * a + vec3(0.85, 0.88, 0.95) * glare;
        float outa = max(a, glare * 0.5);
        if (outa < 0.003) discard;
        gl_FragColor = vec4(outc, outa);
      }
    `,
  })
}

/**
 * The scheduler's targets: rings, not dots, because they are UI -- a size in
 * pixels, not in magnitudes -- and must be findable at any zoom.
 */
export function markerMaterial(s: Shared, accent: Color, dim: Color): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    ...premultipliedOver,
    uniforms: {
      ...sharedUniforms(s),
      uSelected: u(-1),
      uCurrent: u(-1),
      uAccent: u(accent),
      uDim: u(dim),
      uTime: u(0),
    },
    vertexShader: /* glsl */ `
      attribute float aIndex;
      attribute float aScheduled;
      varying float vIndex;
      varying float vScheduled;
      varying float vSize;
      uniform float uSelected;
      uniform float uCurrent;
      uniform float uPixelRatio;
      ${PROJECT}
      void main() {
        vIndex = aIndex;
        vScheduled = aScheduled;
        float emphasis = abs(aIndex - uCurrent) < 0.5 ? 1.5 : abs(aIndex - uSelected) < 0.5 ? 1.25 : 1.0;
        vSize = 24.0 * emphasis * uPixelRatio;
        gl_PointSize = vSize;
        gl_Position = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, vCull);
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying float vCull;
      varying float vIndex;
      varying float vScheduled;
      varying float vSize;
      uniform float uSelected;
      uniform float uCurrent;
      uniform vec3 uAccent;
      uniform vec3 uDim;
      uniform float uTime;
      void main() {
        if (vCull > 0.5) discard;
        vec2 p = gl_PointCoord - 0.5;
        float r = length(p) * 2.0;
        if (r > 1.0) discard;
        bool isCurrent = abs(vIndex - uCurrent) < 0.5;
        bool isChosen = abs(vIndex - uSelected) < 0.5;
        float px = 2.0 / vSize;
        float ring = smoothstep(0.66 - px, 0.66 + px, r) * (1.0 - smoothstep(0.82 - px, 0.82 + px, r));
        float core = isCurrent ? 1.0 - smoothstep(0.18, 0.18 + px * 2.0, r) : 0.0;
        // The target being observed right now breathes, slowly.
        float pulse = isCurrent ? 0.75 + 0.25 * sin(uTime * 2.4) : 1.0;
        vec3 col = (isCurrent || isChosen || vScheduled > 0.5) ? uAccent : uDim;
        float a = (ring + core) * pulse * ((isCurrent || isChosen) ? 1.0 : vScheduled > 0.5 ? 0.8 : 0.55);
        if (a < 0.01) discard;
        gl_FragColor = vec4(col * a, a);
      }
    `,
  })
}

// -------------------------------------------------------------------- lines

/** Thin reference lines: constellations, grids, the sensor frame, rings. */
export function lineMaterial(
  s: Shared,
  color: Color,
  opacity: number,
  opts: { contextual?: boolean } = {},
): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    ...premultipliedOver,
    uniforms: {
      ...sharedUniforms(s),
      uColor: u(color),
      uOpacity: u(opacity),
      uUseContext: u(opts.contextual ? 1 : 0),
    },
    vertexShader: /* glsl */ `
      ${PROJECT}
      void main() {
        gl_Position = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, vCull);
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying float vCull;
      uniform vec3 uColor;
      uniform float uOpacity;
      uniform float uUseContext;
      uniform float uContext;
      void main() {
        if (vCull > 0.001) discard;
        float a = uOpacity * (uUseContext > 0.5 ? uContext : 1.0);
        gl_FragColor = vec4(uColor * a, a);
      }
    `,
  })
}

/**
 * The observing route: every leg between consecutive stops of the plan, as a
 * thick line whose look says where the night has got to.
 *
 *   TRAVELLED  the telescope has already arrived at the far end. Muted, but
 *              kept: the route so far is half of "how is the night going".
 *   SLEWING    the telescope is on this leg right now. Filled up to where it
 *              is, the rest still dashed.
 *   NEXT       the telescope is at the near end; this is where it goes next.
 *              Brightest, with dashes flowing toward the next stop.
 *   LATER      everything after that. Plain accent.
 *
 * Each vertex carries its leg's three instants as night fractions -- when the
 * leg becomes "next", when the slew starts, when it ends -- so one cursor
 * uniform restyles the whole route and scrubbing never rebuilds geometry.
 * `uAlpha` fades a superseded route out when the plan changes.
 */
export function routeMaterial(
  s: Shared,
  colors: { later: Color; next: Color; travelled: Color },
): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    ...premultipliedOver,
    uniforms: {
      ...sharedUniforms(s),
      uCursorT: u(0),
      uTime: u(0),
      uAlpha: u(1),
      uLater: u(colors.later),
      uNext: u(colors.next),
      uTravelled: u(colors.travelled),
    },
    vertexShader: /* glsl */ `
      attribute vec3 aOther;
      attribute float aSide;
      attribute float aU;
      attribute vec3 aT;
      attribute float aLen;
      varying float vU;
      varying vec3 vT;
      varying float vLen;
      uniform float uCursorT;
      uniform vec2 uResolution;
      uniform float uPixelRatio;
      ${PROJECT}
      void main() {
        float c1; float c2;
        vec4 a = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, c1);
        vec4 b = stereoProject((modelViewMatrix * vec4(aOther, 1.0)).xyz, c2);
        vCull = max(c1, c2);
        vec2 sa = a.xy * uResolution * 0.5;
        vec2 sb = b.xy * uResolution * 0.5;
        vec2 dir = sb - sa;
        float len = length(dir);
        dir = len > 1e-4 ? dir / len : vec2(1.0, 0.0);
        vec2 n = vec2(-dir.y, dir.x);
        // The leg that matters now is drawn heavier than the rest.
        float live = step(aT.x, uCursorT) * (1.0 - step(aT.z, uCursorT));
        float travelled = step(aT.z, uCursorT);
        float width = mix(mix(2.4, 3.6, live), 1.8, travelled);
        vec2 off = n * aSide * width * uPixelRatio * 0.5;
        gl_Position = vec4(a.xy + off / (uResolution * 0.5), 0.0, 1.0);
        vU = aU;
        vT = aT;
        vLen = aLen;
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying float vCull;
      varying float vU;
      varying vec3 vT;
      varying float vLen;
      uniform float uCursorT;
      uniform float uTime;
      uniform float uAlpha;
      uniform vec3 uLater;
      uniform vec3 uNext;
      uniform vec3 uTravelled;
      void main() {
        if (vCull > 0.001) discard;
        float c = uCursorT;
        // Dashes every 2 degrees along the leg, flowing toward the far stop.
        float dash = step(fract(vU * vLen / 2.0 - uTime * 0.6), 0.55);
        vec3 col;
        float a;
        if (c >= vT.z) {
          col = uTravelled;
          a = 0.55;
        } else if (c >= vT.y) {
          float p = clamp((c - vT.y) / max(vT.z - vT.y, 1e-6), 0.0, 1.0);
          bool behind = vU <= p;
          col = uNext;
          a = behind ? 1.0 : mix(0.3, 0.95, dash);
        } else if (c >= vT.x) {
          col = uNext;
          a = mix(0.3, 1.0, dash);
        } else {
          col = uLater;
          a = 0.8;
        }
        a *= uAlpha;
        gl_FragColor = vec4(col * a, a);
      }
    `,
  })
}

/**
 * The catalogue: every deep-sky object that is not already a target, as a
 * small faint ring, so anything can be clicked and added to the night. Faint
 * on purpose -- there are nearly two thousand of them, and they are context,
 * not the plan -- and faded further by "focus on plan" like the other context
 * layers. The one that is picked is drawn in the accent, full strength.
 *
 * At a wide field only the Messier objects (`aBright`) show; the rest fade in
 * between 70 and 35 degrees of field, which is where their names appear too.
 * Hundreds of rings across a whole-sky view are clutter, not information.
 */
export function catalogMaterial(s: Shared, dim: Color, accent: Color): ShaderMaterial {
  return new ShaderMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
    ...premultipliedOver,
    uniforms: {
      ...sharedUniforms(s),
      uSelected: u(-1),
      uDim: u(dim),
      uAccent: u(accent),
      uFov: u(120),
    },
    vertexShader: /* glsl */ `
      attribute float aIndex;
      attribute float aBright;
      varying float vChosen;
      varying float vShow;
      varying float vSize;
      uniform float uSelected;
      uniform float uPixelRatio;
      uniform float uFov;
      ${PROJECT}
      void main() {
        vChosen = abs(aIndex - uSelected) < 0.5 ? 1.0 : 0.0;
        vShow = max(max(aBright, vChosen), smoothstep(70.0, 35.0, uFov));
        vSize = mix(10.0, 18.0, vChosen) * uPixelRatio;
        gl_PointSize = vSize;
        gl_Position = stereoProject((modelViewMatrix * vec4(position, 1.0)).xyz, vCull);
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      varying float vCull;
      varying float vChosen;
      varying float vShow;
      varying float vSize;
      uniform vec3 uDim;
      uniform vec3 uAccent;
      uniform float uContext;
      void main() {
        if (vCull > 0.5 || vShow < 0.01) discard;
        float r = length(gl_PointCoord - 0.5) * 2.0;
        if (r > 1.0) discard;
        float px = 2.0 / vSize;
        float ring = smoothstep(0.62 - px, 0.62 + px, r) * (1.0 - smoothstep(0.86 - px, 0.86 + px, r));
        float a = ring * mix(0.28 * uContext, 1.0, vChosen) * vShow;
        if (a < 0.01) discard;
        gl_FragColor = vec4(mix(uDim, uAccent, vChosen) * a, a);
      }
    `,
  })
}
