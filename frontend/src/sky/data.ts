/**
 * Loading the vendored sky, and surviving its absence.
 *
 * Everything here is optional. The scene must mount, orient itself and be
 * interactive with no star catalogue and no background image at all -- that is
 * a hard requirement, not a nicety, because the alternative is a black page at
 * a dark site with no connectivity, which is exactly where this app is
 * supposed to be most useful.
 *
 * `public/sky/manifest.json` is written by `scripts/check-sky-alignment.mjs`
 * after it VERIFIES the background's projection rather than assuming it. The
 * renderer reads the mapping from there so the two cannot drift: a future
 * source image with a different convention changes the manifest, and the
 * shader follows.
 */

export interface SkyManifest {
  stars: {
    file: string
    count: number
    stride: number
    headerBytes: number
    fields: string[]
    credit: string
  }
  background: {
    file: string
    width: number
    height: number
    projection: string
    frame: string
    /** Right ascension, degrees, at u = 0. */
    raAtLeftEdgeDeg: number
    raIncreasesTo: 'left' | 'right'
    decAtTopDeg: number
    credit: string
  }
}

export interface StarField {
  count: number
  /** Unit vectors in ICRS, xyz interleaved. */
  position: Float32Array
  magnitude: Float32Array
  bv: Float32Array
}

export interface ConstellationLines {
  /** Independent SEGMENT PAIRS: draw with one `LineSegments`, no index table. */
  position: Float32Array
  segments: number
}

export interface ConstellationName {
  id: string
  name: string
  abbr: string
  raDeg: number
  decDeg: number
}

const MAGIC = 0x54534b59 // 'TSKY'
const LINE_MAGIC = 0x54434e4c // 'TCNL'
const BASE = 'sky/'

async function fetchOptional(path: string): Promise<Response | null> {
  try {
    const res = await fetch(new URL(BASE + path, document.baseURI), { cache: 'force-cache' })
    return res.ok ? res : null
  } catch {
    return null
  }
}

export async function loadManifest(): Promise<SkyManifest | null> {
  const res = await fetchOptional('manifest.json')
  if (!res) return null
  try {
    return (await res.json()) as SkyManifest
  } catch {
    return null
  }
}

/**
 * Decode `stars.bin` straight into typed arrays.
 *
 * The binary exists to avoid parsing 640 KB of JSON during first paint --
 * which is a main-thread stall at precisely the moment the user is waiting to
 * see something. The header is checked rather than trusted: a stale or
 * truncated file should produce "no stars" and a console line, never a
 * `RangeError` from somewhere inside the renderer three frames later.
 */
export async function loadStars(manifest: SkyManifest | null): Promise<StarField | null> {
  const name = manifest?.stars.file ?? 'stars.bin'
  const res = await fetchOptional(name)
  if (!res) return null

  const buf = await res.arrayBuffer()
  if (buf.byteLength < 16) return null
  const head = new DataView(buf)
  if (head.getUint32(0, true) !== MAGIC) {
    console.warn(`${name}: not a star binary (bad magic); rendering without stars`)
    return null
  }
  const count = head.getUint32(8, true)
  const stride = head.getUint32(12, true)
  if (stride !== 20 || 16 + count * stride > buf.byteLength) {
    console.warn(`${name}: header says ${count} x ${stride} B but the file is ${buf.byteLength} B`)
    return null
  }

  const src = new Float32Array(buf, 16, count * 5)
  const position = new Float32Array(count * 3)
  const magnitude = new Float32Array(count)
  const bv = new Float32Array(count)
  for (let i = 0; i < count; i++) {
    const o = i * 5
    position[i * 3] = src[o] ?? 0
    position[i * 3 + 1] = src[o + 1] ?? 0
    position[i * 3 + 2] = src[o + 2] ?? 0
    magnitude[i] = src[o + 3] ?? 6
    bv[i] = src[o + 4] ?? 0.65
  }
  return { count, position, magnitude, bv }
}

/**
 * The 89 IAU constellation figures, as segment pairs.
 *
 * Drawn faintly by default, and pushed further back by "focus on plan": they
 * are how you find your way around, not what you came to see.
 */
export async function loadConstellations(): Promise<ConstellationLines | null> {
  const res = await fetchOptional('constellations.bin')
  if (!res) return null
  const buf = await res.arrayBuffer()
  if (buf.byteLength < 16) return null
  const head = new DataView(buf)
  if (head.getUint32(0, true) !== LINE_MAGIC) {
    console.warn('constellations.bin: bad magic; drawing without figures')
    return null
  }
  const count = head.getUint32(8, true)
  if (16 + count * 12 > buf.byteLength || count % 2 !== 0) {
    console.warn(`constellations.bin: ${count} vertices does not fit ${buf.byteLength} B`)
    return null
  }
  return { position: new Float32Array(buf.slice(16, 16 + count * 12)), segments: count / 2 }
}

export async function loadConstellationNames(): Promise<ConstellationName[]> {
  const res = await fetchOptional('constellations.json')
  if (!res) return []
  try {
    return (await res.json()) as ConstellationName[]
  } catch {
    return []
  }
}

/**
 * The background, as an `ImageBitmap`, or null.
 *
 * Decoded off the main thread: a 3840x1920 WebP costs tens of milliseconds to
 * decode, and doing that synchronously drops frames right as the scene appears.
 */
export async function loadBackground(manifest: SkyManifest | null): Promise<ImageBitmap | null> {
  const name = manifest?.background.file ?? 'milkyway.webp'
  const res = await fetchOptional(name)
  if (!res) return null
  try {
    return await createImageBitmap(await res.blob(), { imageOrientation: 'none' })
  } catch (err) {
    console.warn(`${name}: could not be decoded`, err)
    return null
  }
}

/**
 * Proper names and Bayer/Flamsteed designations, keyed by index into
 * stars.bin: `[index, properName, designation]`. Either string may be empty.
 */
export type StarName = [number, string, string]

export async function loadStarNames(): Promise<StarName[]> {
  const res = await fetchOptional('starnames.json')
  if (!res) return []
  try {
    return (await res.json()) as StarName[]
  } catch {
    return []
  }
}

export interface SkyAssets {
  manifest: SkyManifest | null
  stars: StarField | null
  background: ImageBitmap | null
  constellations: ConstellationLines | null
  constellationNames: ConstellationName[]
  starNames: StarName[]
}

/**
 * Fetch everything in parallel and never reject.
 *
 * `Promise.all` would be wrong: one missing file would take the other two down
 * with it, turning a partially-degraded sky into no sky.
 */
export async function loadSkyAssets(): Promise<SkyAssets> {
  const manifest = await loadManifest()
  const [stars, background, constellations, constellationNames, starNames] = await Promise.all([
    loadStars(manifest).catch(() => null),
    loadBackground(manifest).catch(() => null),
    loadConstellations().catch(() => null),
    loadConstellationNames().catch(() => []),
    loadStarNames().catch(() => []),
  ])
  return { manifest, stars, background, constellations, constellationNames, starNames }
}
