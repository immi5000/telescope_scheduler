/**
 * What happened, in reverse order, and what it changed.
 *
 * Doubles as the nightbar's marker legend: the markers carry no labels, so
 * this is where "what is that amber dot at 01:40" gets answered. Clicking
 * seeks there.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api, type Session } from '../../api/client'
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
      {session.live && <LiveWatch session={session} live={session.live} />}
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

const hhmm = (iso: string | null | undefined): string => (iso ? iso.slice(11, 16) : '—')

/**
 * The server's watch over a night that is still happening, stated so it can
 * be checked rather than trusted: when it last asked for new data, when it
 * asks next, and what it found. "Check now" asks immediately.
 */
function LiveWatch({ session, live }: { session: Session; live: NonNullable<Session['live']> }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  const check = async () => {
    setBusy(true)
    setNote(null)
    try {
      const next = await api.refresh(session.id)
      qc.setQueryData(['session', session.id], next)
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const headline = live.following
    ? `Watching · checked ${hhmm(live.lastCheckedAt)}${
        live.nextCheckAt ? ` · next ${hhmm(live.nextCheckAt)}` : ''
      }`
    : `Night over · ${live.updates} live update${live.updates === 1 ? '' : 's'}`

  return (
    <>
      <div className={`livewatch${live.lastError ? ' livewatch--error' : ''}`}>
        {live.following && <span className="live-dot" aria-hidden />}
        <span className="num">{headline}</span>
        {live.following && (
          <button
            type="button"
            className="livewatch__check"
            disabled={busy}
            onClick={() => void check()}
          >
            {busy ? 'Checking…' : 'Check now'}
          </button>
        )}
      </div>
      <More label={live.lastError ? 'Last check failed' : 'Last check'}>
        <div>{live.lastResult}</div>
        {live.lastError && <div className="faint">{live.lastError}</div>}
        {note && <div className="faint">{note}</div>}
        <div className="faint">
          {live.checks} check{live.checks === 1 ? '' : 's'}, {live.updates} brought new data
          {live.everyMinutes ? ` · every ${live.everyMinutes} min` : ' · scheduled checks off'}
        </div>
      </More>
    </>
  )
}
