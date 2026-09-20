/**
 * The app.
 *
 * Layout: a full-bleed first-person sky -- you are standing on the ground
 * looking up -- with a thin bar across the top, a bar along the bottom
 * (where, how, when), floating translucent panels on the right, and the
 * nightbar in normal flow underneath. The sky is a VIEW, not the app: every
 * panel reads the plan and the cursor, not the scene, so the night is still
 * legible if the sky cannot be drawn at all.
 *
 * The right dock is ordered by what changes fastest: what to do now -- with
 * the whole night's schedule one click under it -- then what has happened,
 * then the evidence behind it, then the specifications you typed in and will
 * rarely look at again.
 *
 * The left column is the sky's own state: everything the weather does, and
 * everything it does to the sky, in one box, with whatever you last clicked
 * underneath it. It was three boxes on two sides of the screen, which meant
 * reading "cloud" in three places and comparing them by memory.
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, type SessionRequest } from './api/client'
import { CATEGORY, changed, decisionCategory } from './alerts/model'
import { useAlertToasts, useAlerts } from './alerts/useAlerts'
import { BottomBar } from './components/BottomBar'
import { FoldBanner } from './components/FoldBar'
import { ObjectInfo, type PlanAction } from './components/ObjectInfo'
import { Setup } from './components/Setup'
import { Modal } from './components/Modal'
import { Toasts, useToasts } from './components/Toasts'
import { TopBar } from './components/TopBar'
import { WeatherBox } from './components/WeatherBox'
import { Nightbar, type NightMarker } from './components/nightbar/Nightbar'
import { Activity } from './components/panels/Activity'
import { Evidence } from './components/panels/Evidence'
import { Inspector } from './components/panels/Inspector'
import { NowCard } from './components/panels/NowCard'
import { Specs } from './components/panels/Specs'
import { DEFAULT_OPTIONS, type SkyOptions } from './sky/layers'
// From `sky/layers`, NOT `sky/scene`: `sameSelection` is a value, and a value
// import from `scene.ts` pulls all of three.js into the main chunk and silently
// undoes the lazy split below (measured: 480 KB -> 941 KB, build still green).
// `scripts/check-bundle.mjs` is what catches it.
import { sameSelection, type SkySelection } from './sky/layers'
import type { SkyApi } from './sky/SkyScene'
import {
  useCreateNight,
  useGeometry,
  useGrid,
  usePlan,
  usePresets,
  useSatellites,
  useSession,
  useSky,
} from './hooks'
import { useAddTarget, useCatalog } from './plan/amend'
import { cursor, useCursorSlot, useDecisionIndex } from './state/cursorStore'
import { slotClock } from './time/slots'

/**
 * Code-split, so the sidebar and the nightbar are interactive before the
 * renderer has finished downloading. three.js is ~600 KB; nothing else on the
 * page should wait for it.
 */
const SkyScene = lazy(() => import('./sky/SkyScene').then((m) => ({ default: m.SkyScene })))

const NIGHTBAR_HEIGHT = 62

/**
 * Why the sky's remaining objects can never be scheduled.
 *
 * NEITHER PLANETS NOR STARS ARE AMONG THEM ANY MORE.
 *
 * The planets' reason used to be that "a scheduled block is a fixed right
 * ascension and declination held for an hour", which does not survive being
 * measured: against this rig's 27-arcminute frame Jupiter drifts 10 arcseconds
 * in an hour and Saturn 6. They resolve to real targets now
 * (`presets.planet_target`).
 *
 * The stars' reason was that "the exposure model works in surface brightness",
 * which was true only of `quality.efficiency`. The layer underneath always had
 * the point-source model — `source_e_per_s` with an encircled-energy fraction —
 * and `point_snr2_rate` now exposes it, selected by `Target.is_point_source`.
 * A star has no server-side identity at all, so the client sends its own
 * coordinates (`api.plannable`) rather than a name.
 *
 * The two left are answered here rather than by the server because neither has
 * any target to resolve to, at any coordinates.
 */
const UNSCHEDULABLE: Record<'moon' | 'satellite', string> = {
  moon:
    'The Moon is what the plan schedules around: every target keeps its distance from it, ' +
    'and the exclusion moves as it does. It is never a target itself.',
  satellite:
    'A satellite crosses the sky in minutes. They are drawn so you can see which frames one ' +
    'will cross, not to be observed.',
}

export default function App() {
  const presets = usePresets()
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [skyLost, setSkyLost] = useState(false)
  const [options, setOptions] = useState<SkyOptions>(DEFAULT_OPTIONS)
  const [skySelection, setSkySelection] = useState<SkySelection | null>(null)
  /**
   * Fade the two side docks down to a hint, so the sky can be looked at.
   *
   * NOT part of `SkyOptions`: that type is what the RENDERER is asked to draw
   * differently, and it says so. Whether the app's own chrome is in the way is
   * not a property of the sky, and putting it there would ship a re-render of
   * the scene every time someone wanted a clearer view of it.
   */
  const [panelsHidden, setPanelsHidden] = useState(false)
  const [skyApi, setSkyApi] = useState<SkyApi | null>(null)
  /** Why the last add did not happen. The night is unchanged whenever it is set. */
  const [refusal, setRefusal] = useState<string | null>(null)
  const { toasts, push, dismiss } = useToasts()

  const session = useSession(sessionId)
  const points = useMemo(() => session.data?.decisionPoints ?? [], [session.data])
  const ready = session.data?.status === 'ready' || points.length > 0

  const { slot } = useCursorSlot()
  const { index: decisionIndex } = useDecisionIndex()
  const geometry = useGeometry(sessionId, ready)
  const revision = session.data?.live?.revision ?? 0
  const plan = usePlan(sessionId, points, decisionIndex, revision)
  const grid = useGrid(sessionId, points, decisionIndex, revision)
  const sky = useSky(sessionId, !!geometry.data)
  const satellites = useSatellites(sessionId)
  const catalog = useCatalog()
  const { add: addTarget, busy: adding } = useAddTarget(sessionId)

  // The cursor's clock is the session's grid. Setting it also places the
  // cursor: at dusk for a night that is over, on the present -- following it --
  // for one that is still happening. Re-delivering the same grid (every live
  // refresh does) is a no-op, so a refresh never yanks a reviewing observer.
  useEffect(() => {
    cursor.setClock(session.data ? slotClock(session.data.grid) : null)
  }, [session.data])

  // The network tier. Decision points arrive as the fold progresses, so this
  // is re-published rather than set once.
  useEffect(() => {
    cursor.setDecisionTimes(points.map((dp) => Date.parse(dp.at)))
  }, [points])

  // No WebGL2 at all: say so, rather than mount a scene that cannot start.
  useEffect(() => {
    if (typeof WebGL2RenderingContext === 'undefined') setSkyLost(true)
  }, [])

  const createNight = useCreateNight()
  const create = useCallback(
    async (req: SessionRequest) => {
      setCreating(true)
      setCreateError(null)
      try {
        const night = await createNight.mutateAsync(req)
        setSessionId(night.session.id)
        setSelected(null)
        setSkySelection(null)
      } catch (err) {
        setCreateError(err instanceof ApiError ? err.message : String(err))
      } finally {
        setCreating(false)
      }
    },
    [createNight],
  )

  // -- alerts: popped as the cursor reaches them, marked on the nightbar ------
  const alerts = useAlerts(session.data, plan.data, geometry.data)
  useAlertToasts(alerts, push, plan.data)

  const markers: NightMarker[] = useMemo(
    () => [
      ...points.map((dp) => ({
        id: `dp-${dp.index}`,
        tMs: Date.parse(dp.at),
        kind: 'decision' as const,
        color: dp.index === 0 ? 'var(--accent)' : CATEGORY[decisionCategory(dp)].color,
        quiet: dp.index > 0 && !changed(dp),
        label: `${dp.at.slice(11, 16)} — ${dp.reason}`,
      })),
      // News is already on the bottom edge as its decision point. Of the rest,
      // info-level hazards stay in the Now card; the bar carries what is worth
      // seeing from across the night, and every satellite crossing, because
      // each is a specific frame to look at.
      ...alerts
        .filter((a) => a.block && (a.severity !== 'info' || a.category === 'satellite'))
        .map((a) => ({
          id: a.id,
          tMs: a.tMs,
          kind: 'alert' as const,
          color: CATEGORY[a.category].color,
          label: `${new Date(a.tMs).toISOString().slice(11, 16)} — ${a.title}`,
        })),
    ],
    [points, alerts],
  )

  /**
   * What "+ Add to schedule" does for the selection.
   *
   * Every object gets the button, including the ones that can never be
   * scheduled. A missing button answers nothing: the observer is left
   * wondering whether Jupiter is unschedulable or merely unselected. So the
   * refusal is the feature, and it is a MODAL rather than a toast -- the night
   * did not change, and that is exactly the belief a missed message leaves
   * wrong.
   *
   * Who answers which refusal is deliberate. A star, a planet, the Moon and a
   * satellite have no identity on the server at all, so they are answered
   * here. Everything the server can name -- a catalogue object it will not
   * plan, a target already in the plan, a target the optimiser cannot place --
   * is answered BY the server, so one wording exists per reason instead of two
   * that drift apart.
   */
  const planAction = useMemo((): PlanAction | null => {
    const sel = skySelection
    if (!sel) return null
    const refuse = (body: string) => setRefusal(body)

    // A star carries its own coordinates, because the server has no star
    // catalogue to look one up in. Everything else the server can name.
    const own = sel.kind === 'star' ? (skyApi?.plannable(sel) ?? null) : null
    const id =
      sel.kind === 'target' || sel.kind === 'catalog' || sel.kind === 'planet'
        ? sel.id
        : (own?.id ?? null)

    // Already in the night, with time still AHEAD of the cursor. The same test
    // the server makes before it refuses a duplicate (see `api/amend.py`), made
    // here too so the button can say so instead of spending a round trip to be
    // told. A target whose blocks are all behind the cursor is NOT scheduled
    // any more -- it can be asked for again, and the button offers that.
    const scheduled =
      id !== null && (plan.data?.blocks ?? []).some((b) => b.targetId === id && b.slotEnd > slot)
    if (scheduled) return { label: 'Scheduled', busy: false, done: true, onRun: () => {} }

    const run = async () => {
      if (sel.kind === 'moon' || sel.kind === 'satellite') {
        refuse(UNSCHEDULABLE[sel.kind])
        return
      }
      // A star's coordinates come from the renderer, so they are gone if the
      // scene never started. Say that, rather than send an add with no position.
      if (sel.kind === 'star' && own === null) {
        refuse(
          'That star\u2019s position comes from the sky view, which has not finished loading. ' +
            'Try again in a moment.',
        )
        return
      }
      if (!sessionId || id === null) {
        refuse('The night is still being planned. Try again when it is ready.')
        return
      }
      try {
        // Only the wire fields, spelled out: `own` also carries the id, which
        // travels as `target`, and a spread would post it twice under two names.
        const out = await addTarget(
          id,
          cursor.live.tMs,
          own === null
            ? undefined
            : {
                raDeg: own.raDeg,
                decDeg: own.decDeg,
                magnitude: own.magnitude,
                name: own.name,
                isPointSource: own.isPointSource,
              },
        )
        push({
          // The same id as the news alert for this decision point, so the
          // toast is not shown a second time when the cursor reaches it.
          id: `${out.sessionId}:dp-${out.decisionIndex}`,
          title: `Added ${out.targetName}`,
          body: out.message,
          severity: 'info',
          category: 'plan',
          when: out.at.slice(11, 16),
        })
        // It is a target of the night now -- but only take the observer there
        // if they are still looking at what they added. A re-plan runs for
        // seconds, and yanking the card away from whatever they clicked in
        // the meantime is the rudest thing a slow request can do.
        if (sameSelection(selectionNow.current, sel)) {
          setSkySelection({ kind: 'target', id: out.targetId })
          setSelected(out.targetId)
        }
      } catch (err) {
        // 409 and 422 both arrive here, and both mean the same thing to the
        // observer: the night is as it was, and this is why.
        refuse(err instanceof Error ? err.message : String(err))
      }
    }
    return { label: '+ Add to schedule', busy: adding !== null, onRun: () => void run() }
  }, [skySelection, sessionId, addTarget, adding, push, plan.data, slot, skyApi])

  const selectionNow = useRef<SkySelection | null>(null)
  useEffect(() => {
    selectionNow.current = skySelection
  }, [skySelection])

  const selectInSky = useCallback((sel: SkySelection | null) => {
    setSkySelection(sel)
    if (sel?.kind === 'target') setSelected(sel.id)
  }, [])

  if (presets.isPending) return <div className="empty">Loading presets…</div>
  if (presets.isError || !presets.data) {
    return <div className="empty">The server is not reachable. Is it running on :8000?</div>
  }

  const s = session.data
  const showSetup = !sessionId

  return (
    <div className="app">
      <div className="stage">
        {/*
          The sky is ALWAYS mounted, including behind the setup sheet. Pressing
          Create Schedule then raises the ground and drops the targets and the
          slew path into a sky you were already looking at, instead of
          replacing a form with a spinner. The preview has no ground, because
          until a session exists there is no site and no instant to stand on.
        */}
        {skyLost ? (
          <div className="empty">
            The sky needs WebGL, and this browser cannot give it. Everything else — the
            plan, the timeline, the panels — still works.
          </div>
        ) : (
          <Suspense fallback={<div className="empty">Building the sky…</div>}>
            <SkyScene
              session={s ?? null}
              geometry={geometry.data ?? null}
              plan={plan.data}
              sky={sky.data}
              satellites={satellites.data}
              catalog={catalog.data}
              selectedTarget={selected}
              selection={skySelection}
              onSelect={selectInSky}
              options={options}
              onLost={() => setSkyLost(true)}
              onReady={setSkyApi}
            />
          </Suspense>
        )}

        <TopBar
          session={s ?? null}
          api={skyApi}
          onNewNight={() => {
            setSessionId(null)
            setSkySelection(null)
          }}
          onChoose={selectInSky}
        />

        {/*
          Any re-plan that happens with the sky in view: adding an object, or
          re-folding against the current forecast. It is its own subscriber, so
          nothing here re-renders while it ticks.
        */}
        <FoldBanner />

        {s && !geometry.data && (
          <div className="banner chip">
            {s.status === 'building'
              ? `Folding the night — ${(s.progress * 100).toFixed(0)}% · ${s.message}`
              : (s.error ?? 'Waiting for the night geometry…')}
          </div>
        )}

        {/* Top left: all the weather and the sky it makes, and under it
            whatever was clicked. */}
        {s && !showSetup && (
          <div
            className={`dock dock--left${panelsHidden ? ' dock--dimmed' : ''}`}
            // `inert` rather than pointer-events alone: a dock faded to 12% is
            // still legible enough to aim at but not to read, so it must not be
            // clickable OR reachable by Tab. Without it, "hidden" panels keep
            // swallowing clicks meant for the sky behind them.
            inert={panelsHidden}
          >
            {plan.data && (
              <WeatherBox
                session={s}
                plan={plan.data}
                grid={grid.data}
                targetId={selected ?? plan.data.slots[slot]?.targetId ?? null}
              />
            )}
            {skySelection && skyApi && planAction && (
              <ObjectInfo
                api={skyApi}
                selection={skySelection}
                onClose={() => setSkySelection(null)}
                planAction={planAction}
                refreshKey={plan.data?.planId}
              />
            )}
          </div>
        )}

        {s && geometry.data && !showSetup && (
          <BottomBar
            session={s}
            options={options}
            onChange={setOptions}
            onZoom={(f) => skyApi?.zoomBy(f)}
            panelsHidden={panelsHidden}
            onTogglePanels={() => setPanelsHidden((v) => !v)}
          />
        )}

        {s && (
          <div
            className={`dock dock--right${panelsHidden ? ' dock--dimmed' : ''}`}
            inert={panelsHidden}
          >
            <NowCard
              session={s}
              plan={plan.data}
              slot={slot}
              pinnedTargetId={selected}
              onClearPin={() => setSelected(null)}
              alerts={alerts}
              onSeek={(t) => cursor.setT(t)}
            />
            {selected && geometry.data && (
              <Inspector
                session={s}
                geometryRow={
                  geometry.data.rows.find((r) => r.targetId === selected) ?? geometry.data.rows[0]!
                }
                target={s.targets.find((t) => t.id === selected)}
                plan={plan.data}
                grid={grid.data}
                slot={slot}
                onClose={() => setSelected(null)}
              />
            )}
            {plan.data && <Evidence session={s} plan={plan.data} />}
            <Activity
              session={s}
              activeIndex={decisionIndex}
              onSeek={(t) => cursor.setT(t)}
            />
            <Specs session={s} />
          </div>
        )}

        {showSetup && (
          <Setup
            presets={presets.data}
            busy={creating}
            error={createError}
            onCreate={(req) => void create(req)}
            onSiteChange={() => undefined}
          />
        )}
      </div>

      {s ? (
        <Nightbar session={s} plan={plan.data} markers={markers} onSeek={(t) => cursor.setT(t)} />
      ) : (
        <div style={{ height: NIGHTBAR_HEIGHT }} />
      )}

      <Toasts toasts={toasts} onDismiss={dismiss} />

      {/* The night did not change. Said in the one place that cannot be missed. */}
      {refusal && (
        <Modal title="Could not add to schedule" onClose={() => setRefusal(null)}>
          <p>{refusal}</p>
        </Modal>
      )}
    </div>
  )
}
