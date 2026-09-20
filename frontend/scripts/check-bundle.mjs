/**
 * Guard the code split.
 *
 *     npm run build && node scripts/check-bundle.mjs
 *
 * three.js is ~425 KB and is loaded lazily, so the sidebar and the nightbar
 * are interactive before the renderer has finished downloading. That split is
 * one `React.lazy` and is trivially undone by a single VALUE import from
 * `sky/scene.ts` anywhere in the eager graph -- a `type` import is erased and
 * harmless, an ordinary one drags the whole renderer into the main chunk.
 *
 * It has already happened once, when `DEFAULT_LAYERS` moved into `scene.ts`:
 * the main bundle went from 480 KB to 914 KB, the build stayed green, and
 * nothing looked wrong. Hence this file, and hence `sky/layers.ts`.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ASSETS = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist', 'assets')

/** A string that appears in three.js and nowhere else we ship. */
const THREE_FINGERPRINT = 'THREE.WebGLRenderer'
const MAIN_LIMIT_KB = 600

let failed = false
const check = (ok, msg) => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'}  ${msg}`)
  if (!ok) failed = true
}

const files = readdirSync(ASSETS).filter((f) => f.endsWith('.js'))
const main = files.find((f) => f.startsWith('index-'))
const scene = files.find((f) => f.startsWith('SkyScene-'))

if (!main) {
  console.error('no index-*.js in dist/assets — run `npm run build` first')
  process.exit(2)
}

const kb = (f) => statSync(join(ASSETS, f)).size / 1024
console.log('\nChunks:')
for (const f of files.sort()) console.log(`        ${f.padEnd(34)} ${kb(f).toFixed(0).padStart(5)} kB`)
console.log()

check(!!scene, 'the renderer is in its own lazily-loaded chunk')
check(
  kb(main) < MAIN_LIMIT_KB,
  `the main chunk is ${kb(main).toFixed(0)} kB (limit ${MAIN_LIMIT_KB} kB)`,
)
check(
  !readFileSync(join(ASSETS, main), 'utf8').includes(THREE_FINGERPRINT),
  'three.js is NOT in the main chunk',
)
if (scene) {
  check(
    readFileSync(join(ASSETS, scene), 'utf8').includes(THREE_FINGERPRINT),
    'three.js IS in the scene chunk',
  )
}

console.log(failed ? '\nBUNDLE CHECK FAILED\n' : '\nBundle split is intact.\n')
process.exitCode = failed ? 1 : 0
