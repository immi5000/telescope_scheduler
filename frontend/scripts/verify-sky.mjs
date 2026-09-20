/**
 * End-to-end check of the sky the BROWSER will draw.
 *
 *     node scripts/verify-sky.mjs <sessionId> [apiBase]
 *
 * The unit tests prove the backend is self-consistent and the alignment script
 * proves the background is in the right frame. This proves the numbers that
 * actually reach the renderer survive rounding, JSON and the wire -- and it
 * does the arithmetic the way the shader will, not the way Python did.
 *
 * Three checks:
 *   1. the published rotation reproduces the published ALTITUDE and AZIMUTH;
 *   2. named bright stars in `stars.bin` land at their catalogue positions;
 *   3. those stars, carried through the same rotation, are above the horizon
 *      when they should be -- which is the one check that ties the vendored
 *      catalogue and the backend's frame together.
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const [sessionId, base = 'http://127.0.0.1:8000'] = process.argv.slice(2)
if (!sessionId) {
  console.error('usage: node scripts/verify-sky.mjs <sessionId> [apiBase]')
  process.exit(2)
}

const D2R = Math.PI / 180
const R2D = 180 / Math.PI

/** Named stars, ICRS J2000, from SIMBAD. Independent of anything in this repo. */
const KNOWN = [
  { name: 'Sirius', hip: 32349, ra: 101.28716, dec: -16.71612, mag: -1.46 },
  { name: 'Vega', hip: 91262, ra: 279.23473, dec: 38.78369, mag: 0.03 },
  { name: 'Arcturus', hip: 69673, ra: 213.91530, dec: 19.18241, mag: -0.05 },
  { name: 'Polaris', hip: 11767, ra: 37.95456, dec: 89.26411, mag: 1.98 },
  { name: 'Betelgeuse', hip: 27989, ra: 88.79294, dec: 7.40706, mag: 0.42 },
  { name: 'Altair', hip: 97649, ra: 297.69582, dec: 8.86832, mag: 0.76 },
]

const icrs = (raDeg, decDeg) => {
  const ra = raDeg * D2R
  const dec = decDeg * D2R
  return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)]
}

/** Rotate v by the CONJUGATE of q -- i.e. ICRS into the local (E, N, U) basis. */
function unrotate(q, v) {
  const [x, y, z, w] = q
  const m = [
    [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
    [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
    [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
  ]
  return [0, 1, 2].map((i) => m[0][i] * v[0] + m[1][i] * v[1] + m[2][i] * v[2])
}

const sep = (r1, d1, r2, d2) => {
  const [a1, b1, a2, b2] = [r1, d1, r2, d2].map((x) => x * D2R)
  return (
    Math.acos(
      Math.max(-1, Math.min(1, Math.sin(b1) * Math.sin(b2) + Math.cos(b1) * Math.cos(b2) * Math.cos(a1 - a2))),
    ) * R2D
  )
}

function loadConstellations() {
  const buf = readFileSync(join(ROOT, 'public', 'sky', 'constellations.bin'))
  if (buf.readUInt32LE(0) !== 0x54434e4c) throw new Error('constellations.bin: bad magic')
  const n = buf.readUInt32LE(8)
  const pts = []
  for (let i = 0; i < n; i++) {
    const o = 16 + i * 12
    const x = buf.readFloatLE(o)
    const y = buf.readFloatLE(o + 4)
    const z = buf.readFloatLE(o + 8)
    pts.push({
      ra: ((Math.atan2(y, x) * R2D) % 360 + 360) % 360,
      dec: Math.asin(Math.max(-1, Math.min(1, z))) * R2D,
    })
  }
  return pts
}

function loadStars() {
  const buf = readFileSync(join(ROOT, 'public', 'sky', 'stars.bin'))
  if (buf.readUInt32LE(0) !== 0x54534b59) throw new Error('stars.bin: bad magic')
  const count = buf.readUInt32LE(8)
  const stride = buf.readUInt32LE(12)
  const out = []
  for (let i = 0; i < count; i++) {
    const o = 16 + i * stride
    const x = buf.readFloatLE(o)
    const y = buf.readFloatLE(o + 4)
    const z = buf.readFloatLE(o + 8)
    out.push({
      x, y, z,
      mag: buf.readFloatLE(o + 12),
      bv: buf.readFloatLE(o + 16),
      ra: ((Math.atan2(y, x) * R2D) % 360 + 360) % 360,
      dec: Math.asin(Math.max(-1, Math.min(1, z))) * R2D,
    })
  }
  return out
}

const fail = []
const note = (ok, line) => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'}  ${line}`)
  if (!ok) fail.push(line)
}

const geo = await (await fetch(`${base}/api/sessions/${sessionId}/geometry`)).json()
console.log(`\nSession ${sessionId} — ${geo.nSlots} slots, ${geo.rows.length} targets\n`)

// -- 1. the frame -----------------------------------------------------------
console.log('1. Published rotation vs published alt/az')
let worstAlt = 0
let worstAz = 0
for (const row of geo.rows) {
  const v = icrs(row.raDeg, row.decDeg)
  for (let s = 0; s < geo.nSlots; s++) {
    const l = unrotate(geo.frameQuat[s], v)
    const alt = Math.asin(Math.max(-1, Math.min(1, l[2]))) * R2D
    const az = ((Math.atan2(l[0], l[1]) * R2D) % 360 + 360) % 360
    worstAlt = Math.max(worstAlt, Math.abs(alt - row.altitudeDeg[s]))
    if (row.altitudeDeg[s] < 85) {
      const d = Math.abs(((az - row.azimuthDeg[s] + 180) % 360) - 180)
      worstAz = Math.max(worstAz, d * Math.cos(row.altitudeDeg[s] * D2R))
    }
  }
}
note(worstAlt < 0.02, `altitude residual ${(worstAlt * 3600).toFixed(1)}″ (limit 72″)`)
note(worstAz < 0.02, `azimuth residual  ${(worstAz * 3600).toFixed(1)}″ (limit 72″)`)

// -- 2. the catalogue -------------------------------------------------------
console.log('\n2. Named stars in stars.bin vs SIMBAD')
const stars = loadStars()
for (const k of KNOWN) {
  let best = null
  let bestSep = 1e9
  for (const s of stars) {
    const d = sep(s.ra, s.dec, k.ra, k.dec)
    if (d < bestSep) {
      bestSep = d
      best = s
    }
  }
  const magOk = Math.abs(best.mag - k.mag) < 0.25
  note(
    bestSep < 0.05 && magOk,
    `${k.name.padEnd(11)} ${(bestSep * 3600).toFixed(1).padStart(6)}″ away, ` +
      `mag ${best.mag.toFixed(2)} vs ${k.mag.toFixed(2)}, B−V ${best.bv.toFixed(2)}`,
  )
}

// -- 3. the constellation figures ------------------------------------------
//
// Asterisms anyone can check by eye against a star chart. If the longitude
// wrap were mishandled, or the lines came from a different epoch, these
// segments would not land on these stars.
console.log('\n3. Constellation lines connect the right stars')
const lines = loadConstellations()
const ASTERISMS = [
  ['Orion’s Belt: Alnitak–Alnilam', 85.1897, -1.9426, 84.0534, -1.2019],
  ['Orion’s Belt: Alnilam–Mintaka', 84.0534, -1.2019, 83.0016, -0.2991],
  ['Big Dipper: Dubhe–Merak', 165.9319, 61.7511, 165.4603, 56.3824],
  ['Big Dipper: Alioth–Mizar', 193.5073, 55.9598, 200.9814, 54.9254],
  ['Cassiopeia: Schedar–Caph', 10.1268, 56.5373, 2.2945, 59.1498],
]
for (const [label, ra1, d1, ra2, d2] of ASTERISMS) {
  let best = 99
  for (let i = 0; i < lines.length; i += 2) {
    const a = lines[i]
    const b = lines[i + 1]
    best = Math.min(
      best,
      Math.max(sep(a.ra, a.dec, ra1, d1), sep(b.ra, b.dec, ra2, d2)),
      Math.max(sep(a.ra, a.dec, ra2, d2), sep(b.ra, b.dec, ra1, d1)),
    )
  }
  note(best < 0.6, `${label.padEnd(30)} nearest segment ${(best * 60).toFixed(1)}′ off`)
}

// -- 4. the two together ----------------------------------------------------
console.log('\n4. Catalogue stars through the published frame')
const mid = Math.floor(geo.nSlots / 2)
for (const k of KNOWN) {
  const l = unrotate(geo.frameQuat[mid], icrs(k.ra, k.dec))
  const alt = Math.asin(Math.max(-1, Math.min(1, l[2]))) * R2D
  const az = ((Math.atan2(l[0], l[1]) * R2D) % 360 + 360) % 360
  console.log(
    `        ${k.name.padEnd(11)} alt ${alt.toFixed(1).padStart(6)}°  az ${az.toFixed(1).padStart(6)}°` +
      `  ${alt > 0 ? 'up' : 'below the horizon'}`,
  )
}
// Polaris is the assertion: from any northern site its altitude equals the
// latitude, all night, to within its own 0.74° offset from the true pole.
// Nothing else ties the catalogue, the frame and the site together in one number.
const pol = unrotate(geo.frameQuat[mid], icrs(KNOWN[3].ra, KNOWN[3].dec))
const polAlt = Math.asin(Math.max(-1, Math.min(1, pol[2]))) * R2D
const sess = await (await fetch(`${base}/api/sessions/${sessionId}`)).json()
const lat = sess.site.latitudeDeg
note(
  Math.abs(polAlt - lat) < 0.8,
  `Polaris altitude ${polAlt.toFixed(2)}° vs site latitude ${lat.toFixed(2)}° ` +
    `(differ by ${Math.abs(polAlt - lat).toFixed(2)}°, limit 0.8°)`,
)

console.log(fail.length ? `\n${fail.length} CHECK(S) FAILED\n` : '\nAll checks passed.\n')
process.exitCode = fail.length ? 1 : 0
