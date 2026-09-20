/**
 * All the weather, and the sky it makes, in one box on the left.
 *
 * This was three boxes -- the weather at the cursor here, the outlook and the
 * conditions over on the right -- and between them they said "cloud" three
 * times, "seeing" twice, and carried two copies of the same thresholds. One
 * box, read top to bottom, is the whole sky story.
 *
 * TWO KINDS OF NUMBER live here, and they answer different questions:
 *
 *   NOW      the cursor's slot, from the forecast in force at that moment. It
 *            changes at every slot crossing, and the whole box switches --
 *            with a flash -- when a newer forecast run takes over.
 *   TONIGHT  the PLAN's outlook. It belongs to the plan's decision point, not
 *            to the cursor, and after dusk it counts only the hours that are
 *            left. Scrubbing inside one decision interval does not move it.
 *
 * They can disagree and both be right: a clear slot inside a cloudy night. So
 * each is under its own heading. Unlabelled, one of them reads as a bug, and
 * then neither is believed.
 *
 * The Tonight block reads `plan.outlook` and nothing else. It holds still
 * while the cursor moves within a decision interval, and changes only when the
 * cursor crosses into the next one -- which looks unresponsive, and is the
 * point. Making it follow the cursor "so it keeps up" would leave an advisory
 * that describes no plan at all: the headline would summarise hours the
 * scheduler never solved for, beside numbers that came from a plan it is no
 * longer describing. Verified by scrubbing, not by reading: three slots inside
 * decision 1 of 3 move Now and leave Tonight byte-identical; forty slots, into
 * decision 2, move Tonight and its "remaining dark hours" wording with it.
 *
 * `null` from the source means "did not say", and is shown as a dash rather
 * than guessed. Where no forecast reaches, `cloudFraction` holds the plan's
 * clear-sky ASSUMPTION of zero -- never drawn as a clear sky, always as a
 * stub, because "nobody said" and "it will be clear" are different nights.
 * Seeing is not forecast by any source used here; it is the planning
 * assumption, and labelled as such.
 */

import { useEffect, useRef, useState } from 'react'
import type { Plan, QualityGrid, Session } from '../api/client'
import { hhmm, shortName } from '../format'
import { useCursorSlot } from '../state/cursorStore'
import '../styles/weather.css'
import '../styles/outlook.css'
import { More } from './Panel'

/**
 * The outlook's own thresholds (api/outlook.py: CATEGORIES, DEW_SPREAD_C,
 * DEW_HUMIDITY, WIND_* and GUST_*), so this box and the server never call the
 * same sky two different things. One copy, here, for the whole box.
 */
const SKY: [number, string][] = [
  [1 / 8, 'Clear'],
  [3 / 8, 'Mostly clear'],
  [5 / 8, 'Partly cloudy'],
  [7 / 8, 'Mostly cloudy'],
]
const DEW_SPREAD_C = 2
const DEW_HUMIDITY = 0.95
const WIND_WARN_MS = 8
const WIND_CRIT_MS = 12
const GUST_WARN_MS = 12
const GUST_CRIT_MS = 17

const VERDICT_CHIP: Record<string, string> = {
  go: 'chip chip--ok',
  marginal: 'chip chip--warn',
  poor: 'chip chip--crit',
}

const LIGHT: Record<string, string> = {
  natural: 'Natural airglow',
  artificial: 'Light pollution',
  moon: 'Moonlight',
  twilight: 'Twilight',
  cloudDelta: 'Cloud',
}

/** NWS sky-cover terms by eighths (oktas), compared as the outlook does. */
function skyWord(cover: number): string {
  return SKY.find(([limit]) => cover < limit)?.[1] ?? 'Overcast'
}

const at = (arr: (number | null)[] | undefined, i: number): number | null => {
  const v = arr?.[i]
  return v === undefined || v === null || !Number.isFinite(v) ? null : v
}

const fmt = (v: number | null, digits: number, unit: string) =>
  v === null ? '—' : `${v.toFixed(digits)}${unit}`

const pct = (f: number): string => `${Math.round(f * 100)}%`

export function WeatherBox({
  session,
  plan,
  grid,
  targetId,
}: {
  session: Session
  plan: Plan
  /** For the sky-brightness decomposition, which is per target. */
  grid: QualityGrid | undefined
  targetId: string | null
}) {
  const { slot, tMs } = useCursorSlot()
  const w = plan.slotWeather
  const o = plan.outlook

  // Flash when a new forecast takes over, so an update is seen, not inferred.
  const [flash, setFlash] = useState(false)
  const lastPlan = useRef(plan.planId)
  useEffect(() => {
    if (lastPlan.current === plan.planId) return
    lastPlan.current = plan.planId
    setFlash(true)
    const id = setTimeout(() => setFlash(false), 1400)
    return () => clearTimeout(id)
  }, [plan.planId])

  // Whether a forecast reaches a slot is the SERVER's call
  // (`slotWeather.covered`), never inferred from which fields are null. An
  // older payload without the flags is taken as covered, and a plan with no
  // forecast at all as not.
  const noForecast = !o.hasData
  const flags = w?.covered
  const covered = !noForecast && (!flags?.length || flags[slot] === true)

  const cloud = covered ? at(plan.cloudFraction, slot) : null
  const clear = cloud === null ? null : 1 - cloud
  const temp = at(w?.temperatureC, slot)
  const dew = at(w?.dewPointC, slot)
  const spread = temp !== null && dew !== null ? temp - dew : null
  const humidity = at(w?.humidity, slot)
  const dewRisk =
    (spread !== null && spread < DEW_SPREAD_C) || (humidity !== null && humidity >= DEW_HUMIDITY)
  const wind = at(w?.windMs, slot)
  const gust = at(w?.windGustMs, slot)
  const precip = at(w?.precipitationMm, slot)
  const windLevel =
    (wind ?? 0) >= WIND_CRIT_MS || (gust ?? 0) >= GUST_CRIT_MS
      ? ' wx__crit'
      : (wind ?? 0) >= WIND_WARN_MS || (gust ?? 0) >= GUST_WARN_MS
        ? ' wx__warn'
        : ''
  const seeing = at(plan.seeingFwhmArcsec, slot)
  const best = o.bestWindow
  const layers = [
    ['low', at(w?.cloudLow, slot)],
    ['mid', at(w?.cloudMid, slot)],
    ['high', at(w?.cloudHigh, slot)],
  ] as const
  const hasLayers = layers.some(([, v]) => v !== null)

  return (
    <section
      className={`wx${flash ? ' wx--flash' : ''}`}
      aria-label="Weather and sky conditions"
    >
      <header className="wx__head">
        <h2>Weather</h2>
      </header>

      {/* -- now: the cursor's slot ------------------------------------- */}
      <h3 className="eyebrow">Now · {new Date(tMs).toISOString().slice(11, 16)} UTC</h3>

      <div className="wx__main">
        <div>
          <div className="wx__word">{cloud === null ? 'No forecast' : skyWord(cloud)}</div>
          <div className="wx__sub">
            {cloud === null
              ? 'the plan assumes a clear sky here'
              : `${Math.round(cloud * 100)}% cloud cover`}
          </div>
        </div>
        <div className="wx__clear num" title="Share of the sky forecast clear">
          {clear === null ? '—' : pct(clear)}
          <span>clear</span>
        </div>
      </div>

      <dl className="wx__rows">
        {/* Not duplication of the total above: 55% of thin cirrus and 55% of
            low stratus are the same number and different nights, and only
            one of them is worth setting up for.

            The "est" tag is load-bearing, not politeness. These layers do not
            decompose the total and routinely exceed it -- measured at 889 of
            3,225 hours across the cached ECMWF runs, e.g. a 66% total with
            high cloud at 100% -- because for that model the layers are
            estimates while the total is the model's own field. The plan uses
            the total; anyone "fixing" these to sum to it would be inventing
            data. */}
        {covered && hasLayers && (
          <>
            <dt title="Per-layer estimates from the forecast source. They overlap and can exceed the total cloud, which is what the plan uses.">
              Cloud layers
            </dt>
            <dd className="num">
              {layers.map(([k, v]) => `${k} ${v === null ? '—' : Math.round(v * 100)}`).join(' · ')}
              <span className="wx__note"> est</span>
            </dd>
          </>
        )}
        <dt>Seeing</dt>
        <dd className="num">
          {fmt(seeing, 1, '″')}
          {!w?.seeingForecast && <span className="wx__note"> assumed</span>}
        </dd>
        <dt>Temperature</dt>
        <dd className="num">{fmt(temp, 1, ' °C')}</dd>
        <dt>Dew point</dt>
        <dd className={`num${dewRisk ? ' wx__warn' : ''}`}>
          {fmt(dew, 1, ' °C')}
          {/* The humidity is on this row rather than one of its own because
              it is half of what decides the flag beside it -- the other half
              being the spread against the temperature above. A reader who
              wants to know why it says "dew likely" should not have to open
              a fold labelled Source to find the number that said so. */}
          {humidity !== null && <span className="wx__note"> · {pct(humidity)} rh</span>}
          {dewRisk && <span> · dew likely</span>}
        </dd>
        <dt>Wind</dt>
        <dd className={`num${windLevel}`}>
          {fmt(wind, 1, ' m/s')}
          {gust !== null && ` · gusts ${gust.toFixed(0)}`}
        </dd>
        <dt>Precipitation</dt>
        <dd className={`num${precip !== null && precip > 0 ? ' wx__warn' : ''}`}>
          {fmt(precip, 1, ' mm/h')}
        </dd>
      </dl>

      {/*
        -- tonight: the plan's outlook ----------------------------------

        The verdict chip belongs HERE, not in the box's header. It is the
        night's word, drawn from the same NWS vocabulary as the Now sky word,
        so a header chip reading "Clear" could sit directly above a Now block
        reading "Overcast" -- both correct, and nothing saying which was which.
      */}
      <div className="wx__head">
        <h3 className="eyebrow">Tonight</h3>
        <span className={VERDICT_CHIP[o.verdict] ?? 'chip'}>{o.label}</span>
      </div>

      <div className="outlook__headline">{o.headline}</div>
      {o.verdictText && <div className="dim outlook__verdict">{o.verdictText}</div>}

      <CloudStrip plan={plan} session={session} cursorMs={tMs} />

      <dl className="wx__rows">
        {o.darkHours <= 0 ? null : o.hasData ? (
          <>
            <dt>Clear</dt>
            <dd className="num">
              {o.clearHours.toFixed(1)} of {o.darkHours.toFixed(1)} h
            </dd>
            <dt>Mean cloud</dt>
            <dd className="num">{pct(o.meanCloudFraction)}</dd>
          </>
        ) : (
          // With no forecast the server's clear hours describe the ASSUMED
          // clear sky; showing them beside "No forecast" would read as one.
          <>
            <dt>Forecast</dt>
            <dd className="num">none for {o.darkHours.toFixed(1)} h</dd>
          </>
        )}
        {best && (
          <>
            <dt>Best window</dt>
            <dd className="num">
              {hhmm(best.startsAt)}&ndash;{hhmm(best.endsAt)} UTC &middot; {best.hours.toFixed(1)} h
              at {pct(best.meanCloudFraction)}
            </dd>
          </>
        )}
      </dl>

      {o.notes.length > 0 && (
        <ul className="outlook__notes">
          {o.notes.map((n, i) => (
            <li key={i} className={`outlook__note outlook__note--${n.severity}`}>
              {n.text}
            </li>
          ))}
        </ul>
      )}

      <SkyLight session={session} grid={grid} slot={slot} targetId={targetId} />

      <More label="Source">
        <div>{w?.basis || o.basis || 'No forecast record reached this plan.'}</div>
        <div className="faint">
          {session.weather.label}. As of {plan.asOf.slice(11, 16)} UTC.
        </div>
        <div className="faint">Seeing is not forecast; the plan assumes 2.5&Prime;.</div>
        {/* How well covered this night's replay was -- a fact about THIS
            night, not boilerplate. Thin coverage is the first thing to
            suspect when a replay looks too smooth. */}
        {session.weather.runs > 0 && (
          <div className="faint">
            {session.weather.runs} model run{session.weather.runs === 1 ? '' : 's'},{' '}
            {session.weather.records} hourly records.
          </div>
        )}
      </More>
    </section>
  )
}

/**
 * What is making the sky the brightness it is, at the cursor.
 *
 * The decomposition is the part worth having. A single "20.4 mag/arcsec²" says
 * the slot is mediocre; the breakdown says the MOON is 71% of the flux, which
 * is actionable -- it will move -- in a way that a total never is. It is per
 * target and it is the working rather than the answer, so it sits behind the
 * disclosure with the Sun and the Moon that drive it.
 *
 * Kept in nanoLamberts throughout and converted once, because these components
 * SUM and magnitudes do not. `cloudDelta` is legitimately negative at a
 * pristine site, where cloud darkens the sky rather than amplifying city glow.
 */
function SkyLight({
  session,
  grid,
  slot,
  targetId,
}: {
  session: Session
  grid: QualityGrid | undefined
  slot: number
  targetId: string | null
}) {
  const row = grid?.rows.find((r) => r.targetId === targetId) ?? grid?.rows[0]
  const parts = row
    ? Object.entries(row.skyComponentsNl)
        .map(([k, arr]) => ({ k, v: arr[slot] ?? 0 }))
        .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
    : []
  const total = parts.reduce((a, p) => a + p.v, 0)

  return (
    <More label={`What lights the sky${row ? ` · ${shortName(row.name)}` : ''}`}>
      <dl className="wx__rows">
        <dt>Sun</dt>
        <dd className="num">{(session.sunAltitudeDeg[slot] ?? 0).toFixed(1)}&deg;</dd>
        <dt>Moon</dt>
        <dd className="num">
          {(session.moon.altitudeDeg[slot] ?? 0).toFixed(0)}&deg;,{' '}
          {((session.moon.illumination[slot] ?? 0) * 100).toFixed(0)}% lit
        </dd>
        {row && (
          <>
            <dt>Sky</dt>
            <dd className="num">{(row.skyMagArcsec2[slot] ?? 0).toFixed(2)} mag/&#9633;&Prime;</dd>
          </>
        )}
      </dl>

      {parts.length > 0 && total > 0 && (
        <div style={{ display: 'grid', gap: 3 }}>
          {parts.map(({ k, v }) => {
            const share = v / total
            return (
              <div key={k} className="row spread" style={{ fontSize: 11 }}>
                <span className="dim" style={{ minWidth: 104 }}>
                  {LIGHT[k] ?? k}
                </span>
                <span
                  aria-hidden
                  style={{
                    flex: 1,
                    height: 4,
                    borderRadius: 2,
                    background: 'var(--line-soft)',
                    overflow: 'hidden',
                  }}
                >
                  <span
                    style={{
                      display: 'block',
                      height: '100%',
                      width: `${Math.min(100, Math.abs(share) * 100)}%`,
                      background: v < 0 ? 'var(--accent-deep)' : 'var(--moon)',
                    }}
                  />
                </span>
                <span className="num faint" style={{ minWidth: 46, textAlign: 'right' }}>
                  {v < 0 ? '−' : ''}
                  {(Math.abs(share) * 100).toFixed(0)}%
                </span>
              </div>
            )
          })}
        </div>
      )}
    </More>
  )
}

/**
 * Forecast cloud across the session, one bar per slot, with the best window
 * bracketed. Taller and more opaque is cloudier. The values are the plan's own
 * per-slot array, so this strip and the scheduler never disagree.
 *
 * A slot no forecast reaches holds the plan's clear-sky ASSUMPTION (zero), so
 * it is drawn as a faint stub, never as a short "clear" bar. Slots before the
 * plan's decision point are greyed: the outlook no longer speaks for them.
 */
function CloudStrip({
  plan,
  session,
  cursorMs,
}: {
  plan: Plan
  session: Session
  /** Where the cursor is, so the strip can say "you are here". Without it the
   *  only time references are the best-window bracket and the greyed past,
   *  and the greyed past is the plan's decision point, not the cursor. */
  cursorMs: number
}) {
  const cloud = plan.cloudFraction
  const n = cloud.length
  if (!plan.outlook.hasData || n === 0) return null
  const startMs = Date.parse(session.grid.start)
  const spanMs = Date.parse(session.grid.end) - startMs
  const best = plan.outlook.bestWindow
  // One flag per plan slot, i.e. per grid slot. A payload without them is
  // covered wherever the plan has a forecast at all.
  const covered = plan.slotWeather?.covered
  const slotCovered = (i: number): boolean =>
    covered === undefined || covered.length === 0 || covered[i] === true
  const past = Math.min(Math.max(plan.changes.lockedThroughSlot, 0), n)
  const x = (iso: string) => ((Date.parse(iso) - startMs) / spanMs) * 100
  return (
    <svg
      className="outlook__strip"
      viewBox="0 0 100 20"
      preserveAspectRatio="none"
      role="img"
      aria-label="Forecast cloud cover across the night"
    >
      {best && (
        <rect
          className="outlook__best"
          x={x(best.startsAt)}
          y={0}
          width={Math.max(x(best.endsAt) - x(best.startsAt), 0.5)}
          height={20}
        />
      )}
      {(() => {
        const f = ((cursorMs - startMs) / spanMs) * 100
        return f < -0.5 || f > 100.5 ? null : (
          <rect
            className="outlook__now"
            fill="var(--accent)"
            x={Math.min(99.6, Math.max(0, f))}
            y={0}
            width={0.4}
            height={20}
          >
            <title>The cursor</title>
          </rect>
        )
      })()}
      {cloud.map((c, i) => {
        if (!slotCovered(i)) {
          return (
            <rect
              key={i}
              className="outlook__bar outlook__bar--none"
              x={(i / n) * 100}
              y={18}
              width={100 / n}
              height={2}
            >
              <title>No forecast reaches this slot: the plan assumes a clear sky</title>
            </rect>
          )
        }
        const h = Math.max(c, 0.02) * 18
        return (
          <rect
            key={i}
            className={i < past ? 'outlook__bar outlook__bar--past' : 'outlook__bar'}
            x={(i / n) * 100}
            y={20 - h}
            width={100 / n}
            height={h}
            opacity={0.25 + 0.75 * c}
          />
        )
      })}
    </svg>
  )
}
