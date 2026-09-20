/**
 * The clock in the corner, and how fast the night runs.
 *
 * Two different controls for two different nights. A night that is over is a
 * replay, and gets speeds: real time, because at 1x the satellites cross the
 * sky at their true pace, and faster ones for watching the stars wheel. A
 * night that is still happening gets no speeds at all -- only LIVE, which
 * pins the clock to the present. There is nothing ahead of the present to
 * fast-forward into; see the live edge in `cursorStore`.
 *
 * The time text is written straight to the DOM on the frame channel, like the
 * nightbar's handle: sixty renders a second of a React component to change a
 * seconds digit would be the most expensive clock on the page.
 */

import { useEffect, useRef } from 'react'
import type { Session } from '../api/client'
import { cursor, useCursorMode } from '../state/cursorStore'
import '../styles/live.css'

const RATES = [
  { rate: 1, label: '1×', hint: 'Real time — satellites move at their true speed' },
  { rate: 10, label: '10×', hint: 'Ten times real time' },
  { rate: 60, label: '1 min/s', hint: 'One minute of night per second' },
  { rate: 600, label: '10 min/s', hint: 'Ten minutes of night per second' },
] as const

export function TimeBox({ session }: { session: Session }) {
  const { playing, rate, live, following } = useCursorMode()
  const timeRef = useRef<HTMLDivElement | null>(null)
  const dateRef = useRef<HTMLDivElement | null>(null)
  const startsIn = live && Date.parse(session.grid.start) > Date.now()

  useEffect(() => {
    const paint = () => {
      const d = new Date(cursor.live.tMs)
      if (timeRef.current) {
        timeRef.current.textContent = d.toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
      }
      if (dateRef.current) {
        dateRef.current.textContent = `${d.toLocaleDateString([], {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
        })} · ${d.toISOString().slice(11, 16)} UTC`
      }
    }
    paint()
    return cursor.subscribeFrame(paint)
  }, [])

  return (
    <div className="timebox">
      {live ? (
        <div className="timebox__rates" role="group" aria-label="Time">
          <button
            type="button"
            className="timebox__live"
            aria-pressed={following}
            title={
              startsIn
                ? 'The night has not started. The clock waits at dusk and runs from there in real time.'
                : following
                  ? 'Showing the present, in real time. Drag the night bar back to review what has happened tonight.'
                  : 'Reviewing earlier tonight. Click to return to the present.'
            }
            onClick={() => cursor.goLive()}
          >
            <span className="live-dot" aria-hidden />
            {following ? 'Live' : 'Back to live'}
          </button>
        </div>
      ) : (
        <div className="timebox__rates" role="group" aria-label="Speed">
          <button
            type="button"
            className="timebox__play"
            aria-label={playing ? 'Pause' : 'Play'}
            onClick={() => cursor.togglePlay()}
          >
            {playing ? (
              <svg viewBox="0 0 24 24" aria-hidden>
                <path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" fill="currentColor" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" aria-hidden>
                <path d="M7 4.5v15l12-7.5z" fill="currentColor" />
              </svg>
            )}
          </button>
          {RATES.map((r) => (
            <button
              key={r.rate}
              type="button"
              className="timebox__rate"
              aria-pressed={rate === r.rate}
              title={r.hint}
              onClick={() => cursor.setMode({ rate: r.rate, playing: true })}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}
      <div className="timebox__clock" title="Your computer's local time; UTC below">
        <div ref={timeRef} className="timebox__time num" />
        <div ref={dateRef} className="timebox__date num" />
      </div>
    </div>
  )
}
