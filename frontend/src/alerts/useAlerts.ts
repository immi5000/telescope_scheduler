import { useEffect, useMemo, useRef } from 'react'
import type { Plan, Session } from '../api/client'
import type { components } from '../api/schema'
import type { Toast } from '../components/Toasts'
import { useSatellites } from '../hooks'
import { cursor } from '../state/cursorStore'
import {
  CATEGORY,
  crossingAlerts,
  hazardAlerts,
  liveUntil,
  newsAlerts,
  pops,
  type Alert,
} from './model'

type Geometry = components['schemas']['GeometryOut']

/** Every alert for the night under the plan in force, in time order. */
export function useAlerts(
  session: Session | undefined,
  plan: Plan | undefined,
  geometry: Geometry | undefined,
): Alert[] {
  // Shared with the sky renderer's query: one request serves both.
  const sats = useSatellites(session?.id ?? null)
  return useMemo(() => {
    if (!session) return []
    const out = newsAlerts(session, plan)
    if (plan) {
      out.push(...hazardAlerts(session, plan))
      if (geometry) out.push(...crossingAlerts(session, plan, geometry, sats.data))
    }
    return out.sort((a, b) => a.tMs - b.tMs)
  }, [session, plan, geometry, sats.data])
}

/**
 * Pop an alert when the cursor reaches it.
 *
 * Tied to the CURSOR, not to when the data arrived. The fold computes the
 * whole night in a few seconds, so "on arrival" meant a burst of toasts about
 * 01:40 while the cursor sat at dusk: news from the future. Reaching it is
 * what an observer at the telescope would actually have experienced.
 *
 * An alert pops once. Moving the cursor back before it re-arms it, so
 * replaying a stretch of night replays its alerts. Nothing pops mid-drag --
 * sweeping across five hours would be a flood, not an alert -- and on release
 * only what is current where the cursor landed pops.
 *
 * Nothing pops while the cursor is outside the window of the plan the alerts
 * were built from. Jumping across a decision point leaves the previous plan
 * in hand until the next one loads, and popping then announces the OLD plan's
 * hazards at the new moment -- observed: a cloud warning for a block the new
 * plan had just cut short. The test is on the plan's own validity window and
 * the live cursor, not on React state, because the cursor notifies
 * synchronously inside `setT`, a render before any state could catch up.
 */
export function useAlertToasts(
  alerts: Alert[],
  push: (t: Omit<Toast, 'id'> & { id?: string }) => void,
  plan: Plan | undefined,
): void {
  const fired = useRef(new Set<string>())
  const from = plan ? Date.parse(plan.validFrom) : NaN
  const until = plan ? Date.parse(plan.validUntil) : NaN

  useEffect(() => {
    const check = () => {
      if (cursor.live.dragging) return
      const t = cursor.live.tMs
      // The last plan runs to the end of the night, where the cursor clamps.
      const last = until >= (cursor.getClock()?.endMs ?? Infinity)
      if (!(t >= from && (t < until || last))) return
      for (const a of alerts) {
        if (t < a.tMs) {
          fired.current.delete(a.id)
          continue
        }
        if (fired.current.has(a.id) || t >= liveUntil(a) || !pops(a)) continue
        fired.current.add(a.id)
        push(toToast(a))
      }
    }
    check()
    const offFrame = cursor.subscribeFrame(check)
    const offMode = cursor.subscribeMode(check)
    return () => {
      offFrame()
      offMode()
    }
  }, [alerts, push, from, until])
}

function toToast(a: Alert): Omit<Toast, 'id'> & { id: string } {
  const seek = a.seekMs
  return {
    id: a.id,
    title: a.title,
    body: a.summary,
    severity: a.severity,
    category: a.category,
    categoryLabel: CATEGORY[a.category].label,
    when: new Date(a.tMs).toISOString().slice(11, 16),
    // Pauses as well as seeking: during playback the moment is gone a second
    // after the toast appears, and "show me" means "hold it there".
    action:
      seek !== undefined
        ? {
            label: 'Show me',
            run: () => {
              cursor.setMode({ playing: false })
              cursor.setT(seek)
            },
          }
        : undefined,
  }
}
