/**
 * The card for whatever was clicked in the sky, top left, the way Stellarium
 * Web shows its selection: a name, what it is, and the few numbers that say
 * where it is and how bright.
 *
 * The numbers come from the renderer's `describe`, which takes the backend's
 * value for the slot the cursor is in wherever one exists -- so this card and
 * the plan never disagree by a decimal. It refreshes on each slot crossing
 * and, while time runs, twice a second: a satellite's range and position
 * change by the second.
 */

import { useEffect, useState } from 'react'
import type { ObjectDescription, SkySelection } from '../sky/scene'
import type { SkyApi } from '../sky/SkyScene'
import { useCursorMode, useCursorSlot } from '../state/cursorStore'
import { FoldBar } from './FoldBar'

const KIND_ICON: Record<SkySelection['kind'], string> = {
  target: '◎',
  star: '✦',
  planet: '●',
  moon: '◐',
  satellite: '⟡',
  catalog: '◌',
}

/**
 * "Add to schedule" for the selection. The caller owns it, because deciding
 * what an add MEANS needs the plan, the night and the catalogue, none of which
 * this card has.
 *
 * It is never absent. Every object gets the same two actions, including the
 * planets and the satellites that can never be scheduled: the button then
 * explains why instead of adding, which is the answer to "can I shoot that?"
 * that a missing button never gives.
 */
export interface PlanAction {
  label: string
  busy: boolean
  onRun: () => void
  /** Already in the plan: the button states that instead of offering an add. */
  done?: boolean
}

export function ObjectInfo({
  api,
  selection,
  onClose,
  planAction,
  refreshKey,
}: {
  api: SkyApi
  selection: SkySelection
  onClose: () => void
  planAction: PlanAction
  /** Changes when the plan does, so "in tonight's plan" never goes stale. */
  refreshKey?: string
}) {
  const { slot } = useCursorSlot()
  const { playing, following: followingNow } = useCursorMode()
  // Following the present moves time as surely as playback does.
  const moving = playing || followingNow
  const [tick, setTick] = useState(0)
  const [desc, setDesc] = useState<ObjectDescription | null>(null)

  useEffect(() => {
    if (!moving) return
    const id = setInterval(() => setTick((n) => n + 1), 500)
    return () => clearInterval(id)
  }, [moving])

  useEffect(() => {
    setDesc(api.describe(selection))
  }, [api, selection, slot, tick, refreshKey])

  if (!desc) return null

  return (
    <section className="objinfo" aria-label={`About ${desc.title}`}>
      <header className="objinfo__head">
        <span className="objinfo__icon" aria-hidden>
          {KIND_ICON[selection.kind]}
        </span>
        <div className="objinfo__names">
          <h2 className="objinfo__title">{desc.title}</h2>
          <div className="objinfo__sub">{desc.subtitle}</div>
        </div>
        <button type="button" className="objinfo__close" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </header>

      <dl className="objinfo__rows">
        {desc.rows.map((r) => (
          <div key={r.label} className="objinfo__row" title={r.hint}>
            <dt>{r.label}</dt>
            <dd className="num">{r.value}</dd>
          </div>
        ))}
      </dl>
      {desc.note && <p className="objinfo__note">{desc.note}</p>}

      {/*
        Two actions: look at it, or observe it. Center went because zooming in
        centres on the way; Follow went because a two-button card was the ask,
        and the one object that is unwatchable without it -- a satellite, which
        leaves the frame in seconds -- gets followed by Zoom in itself.
      */}
      <div className="objinfo__actions">
        <button
          type="button"
          className="ghost"
          onClick={() =>
            api.flyTo(selection, {
              fovDeg: selection.kind === 'target' ? 3 : 12,
              follow: selection.kind === 'satellite',
            })
          }
          disabled={!desc.present}
        >
          Zoom in
        </button>
        <button
          type="button"
          className="primary"
          onClick={planAction.onRun}
          disabled={planAction.busy || !!planAction.done}
        >
          {planAction.busy ? 'Re-planning…' : planAction.label}
        </button>
      </div>
      {/*
        The re-plan runs for seconds. The banner over the sky carries the same
        wait for an observer who has since closed this card or clicked away;
        this copy is for the one still looking at what they asked for.
      */}
      {planAction.busy && <FoldBar label={`Adding ${desc.title} to the schedule`} />}
    </section>
  )
}
