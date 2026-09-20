/**
 * Vendor the real sky into `public/sky/`. Run BY HAND, never from CI.
 *
 *     node scripts/build-sky-data.mjs               # everything
 *     node scripts/build-sky-data.mjs --stars-only  # catalogue + names, keep the background
 *
 * A build that reaches the network is a build that fails on a plane, and this
 * data changes approximately never -- the star catalogue is Hipparcos and the
 * background is a 2020 NASA release. So the outputs are committed and this
 * script exists to document and reproduce them, not to run on every install.
 *
 * Two sources, both with checked provenance, because "use packages that
 * actually create the real sky, not an imitation" rules out anything whose
 * origin cannot be traced:
 *
 *   stars    ofrohn/d3-celestial `stars.8.json` -- 41,411 real Hipparcos stars
 *            to magnitude 8.0 with Johnson B-V, and `starnames.json` for the
 *            proper names and Bayer/Flamsteed designations. BSD-3-Clause. We
 *            take the DATA and not the library, which is a 2D canvas renderer
 *            last published in 2022.
 *
 *            Magnitude 8, not 6: the sky view is first-person and zooms, and
 *            stars are drawn to a limit that deepens as the field narrows. At
 *            a 20-degree field the limit is past 7, and a mag-6 catalogue runs
 *            out exactly when you look closely.
 *
 *   milkyway NASA/GSFC Scientific Visualization Studio "Deep Star Maps 2020",
 *            via Wikimedia Commons. svs.gsfc.nasa.gov itself sends
 *            `Access-Control-Allow-Origin: https://tempo.multiverse.music` --
 *            one unrelated origin -- so a runtime fetch is impossible and
 *            vendoring is the only option, not merely the faster one.
 *
 *            We take the MILKYWAY layer, not the STARMAP layer. The starmap
 *            has Gaia stars already rendered into it, which would double every
 *            star we draw ourselves from the catalogue above, at slightly the
 *            wrong positions and with none of the colour. The milkyway layer
 *            is the diffuse light only, which is exactly the part we cannot
 *            compute.
 */

import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync, rmSync, statSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(ROOT, 'public', 'sky')
const TMP = join(ROOT, 'node_modules', '.cache', 'sky-build')

const D3C = 'https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/'
const STARS_URL = D3C + 'stars.8.json'
const STARNAMES_URL = D3C + 'starnames.json'
const LINES_URL = D3C + 'constellations.lines.json'
const NAMES_URL = D3C + 'constellations.json'

/**
 * Commons renders any width from the 32768px master, so 626 MB stays on their
 * disk. The thumbnail URL is ASKED FOR rather than constructed: it embeds a
 * hash-derived directory and a title with an en-dash in it, and hand-building
 * that string is how you get an HTTP 400 that looks like the file is gone.
 */
const MILKYWAY_TITLE = 'File:Deep Star Maps 2020 – Milkyway 2020 64k.jpg'
const COMMONS_API = 'https://commons.wikimedia.org/w/api.php'

async function commonsThumb(title, width) {
  const url =
    `${COMMONS_API}?action=query&format=json&prop=imageinfo` +
    `&iiprop=url|size|extmetadata&iiurlwidth=${width}&titles=${encodeURIComponent(title)}`
  const res = await fetch(url, {
    headers: { 'user-agent': 'traveling-telescope build-sky-data (local, one-off)' },
  })
  if (!res.ok) throw new Error(`Commons API: HTTP ${res.status}`)
  const pages = Object.values((await res.json()).query.pages)
  const info = pages[0]?.imageinfo?.[0]
  if (!info?.thumburl) throw new Error(`Commons has no thumbnail for ${title}`)
  if (info.width !== 2 * info.height) {
    throw new Error(
      `${title} is ${info.width}x${info.height}, not 2:1. An all-sky background ` +
        'must be equirectangular or it cannot be mapped onto a sphere.',
    )
  }
  const license = info.extmetadata?.LicenseShortName?.value ?? '(none stated)'
  console.log(`  ${title}\n    ${info.width}x${info.height} master, licence: ${license}`)
  return info.thumburl
}

/**
 * 2048x1024 is the smallest size at which the dust lanes through Cygnus still
 * read as structure rather than as noise, and it is one mip level on any GPU
 * made this decade. 4096 doubles the download for detail that a sphere 800px
 * across cannot show.
 */
const MILKYWAY_WIDTH = 2048

const STAR_STRIDE = 20 // x,y,z,mag,bv as float32
const MAGIC = 0x54534b59 // 'TSKY'
const LINE_MAGIC = 0x54434e4c // 'TCNL'
const VERSION = 1

/**
 * Right ascension from a GeoJSON longitude.
 *
 * The catalogue is GeoJSON, so longitude runs -180..+180 while right ascension
 * runs 0..360. Skip this and exactly half the sky mirrors about the vernal
 * equinox -- which still looks like a sky, just not this one.
 */
const raFromLon = (lon) => (lon < 0 ? lon + 360 : lon)

const unit = (lon, lat) => {
  const ra = (raFromLon(lon) * Math.PI) / 180
  const dec = (lat * Math.PI) / 180
  return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)]
}

/**
 * B-V for the two stars that lack it. Roughly solar, chosen over 0 (which is
 * A0V, a blue-white star) and over NaN, which propagates through the colour
 * ramp and renders an INVISIBLE star -- a missing-data bug that looks exactly
 * like a correct catalogue with one fewer entry.
 */
const DEFAULT_BV = 0.65

async function get(url, what) {
  process.stdout.write(`  fetching ${what} ... `)
  const res = await fetch(url, {
    headers: { 'user-agent': 'traveling-telescope build-sky-data (local, one-off)' },
  })
  if (!res.ok) throw new Error(`${what}: HTTP ${res.status} ${res.statusText}`)
  const buf = Buffer.from(await res.arrayBuffer())
  console.log(`${(buf.length / 1e6).toFixed(1)} MB`)
  return buf
}

function buildStars(json) {
  const features = JSON.parse(json).features
  const bytes = Buffer.alloc(16 + features.length * STAR_STRIDE)
  bytes.writeUInt32LE(MAGIC, 0)
  bytes.writeUInt32LE(VERSION, 4)
  bytes.writeUInt32LE(features.length, 8)
  bytes.writeUInt32LE(STAR_STRIDE, 12)

  let missingBv = 0
  let negativeLon = 0
  features.forEach((f, i) => {
    const [lon, lat] = f.geometry.coordinates
    // GeoJSON longitude is -180..+180. Right ascension is 0..360. Skip this
    // and exactly half the sky mirrors about the vernal equinox -- which still
    // looks like a sky, just not this one.
    if (lon < 0) negativeLon++
    const ra = (raFromLon(lon) * Math.PI) / 180
    const dec = (lat * Math.PI) / 180

    // `bv` arrives as a STRING, and is absent for 2 of 5,044.
    const raw = f.properties.bv
    const bv = raw === undefined || raw === null || raw === '' ? NaN : Number(raw)
    const ok = Number.isFinite(bv)
    if (!ok) missingBv++

    const o = 16 + i * STAR_STRIDE
    bytes.writeFloatLE(Math.cos(dec) * Math.cos(ra), o)
    bytes.writeFloatLE(Math.cos(dec) * Math.sin(ra), o + 4)
    bytes.writeFloatLE(Math.sin(dec), o + 8)
    bytes.writeFloatLE(f.properties.mag, o + 12)
    bytes.writeFloatLE(ok ? bv : DEFAULT_BV, o + 16)
  })

  if (negativeLon === 0) {
    throw new Error(
      'no negative longitudes in the catalogue -- the upstream format changed ' +
        'from GeoJSON to plain RA, and the +360 wrap above is now corrupting it',
    )
  }
  console.log(
    `  ${features.length} stars, ${negativeLon} wrapped from negative longitude, ` +
      `${missingBv} missing B-V defaulted to ${DEFAULT_BV}`,
  )
  return bytes
}

/**
 * Constellation lines, flattened to SEGMENT PAIRS.
 *
 * Stored as independent pairs rather than as polylines with a length table so
 * the renderer can draw all 89 constellations with one `LineSegments` and no
 * index bookkeeping at all. The duplication costs a few kilobytes and removes
 * a whole category of off-by-one.
 */
function buildConstellationLines(json) {
  const features = JSON.parse(json).features
  const pts = []
  let polylines = 0
  for (const f of features) {
    const geom = f.geometry
    const lines = geom.type === 'MultiLineString' ? geom.coordinates : [geom.coordinates]
    for (const line of lines) {
      polylines++
      for (let i = 0; i + 1 < line.length; i++) {
        // A segment whose endpoints straddle the 0h/24h wrap would be drawn
        // the long way round the sphere -- a chord clean across the middle of
        // the globe. Splitting at the seam is not needed because we work in
        // unit VECTORS, where the wrap does not exist; this is precisely why
        // the positions are baked here rather than at runtime from angles.
        pts.push(...unit(line[i][0], line[i][1]), ...unit(line[i + 1][0], line[i + 1][1]))
      }
    }
  }
  const count = pts.length / 3
  const bytes = Buffer.alloc(16 + count * 12)
  bytes.writeUInt32LE(LINE_MAGIC, 0)
  bytes.writeUInt32LE(VERSION, 4)
  bytes.writeUInt32LE(count, 8)
  bytes.writeUInt32LE(12, 12)
  pts.forEach((v, i) => bytes.writeFloatLE(v, 16 + i * 4))
  console.log(`  ${features.length} constellations, ${polylines} polylines, ${count / 2} segments`)
  return bytes
}

/**
 * Names and designations, keyed by INDEX into stars.bin.
 *
 * Indexed rather than keyed by Hipparcos number so the renderer can label a
 * star without a lookup table: entry `i` is the star at `stars.bin[i]`. Only
 * stars brighter than 6.5 are named -- nobody looks up an eighth-magnitude
 * star by its Flamsteed number, and the file stays small.
 */
function buildStarNames(starsJson, namesJson) {
  const features = JSON.parse(starsJson).features
  const names = JSON.parse(namesJson)
  const out = []
  features.forEach((f, i) => {
    if (f.properties.mag > 6.5) return
    const n = names[String(f.id)]
    if (!n) return
    const desig = n.bayer || n.flam ? `${n.bayer || n.flam} ${n.c}` : ''
    if (!n.name && !desig) return
    out.push([i, n.name || '', desig])
  })
  console.log(`  ${out.length} named or designated stars`)
  return out
}

/** Constellation names and their label anchors. */
function buildConstellationNames(json) {
  const out = JSON.parse(json).features.map((f) => {
    const [lon, lat] = f.geometry.coordinates
    return {
      id: f.id ?? f.properties.desig,
      name: f.properties.name ?? f.properties.en,
      abbr: f.properties.desig,
      raDeg: Number(raFromLon(lon).toFixed(4)),
      decDeg: Number(lat.toFixed(4)),
    }
  })
  console.log(`  ${out.length} constellation labels`)
  return out
}

function toWebp(jpeg) {
  mkdirSync(TMP, { recursive: true })
  const src = join(TMP, 'milkyway.jpg')
  const dst = join(TMP, 'milkyway.webp')
  writeFileSync(src, jpeg)
  try {
    // -q 82 is where banding starts to show in the dust lanes on a dark
    // display; -m 6 is the slow encoder, which is free here because this runs
    // once by hand.
    execFileSync('cwebp', ['-q', '82', '-m', '6', '-quiet', src, '-o', dst])
  } catch (err) {
    throw new Error(
      'cwebp is required to build the background (brew install webp). ' +
        `Underlying error: ${err.message}`,
    )
  }
  const out = readFileSync(dst)
  rmSync(TMP, { recursive: true, force: true })
  return out
}

const LICENSES = `# Vendored sky data

Everything in this directory was fetched by \`scripts/build-sky-data.mjs\` and is
redistributed under the terms below. Both notices must travel with the files.

## stars.bin, starnames.json, constellations.bin, constellations.json

41,411 stars to visual magnitude 8.0 from the Hipparcos catalogue, their
proper names and Bayer/Flamsteed designations, plus the 89 IAU constellation
line figures and their names, as distributed in the \`stars.8.json\`,
\`starnames.json\`, \`constellations.lines.json\` and \`constellations.json\`
data files of **d3-celestial** by Olaf Frohn.

Source: https://github.com/ofrohn/d3-celestial

    Copyright (c) 2015-2022, Olaf Frohn
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of the copyright holder nor the names of its
      contributors may be used to endorse or promote products derived from this
      software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
    ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
    LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
    CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.

## milkyway.webp -- diffuse all-sky background

"Deep Star Maps 2020", the Milky Way layer, equirectangular in equatorial
(ICRS) coordinates, downscaled from the 32768x16384 master.

Source: https://svs.gsfc.nasa.gov/4851/
Obtained via Wikimedia Commons, because svs.gsfc.nasa.gov serves
\`Access-Control-Allow-Origin\` for a single unrelated origin.

    NASA/Goddard Space Flight Center Scientific Visualization Studio
    (Ernie Wright, Laurence Schuler, Ian Jones).
    Gaia DR2: ESA/Gaia/DPAC.

This file ships with the built app at `/sky/LICENSES.md`, which is how the
notice travels with the data it describes. The sky view itself no longer
prints a credit line over the sky; the selected-object card carries the
provenance that changes (which orbital elements, published when).
`

async function main() {
  mkdirSync(OUT, { recursive: true })
  console.log('Building sky data into public/sky/\n')

  const starsOnly = process.argv.includes('--stars-only')

  const starsJson = await get(STARS_URL, 'star catalogue')
  const stars = buildStars(starsJson)
  writeFileSync(join(OUT, 'stars.bin'), stars)

  const starNamesJson = await get(STARNAMES_URL, 'star names')
  writeFileSync(
    join(OUT, 'starnames.json'),
    JSON.stringify(buildStarNames(starsJson, starNamesJson)) + '\n',
  )

  const linesJson = await get(LINES_URL, 'constellation lines')
  writeFileSync(join(OUT, 'constellations.bin'), buildConstellationLines(linesJson))

  const namesJson = await get(NAMES_URL, 'constellation names')
  writeFileSync(
    join(OUT, 'constellations.json'),
    JSON.stringify(buildConstellationNames(namesJson)) + '\n',
  )

  if (!starsOnly) {
    const thumb = await commonsThumb(MILKYWAY_TITLE, MILKYWAY_WIDTH)
    const jpeg = await get(thumb, `Milky Way (${MILKYWAY_WIDTH}px)`)
    const webp = toWebp(jpeg)
    writeFileSync(join(OUT, 'milkyway.webp'), webp)
  }

  writeFileSync(join(OUT, 'LICENSES.md'), LICENSES)

  console.log('\nWrote:')
  for (const name of [
    'stars.bin',
    'starnames.json',
    'constellations.bin',
    'constellations.json',
    'milkyway.webp',
    'LICENSES.md',
  ]) {
    const buf = readFileSync(join(OUT, name))
    const gz = gzipSync(buf).length
    const sha = createHash('sha256').update(buf).digest('hex').slice(0, 16)
    console.log(
      `  ${name.padEnd(16)} ${String(statSync(join(OUT, name)).size).padStart(9)} B` +
        `  (${String(gz).padStart(8)} B gzipped)  sha256:${sha}`,
    )
  }
  console.log(
    '\nNow verify the background is aligned before trusting it:\n' +
      '  node scripts/check-sky-alignment.mjs',
  )
}

main().catch((err) => {
  console.error('\nFAILED:', err.message)
  process.exitCode = 1
})
