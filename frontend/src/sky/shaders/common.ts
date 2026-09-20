/**
 * GLSL shared by every layer of the first-person sky.
 *
 * Colour convention, stated once: the framebuffer holds DISPLAY sRGB, and
 * every material writes sRGB directly. Anything that models light (the
 * atmosphere, the Milky Way, the ground) does its arithmetic in linear and
 * encodes at the end with `toSrgb`; points and lines are blended additively
 * in display space, which is how a star looks brighter by getting bigger
 * rather than by clipping to white.
 */

export const COLOR_FN = /* glsl */ `
  vec3 toSrgb(vec3 c) {
    c = max(c, 0.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
  }
`

/** Equirectangular lookup for the all-sky background, per the verified manifest. */
export const EQUIRECT_UNIFORMS = /* glsl */ `
  uniform sampler2D uMap;
  uniform float uRaAtLeft;
  uniform float uRaDir;
`

/**
 * Sample the background with explicit gradients.
 *
 * `atan` jumps by a full turn where right ascension wraps, and the automatic
 * derivatives there are enormous -- the GPU picks the smallest mip and draws a
 * one-pixel seam from pole to pole. Wrapping the u-derivative into [-0.5, 0.5]
 * removes it.
 */
export const EQUIRECT_FN = /* glsl */ `
  vec2 equirectUv(vec3 dir) {
    vec3 d = normalize(dir);
    float ra = mod(degrees(atan(d.y, d.x)), 360.0);
    float dec = degrees(asin(clamp(d.z, -1.0, 1.0)));
    // Inverse of  ra = uRaAtLeft + uRaDir * 360 * u
    float u = fract((ra - uRaAtLeft) / (360.0 * uRaDir));
    // v = 0 is the FIRST row of the image; the texture is uploaded with
    // flipY = false precisely so that holds.
    float v = (90.0 - dec) / 180.0;
    return vec2(u, v);
  }
  vec3 sampleSky(vec3 dir) {
    vec2 uv = equirectUv(dir);
    vec2 dx = dFdx(uv);
    vec2 dy = dFdy(uv);
    dx.x -= floor(dx.x + 0.5);
    dy.x -= floor(dy.x + 0.5);
    return textureGrad(uMap, uv, dx, dy).rgb;
  }
`

/** Every point and line vertex shader: declare the cull flag and project. */
export const VERTEX_HEAD = /* glsl */ `
  varying float vCull;
`
