/**
 * The view's field of view and follow state, for the chrome around the sky.
 *
 * A tiny external store rather than React state in the app: the field changes
 * on every wheel tick, and routing that through the app root would re-render
 * every panel on the page to update three characters in the top bar. Only the
 * components that subscribe re-render, and only when the value they show moves.
 */

import { useSyncExternalStore } from 'react'

interface ViewSnapshot {
  fovDeg: number
  following: boolean
}

let snapshot: ViewSnapshot = { fovDeg: 120, following: false }
const listeners = new Set<() => void>()

export function publishView(fovDeg: number, following: boolean): void {
  // Quantised to what the readout can show, so a slow zoom is not 60 renders a second.
  const f = fovDeg >= 10 ? Math.round(fovDeg) : Math.round(fovDeg * 10) / 10
  if (f === snapshot.fovDeg && following === snapshot.following) return
  snapshot = { fovDeg: f, following }
  for (const l of listeners) l()
}

const subscribe = (l: () => void) => {
  listeners.add(l)
  return () => listeners.delete(l)
}
const get = () => snapshot

export function useSkyView(): ViewSnapshot {
  return useSyncExternalStore(subscribe, get, get)
}
