/** Everything you entered, folded away once a plan exists. */

import type { Session } from '../../api/client'
import { Collapsible } from '../Panel'

/** Two significant figures: 0.0005 e⁻/s must not read as 0.001. */
const sig = (n: number) => (n === 0 ? '0' : String(Number(n.toPrecision(2))))

export function Specs({ session }: { session: Session }) {
  const s = session.site
  const e = session.equipment
  const reduced = Math.abs(e.reducer - 1) > 1e-6
  return (
    <Collapsible title="Specifications">
      <dl className="kv">
        <dt>Site</dt>
        <dd>{s.name}</dd>
        <dt>Position</dt>
        <dd>
          {s.latitudeDeg.toFixed(3)}, {s.longitudeDeg.toFixed(3)}
        </dd>
        <dt>Sky</dt>
        <dd>
          Bortle {s.bortle} &middot; {s.zenithSkyMagArcsec2.toFixed(1)} mag/&#9633;&Prime;
        </dd>
        <dt>Floor</dt>
        <dd>
          {s.minAltitudeDeg.toFixed(0)}&deg; alt &middot; {s.minMoonSeparationDeg.toFixed(0)}&deg;
          from Moon
        </dd>
        <dt>Extinction</dt>
        <dd>{s.extinctionK.toFixed(2)} mag/airmass</dd>

        <dt style={{ paddingTop: 8 }}>Telescope</dt>
        <dd style={{ paddingTop: 8 }}>{e.telescopeName ?? '—'}</dd>
        <dt>Aperture</dt>
        <dd className="num">
          {e.apertureMm.toFixed(0)} mm &middot; {e.collectingAreaCm2.toFixed(0)} cm&sup2;
          {e.centralObstructionMm > 0 &&
            ` · ${(e.obstructionFraction * 100).toFixed(0)}% obstructed`}
        </dd>
        <dt>Focal length</dt>
        <dd className="num">
          {e.focalLengthMm.toFixed(0)} mm f/{e.focalRatio.toFixed(1)}
          {reduced && ` (${e.nativeFocalLengthMm.toFixed(0)} mm × ${e.reducer})`}
        </dd>
        <dt>Resolving</dt>
        <dd className="num">
          {e.dawesLimitArcsec.toFixed(2)}&Prime; Dawes &middot; {e.diffractionFwhmArcsec.toFixed(2)}
          &Prime; Airy core
        </dd>
        <dt>Throughput</dt>
        <dd className="num">{(e.throughput * 100).toFixed(0)}%</dd>

        <dt style={{ paddingTop: 8 }}>Camera</dt>
        <dd style={{ paddingTop: 8 }}>{e.cameraName ?? '—'}</dd>
        <dt>Scale</dt>
        <dd className="num">
          {e.pixelScaleArcsec.toFixed(2)}&Prime;/px &middot; {e.samplingPxPerFwhm.toFixed(1)} px per
          star ({e.sampling})
        </dd>
        <dt>Field</dt>
        <dd className="num">
          {e.fovWidthDeg.toFixed(2)} &times; {e.fovHeightDeg.toFixed(2)}&deg;
        </dd>
        <dt>Sensor</dt>
        <dd className="num">
          QE {(e.quantumEfficiency * 100).toFixed(0)}% &middot; {e.readNoiseE.toFixed(1)} e&#8315;
          read &middot; {sig(e.darkCurrentEPerS)} e&#8315;/s dark
        </dd>
        <dt>Mount</dt>
        <dd>
          {e.switchMinutes.toFixed(0)} min slew &middot; {e.minBlockMinutes.toFixed(0)} min min
          block
        </dd>

        <dt style={{ paddingTop: 8 }}>Weather</dt>
        <dd style={{ paddingTop: 8 }}>{session.weather.label}</dd>
        {/* Reachable once a session exists; the target picker carries the same
            link before one does, which is where the catalogue is on show. */}
        <dt style={{ paddingTop: 8 }}>Catalogue</dt>
        <dd style={{ paddingTop: 8 }}>
          <a className="specs__notice" href="/api/catalog/notice" target="_blank" rel="noopener">
            OpenNGC &middot; CC BY-SA 4.0
          </a>
        </dd>
      </dl>
    </Collapsible>
  )
}
