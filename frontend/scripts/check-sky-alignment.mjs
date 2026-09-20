/**
 * Prove the vendored background is in the frame we think it is, then record
 * that frame where the renderer reads it.
 *
 *     node scripts/check-sky-alignment.mjs
 *
 * A wrong all-sky background renders beautifully. Flip it in declination,
 * mirror it in right ascension, or hand it a galactic-frame image by mistake,
 * and you still get a gorgeous Milky Way arching over a gorgeous horizon --
 * just not this sky, from here, tonight. Nothing in the rendering path can
 * tell you, and neither can your eye.
 *
 * This file exists because the obvious check is not good enough. "Find the
 * brightest patch and confirm it is the galactic centre" PASSES on a mirrored
 * image, because the mirror RA -> 180 - RA has a fixed point at RA 270 and the
 * galactic centre sits at RA 266. Two different frames, one test, both green.
 *
 * So the real test is frame-agnostic: take the brightness-weighted moment
 * tensor of every pixel's unit vector, and the eigenvector with the smallest
 * eigenvalue is the pole of whatever plane the light is concentrated in. No
 * assumption goes in. Then ask whether that pole is the galactic pole.
 *
 * The answer for NASA's Deep Star Maps, which this run established rather than
 * assumed, is that they use the planetarium convention: right ascension ZERO
 * at the image centre, INCREASING TO THE LEFT, because the texture is meant to
 * be seen from inside the sphere.
 */

import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync, existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SKY = join(ROOT, 'public', 'sky')
const IMAGE = join(SKY, 'milkyway.webp')
const STARS = join(SKY, 'stars.bin')
const MANIFEST = join(SKY, 'manifest.json')

const D2R = Math.PI / 180

/** IAU 1958 galactic pole and zero point, ICRS. */
const NGP_RA = 192.85948
const NGP_DEC = 27.12825
const L_NCP = 122.93192
const GC_RA = 266.405
const GC_DEC = -28.936

/**
 * The convention under test. `u` runs 0 at the left edge to 1 at the right.
 *
 * If a future release of the source image changes this, every assertion below
 * fails at once and loudly -- which is the entire point of writing the mapping
 * down as code rather than as a comment in a shader.
 */
const raAt = (u) => (((180 - 360 * u) % 360) + 360) % 360
const decAt = (v) => 90 - 180 * v

function galToEq(l, b) {
  const [lr, br, pr, pd, ln] = [l, b, NGP_RA, NGP_DEC, L_NCP].map((x) => x * D2R)
  const dec = Math.asin(
    Math.sin(br) * Math.sin(pd) + Math.cos(br) * Math.cos(pd) * Math.cos(ln - lr),
  )
  const y = Math.cos(br) * Math.sin(ln - lr)
  const x =
    Math.sin(br) * Math.cos(pd) - Math.cos(br) * Math.sin(pd) * Math.cos(ln - lr)
  let ra = (pr + Math.atan2(y, x)) / D2R
  return [((ra % 360) + 360) % 360, dec / D2R]
}

const sep = (r1, d1, r2, d2) => {
  const [a1, b1, a2, b2] = [r1, d1, r2, d2].map((v) => v * D2R)
  return (
    Math.acos(
      Math.max(
        -1,
        Math.min(1, Math.sin(b1) * Math.sin(b2) + Math.cos(b1) * Math.cos(b2) * Math.cos(a1 - a2)),
      ),
    ) / D2R
  )
}

function decodePpm(path) {
  const buf = readFileSync(path)
  let pos = 0
  const token = () => {
    while ([0x20, 0x0a, 0x0d, 0x09].includes(buf[pos])) pos++
    if (buf[pos] === 0x23) {
      while (buf[pos] !== 0x0a) pos++
      return token()
    }
    const start = pos
    while (pos < buf.length && buf[pos] > 0x20) pos++
    return buf.slice(start, pos).toString('ascii')
  }
  if (token() !== 'P6') throw new Error('expected a P6 PPM from dwebp')
  const w = Number(token())
  const h = Number(token())
  if (Number(token()) !== 255) throw new Error('unsupported PPM maxval')
  pos++
  return { w, h, data: buf.slice(pos) }
}

const lum = (d, o) => 0.2126 * d[o] + 0.7152 * d[o + 1] + 0.0722 * d[o + 2]

/** Jacobi eigen-decomposition of a symmetric 3x3. */
function jacobi(input) {
  const A = input.map((r) => r.slice())
  const V = [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ]
  for (let sweep = 0; sweep < 64; sweep++) {
    let off = 0
    for (let p = 0; p < 3; p++) for (let q = p + 1; q < 3; q++) off += A[p][q] ** 2
    if (off < 1e-20) break
    for (let p = 0; p < 3; p++) {
      for (let q = p + 1; q < 3; q++) {
        if (Math.abs(A[p][q]) < 1e-18) continue
        const th = (A[q][q] - A[p][p]) / (2 * A[p][q])
        const t = (th >= 0 ? 1 : -1) / (Math.abs(th) + Math.sqrt(th * th + 1))
        const c = 1 / Math.sqrt(t * t + 1)
        const s = t * c
        for (let k = 0; k < 3; k++) {
          const [akp, akq] = [A[k][p], A[k][q]]
          A[k][p] = c * akp - s * akq
          A[k][q] = s * akp + c * akq
        }
        for (let k = 0; k < 3; k++) {
          const [apk, aqk] = [A[p][k], A[q][k]]
          A[p][k] = c * apk - s * aqk
          A[q][k] = s * apk + c * aqk
        }
        for (let k = 0; k < 3; k++) {
          const [vkp, vkq] = [V[k][p], V[k][q]]
          V[k][p] = c * vkp - s * vkq
          V[k][q] = s * vkp + c * vkq
        }
      }
    }
  }
  return { vals: [A[0][0], A[1][1], A[2][2]], vecs: V }
}

function main() {
  for (const f of [IMAGE, STARS]) {
    if (!existsSync(f)) throw new Error(`${f} is missing -- run build-sky-data.mjs first`)
  }
  const tmp = mkdtempSync(join(tmpdir(), 'skycheck-'))
  const ppm = join(tmp, 'mw.ppm')
  try {
    execFileSync('dwebp', [IMAGE, '-ppm', '-quiet', '-o', ppm])
  } catch (err) {
    rmSync(tmp, { recursive: true, force: true })
    throw new Error(`dwebp is required (brew install webp): ${err.message}`)
  }
  const { w, h, data } = decodePpm(ppm)
  rmSync(tmp, { recursive: true, force: true })
  if (w !== 2 * h) throw new Error(`${w}x${h} is not 2:1, so it is not equirectangular`)
  console.log(`Decoded ${w}x${h}\n`)

  const problems = []

  // -- 1. frame-agnostic: where IS the plane of concentration? --------------
  const M = [
    [0, 0, 0],
    [0, 0, 0],
    [0, 0, 0],
  ]
  let total = 0
  let best = { v: -1, ra: 0, dec: 0 }
  const STEP = 4
  for (let y = 0; y < h; y += STEP) {
    const dec = decAt((y + 0.5) / h) * D2R
    const solidAngle = Math.cos(dec) // equirectangular over-samples the poles
    for (let x = 0; x < w; x += STEP) {
      const raw = lum(data, (y * w + x) * 3)
      const v = raw * solidAngle
      if (v <= 0) continue
      const ra = raAt((x + 0.5) / w) * D2R
      const u = [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)]
      for (let a = 0; a < 3; a++) for (let b = 0; b < 3; b++) M[a][b] += v * u[a] * u[b]
      total += v
    }
  }
  for (let a = 0; a < 3; a++) for (let b = 0; b < 3; b++) M[a][b] /= total

  const { vals, vecs } = jacobi(M)
  const k = [0, 1, 2].sort((a, b) => vals[a] - vals[b])[0]
  const pole = [vecs[0][k], vecs[1][k], vecs[2][k]]
  let poleRa = Math.atan2(pole[1], pole[0]) / D2R
  if (poleRa < 0) poleRa += 360
  const poleDec = Math.asin(Math.max(-1, Math.min(1, pole[2]))) / D2R
  const toNgp = Math.min(
    sep(poleRa, poleDec, NGP_RA, NGP_DEC),
    sep((poleRa + 180) % 360, -poleDec, NGP_RA, NGP_DEC),
  )

  console.log('Plane of concentration, found without assuming a frame:')
  console.log(`  pole            RA ${poleRa.toFixed(2)}  Dec ${poleDec.toFixed(2)}`)
  console.log(`  galactic pole   RA ${NGP_RA}  Dec ${NGP_DEC}`)
  console.log(`  separation      ${toNgp.toFixed(1)} deg`)
  if (toNgp > 5) {
    problems.push(
      `the light is concentrated about a pole ${toNgp.toFixed(1)} deg from the ` +
        'galactic pole. The declared mapping is wrong, or this is not an ' +
        'equatorial-frame image of the Milky Way.',
    )
  }

  // -- 2. does the brightness actually follow the plane? -------------------
  const NX = 360
  const NY = 180
  const cell = new Float64Array(NX * NY)
  const count = new Float64Array(NX * NY)
  for (let y = 0; y < h; y += 2) {
    const j = Math.min(NY - 1, Math.floor((y / h) * NY))
    for (let x = 0; x < w; x += 2) {
      const i = Math.min(NX - 1, Math.floor((x / w) * NX))
      cell[j * NX + i] += lum(data, (y * w + x) * 3)
      count[j * NX + i]++
    }
  }
  const onPlane = new Set()
  for (let l = 0; l < 360; l += 0.1) {
    const [ra, dec] = galToEq(l, 0)
    const u = ((((180 - ra) % 360) + 360) % 360) / 360 // invert raAt
    const i = Math.min(NX - 1, Math.floor(u * NX))
    const j = Math.min(NY - 1, Math.floor(((90 - dec) / 180) * NY))
    onPlane.add(j * NX + i)
  }
  let on = 0
  let onN = 0
  let off = 0
  let offN = 0
  for (let idx = 0; idx < cell.length; idx++) {
    if (!count[idx]) continue
    const v = cell[idx] / count[idx]
    if (onPlane.has(idx)) {
      on += v
      onN++
    } else {
      off += v
      offN++
    }
  }
  const ratio = on / onN / (off / offN)
  console.log(`\n  mean brightness on the galactic plane : ${(on / onN).toFixed(1)}`)
  console.log(`  mean brightness off it                : ${(off / offN).toFixed(1)}`)
  console.log(`  ratio                                 : ${ratio.toFixed(2)}x`)
  if (ratio < 2.5) {
    problems.push(
      `the galactic plane is only ${ratio.toFixed(2)}x brighter than the rest of ` +
        'the sky; under the correct mapping it is over 3x.',
    )
  }

  // -- 3. the galactic bulge, kept as a weak corroboration ---------------
  //
  // Measured on a COARSE grid, and that is not a detail. The brightest single
  // pixel in this image is not the galactic centre at all -- it is the Small
  // Magellanic Cloud, at RA 13, Dec -73, which is compact, genuinely brilliant,
  // and 45 degrees off the plane. Asking for the brightest extended region
  // instead finds the bulge, which is what we mean.
  const BX = 30
  const BY = 15
  const blocks = new Float64Array(BX * BY)
  const bn = new Float64Array(BX * BY)
  for (let y = 0; y < h; y += 2) {
    const j = Math.min(BY - 1, Math.floor((y / h) * BY))
    for (let x = 0; x < w; x += 2) {
      const i = Math.min(BX - 1, Math.floor((x / w) * BX))
      blocks[j * BX + i] += lum(data, (y * w + x) * 3)
      bn[j * BX + i]++
    }
  }
  for (let idx = 0; idx < blocks.length; idx++) {
    const v = blocks[idx] / (bn[idx] || 1)
    if (v > best.v) {
      best = {
        v,
        ra: raAt(((idx % BX) + 0.5) / BX),
        dec: decAt((Math.floor(idx / BX) + 0.5) / BY),
      }
    }
  }
  const gcSep = sep(best.ra, best.dec, GC_RA, GC_DEC)
  console.log(`\n  brightest region : RA ${best.ra.toFixed(1)}  Dec ${best.dec.toFixed(1)}`)
  console.log(`  galactic centre  : RA ${GC_RA}  Dec ${GC_DEC}  (${gcSep.toFixed(1)} deg away)`)
  console.log(
    '  NOTE: corroboration only. On its own this check also passes on a\n' +
      '        MIRRORED image, because RA 270 is a fixed point of that mirror\n' +
      '        and the galactic centre happens to sit almost exactly there.\n' +
      '        Test 1 is the one that actually constrains the frame.',
  )
  if (gcSep > 15) {
    problems.push(`the brightest region is ${gcSep.toFixed(1)} deg from the galactic centre`)
  }

  if (problems.length) {
    console.error('\nMISALIGNED:')
    for (const p of problems) console.error('  - ' + p)
    process.exitCode = 1
    return
  }

  const stars = readFileSync(STARS)
  const manifest = {
    _comment:
      'Written by scripts/check-sky-alignment.mjs. The projection block is ' +
      'VERIFIED, not assumed -- see that file. Do not hand-edit.',
    stars: {
      file: 'stars.bin',
      count: stars.readUInt32LE(8),
      stride: stars.readUInt32LE(12),
      headerBytes: 16,
      fields: ['x', 'y', 'z', 'mag', 'bv'],
      frame: 'icrs',
      credit: 'Hipparcos, via d3-celestial (BSD-3-Clause). See LICENSES.md.',
    },
    background: {
      file: 'milkyway.webp',
      width: w,
      height: h,
      projection: 'equirectangular',
      frame: 'icrs',
      raAtLeftEdgeDeg: 180,
      raIncreasesTo: 'left',
      decAtTopDeg: 90,
      credit: 'NASA/GSFC Scientific Visualization Studio; Gaia DR2: ESA/Gaia/DPAC',
      verified: {
        planePoleSeparationDeg: Number(toNgp.toFixed(2)),
        planeBrightnessRatio: Number(ratio.toFixed(2)),
        galacticCentreSeparationDeg: Number(gcSep.toFixed(2)),
      },
    },
  }
  writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2) + '\n')

  console.log('\nOK: equirectangular, ICRS, RA 0 at the image CENTRE increasing LEFT.')
  console.log(`    Recorded in public/sky/manifest.json for the renderer to read.`)
}

try {
  main()
} catch (err) {
  console.error('FAILED:', err.message)
  process.exitCode = 1
}
