/**
 * What happened, in reverse order, and what it changed.
 *
 * Doubles as the nightbar's marker legend: the markers carry no labels, so
 * this is where "what is that amber dot at 01:40" gets answered. Clicking
 * seeks there.
 */

import { useState } from 'react'
import { ApiError, type Session } from '../../api/client'
import { useRefreshNight } from '../../hooks'
import { AlertIcon } from '../../alerts/AlertViews'
import { changeSummary, changed, decisionCategory, targetNames } from '../../alerts/model'
import { More, Panel } from '../Panel'
import '../../styles/live.css'

export function Activity({
  session,
  activeIndex,
  onSeek,
}: {
  session: Session
  activeIndex: number
  onSeek: (tMs: number) => void
}) {
  const names = targetNames(session)
  const points = [...session.decisionPoints].reverse()
  return (
    <Panel
      title="Activity"
      right={<span className="chip">{session.decisionPoints.length}</span>}
    >
      <Replan session={session} />
      <div className="log">
        {points.map((dp) => {
          const moved = changed(dp)
          const category = decisionCategory(dp)
          const label =
            dp.index === 0
              ? 'Opening plan'
              : dp.kind === 'amend'
                ? 'You'
                : category === 'weather'
                  ? 'Forecast'
                  : 'Update'
          return (
            <button
              type="button"
              key={dp.index}
              title={dp.reason}
              className={[
                'log__item',
                dp.index === activeIndex ? 'log__item--active' : '',
                dp.index > 0 && !moved ? 'log__item--quiet' : '',
              ].join(' ')}
              onClick={() => onSeek(Date.parse(dp.at))}
            >
              <span className="log__time">{dp.at.slice(11, 16)}</span>
              <span className="log__icon">
                <AlertIcon category={category} size={13} />
              </span>
              <span>
                {label}
                {dp.index > 0 && (
                  <span className="faint"> · {moved ? changeSummary(dp, names) : 'no change'}</span>
                )}
              </span>
            </button>
          )
        })}
      </div>
    </Panel>
  )
}

/**
 * The server's watch over a night that is still happening, stated so it can
 * be checked rather than trusted: when it last asked for new data, when it
 * asks next, and what it found. "Check now" asks immediately.
 */
/**
 * Re-folding the night, and the plain statement that nothing else will.
 *
 * The server used to watch a night that had not ended and append a decision
 * point when a new forecast run landed. A host that forgets the night between
 * requests has nothing to watch it with, so no watch exists.
 *
 * Two things follow, and both are said rather than implied. The panel does
 * not claim to be watching, because it is not. And the control stays put
 * instead of disappearing with the watch that used to own it -- re-planning
 * is now the ONLY way to get current data into this night, which is a reason
 * to make it reachable, not a reason to hide it.
 */
function Replan({ session }: { session: Session }) {
  const refresh = useRefreshNight(session.id)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  const replan = async () => {
    setBusy(true)
    setNote(null)
    try {
      await refresh()
      setNote(`Re-planned at ${new Date().toISOString().slice(11, 16)} against the current forecast.`)
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="livewatch">
        <span className="num">Not watched · re-plan to pick up new data</span>
        <button
          type="button"
          className="livewatch__check"
          disabled={busy}
          onClick={() => void replan()}
        >
          {busy ? 'Re-planning…' : 'Re-plan now'}
        </button>
      </div>
      <More label="Why this is manual">
        <div>
          Nothing is folding this night in the background. Each plan was built
          from the forecast as it stood when the night was folded; re-planning
          folds it again against whatever the forecast says now.
        </div>
        {note && <div className="faint">{note}</div>}
      </More>
    </>
  )
}
