/**
 * The cursor, at three cadences, because it is read by three things that can
 * afford wildly different costs.
 *
 *   PER FRAME          the rig quaternion, the Moon, four uniforms, the labels.
 *                      A mutable ref. Zero React, zero allocation, < 4 ms.
 *
 *   PER SLOT CROSSING  sidebar numbers, marker states, `aria-valuetext`.
 *                      React, but quantised: dragging across a whole ten-hour
 *                      night is ~120 renders, not one per pointermove.
 *
 *   PER DECISION POINT the plan and grid queries. Network, and usually a cache
 *                      hit, because the server already said the answer cannot
 *                      change until the next decision point.
 *
 * The old build put the cursor in zustand and re-rendered the entire app on
 * every `pointermove`. Quantising beats `useDeferredValue` here because it is
 * deterministic and bounded, where deferring is neither -- under load React
 * decides how much to drop, and "how jumpy is the sidebar" becomes a function
 * of what else is on the page.
 *
 * **The slot channel must never appear in a query key.** That is precisely
 * what keeps scrubbing inside a decision interval free.
 *
 * THE LIVE EDGE. The cursor may never show an instant that has not happened.
 * For a night that is over, that bound is dawn and the whole night is a
 * replay: drag anywhere, play at any speed. For tonight, the bound is the
 * wall clock, and it moves. The cursor opens there and FOLLOWS it at real
 * time; dragging back reviews what has already happened tonight, and seeking
 * to or past the edge resumes following. There is no fast-forward, because
 * ahead of the edge there is nothing to show but the plan's own guess, and
 * presenting that as history is how a snapshot passes itself off as live.
 * When dawn arrives the edge reaches the end, the night is over, and the
 * replay controls unlock.
 */

import { useSyncExternalStore } from 'react'
import { containingSlot, slotMidMs, type SlotClock } from '../time/slots'

type Listener = () => void

/** The wall clock. The ONLY place in the frontend's time model that reads it. */
const wallNow = (): number => Date.now()

/**
 * Read this every frame. It is mutated in place and never replaced, so the
 * render loop holds one reference for the life of the scene and allocates
 * nothing per frame.
 */
export interface LiveCursor {
  /** Epoch ms. Continuous -- this is the one that moves smoothly. */
  tMs: number
  /** Slot index containing `tMs`. Kept in sync so the scene never recomputes it. */
  slot: number
  /** True while a pointer is down on the nightbar. Suppresses playback. */
  dragging: boolean
  /** Replay playback. Never true on a night that is still happening. */
  playing: boolean
  /** Playback rate, in night-seconds per real second. Replay only. */
  rate: number
  /** Index of the decision point in force. The network tier. */
  decision: number
  /**
   * The latest instant the cursor may show: dawn, or the wall clock if that
   * is earlier. Before dusk it sits before the night, and the cursor pins to
   * dusk, because none of the night has happened yet.
   */
  edgeMs: number
  /** Pinned to the wall clock, moving at real time. Tonight only. */
  following: boolean
}

export interface CursorMode {
  playing: boolean
  dragging: boolean
  rate: number
  following: boolean
  /** The night has not ended: the cursor is capped at the wall clock. */
  live: boolean
}

class CursorStore {
  readonly live: LiveCursor = {
    tMs: 0,
    slot: 0,
    dragging: false,
    playing: false,
    rate: 600,
    decision: 0,
    edgeMs: Infinity,
    following: false,
  }

  private clock: SlotClock | null = null
  /** Decision-point instants, ascending. Sorted once, bisected thereafter. */
  private decisionTimes: number[] = []
  private slotListeners = new Set<Listener>()
  private modeListeners = new Set<Listener>()
  private frameListeners = new Set<Listener>()
  private decisionListeners = new Set<Listener>()
  private edgeListeners = new Set<Listener>()
  /** The live loop: advances the edge, and the cursor with it while following. */
  private liveRaf = 0
  /** Whole seconds of the last edge notification, so listeners hear 1 Hz. */
  private edgeSecond = -1

  /** Snapshot identity for React. Bumped only when the SLOT changes. */
  private slotSnapshot = { slot: 0, tMs: 0 }
  private modeSnapshot: CursorMode = {
    playing: false,
    dragging: false,
    rate: 600,
    following: false,
    live: false,
  }
  private decisionSnapshot = { index: 0, count: 0 }

  setClock(clock: SlotClock | null): void {
    const prev = this.clock
    if (
      prev &&
      clock &&
      prev.startMs === clock.startMs &&
      prev.slotMs === clock.slotMs &&
      prev.nSlots === clock.nSlots
    ) {
      // The same night re-delivered -- every live refresh re-sends the
      // session. Resetting here would yank a reviewing observer back to dusk
      // (or to now) each time the server checked for news.
      return
    }
    this.stopLiveLoop()
    this.clock = clock
    if (!clock) return
    this.live.edgeMs = Math.min(clock.endMs, wallNow())
    this.live.playing = false
    if (this.isLive) {
      // Tonight opens on the present and follows it. Opening at dusk, as a
      // replay does, would start the observer hours in the past with a play
      // button that runs toward instants that have not happened.
      this.live.rate = 1
      this.live.following = true
      this.place(this.live.edgeMs, true)
      this.startLiveLoop()
    } else {
      // A night that is over starts at dusk rather than wherever the last one
      // left the cursor, which would otherwise be an instant outside it.
      this.live.following = false
      this.place(slotMidMs(clock, 0), true)
    }
    this.publishMode()
  }

  getClock(): SlotClock | null {
    return this.clock
  }

  /** The night has not ended yet, so the wall clock caps the cursor. */
  get isLive(): boolean {
    const c = this.clock
    return !!c && this.live.edgeMs < c.endMs
  }

  /**
   * The instants at which the plan can change.
   *
   * Held here rather than derived in a component because the decision index is
   * the NETWORK tier of the cursor, and it must change the moment the cursor
   * crosses a publication instant -- not when it next crosses a slot boundary.
   * Deriving it from the slot channel instead would quantise it to the grid,
   * so a run published mid-slot would appear up to five minutes late, or (if
   * quantised to the midpoint) up to 150 seconds EARLY, which is worse: the
   * plan would briefly be one the observer could not yet have been given.
   */
  setDecisionTimes(times: number[]): void {
    this.decisionTimes = [...times].sort((a, b) => a - b)
    this.recomputeDecision(true)
  }

  private recomputeDecision(force = false): void {
    const times = this.decisionTimes
    const t = this.live.tMs
    let lo = 0
    let hi = times.length - 1
    let best = 0
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if ((times[mid] ?? Infinity) <= t) {
        best = mid
        lo = mid + 1
      } else {
        hi = mid - 1
      }
    }
    if (best === this.live.decision && !force) return
    this.live.decision = best
    this.decisionSnapshot = { index: best, count: times.length }
    for (const l of this.decisionListeners) l()
  }

  /**
   * A SEEK: someone asked for this instant.
   *
   * On a live night it is clamped to the edge, and it also decides whether
   * the cursor keeps following: a seek to or past the edge means "now", so
   * following resumes; anything earlier is a review and stops it. That is
   * what makes dragging to the right end of the nightbar the way back to live.
   */
  setT(tMs: number, opts?: { force?: boolean }): void {
    if (!this.clock) return
    if (this.isLive) {
      this.advanceEdge()
      this.setFollowing(tMs >= this.live.edgeMs)
    }
    this.place(tMs, opts?.force)
  }

  /**
   * Move the cursor, clamped to what may be shown. Internal: it never touches
   * `following`, so the live loop can call it every frame without undoing a
   * seek that happened in between.
   */
  private place(tMs: number, force?: boolean): void {
    const c = this.clock
    if (!c) return
    const hi = Math.max(c.startMs, Math.min(c.endMs, this.live.edgeMs))
    const t = Math.max(c.startMs, Math.min(hi, tMs))
    this.live.tMs = t
    const slot = containingSlot(c, t)
    const changed = slot !== this.live.slot
    this.live.slot = slot
    this.emitFrame()
    this.recomputeDecision()
    if (changed || force) {
      // A new object, because `useSyncExternalStore` compares by identity.
      this.slotSnapshot = { slot, tMs: slotMidMs(c, slot) }
      for (const l of this.slotListeners) l()
    }
  }

  nudgeSlots(delta: number): void {
    const c = this.clock
    if (!c) return
    const next = Math.max(0, Math.min(c.nSlots - 1, this.live.slot + delta))
    this.setT(slotMidMs(c, next))
  }

  /**
   * Replay playback controls. On a live night there is nothing ahead of the
   * edge to play into, so "play" means "back to now" and a rate is refused:
   * fast-forward exists only for nights that are over.
   */
  setMode(patch: Partial<Pick<LiveCursor, 'playing' | 'dragging' | 'rate'>>): void {
    const p = { ...patch }
    if (this.isLive) {
      if (p.playing) this.goLive()
      delete p.playing
      delete p.rate
    }
    let changed = false
    for (const [k, v] of Object.entries(p) as [keyof typeof p, never][]) {
      if (this.live[k] !== v) {
        this.live[k] = v
        changed = true
      }
    }
    if (!changed) return
    this.publishMode()
    this.emitFrame()
  }

  /** Jump to the present and follow it. A no-op once the night is over. */
  goLive(): void {
    if (!this.isLive) return
    this.advanceEdge()
    this.setFollowing(true)
    this.place(this.live.edgeMs)
  }

  /** Stop where the cursor is. On a live night the edge keeps moving on. */
  pause(): void {
    if (this.isLive) this.setFollowing(false)
    else this.setMode({ playing: false })
  }

  /** The space bar and the transport button: pause, or resume what pause stopped. */
  togglePlay(): void {
    if (this.isLive) {
      if (this.live.following) this.pause()
      else this.goLive()
    } else {
      this.setMode({ playing: !this.live.playing })
    }
  }

  private setFollowing(on: boolean): void {
    if (this.live.following === on) return
    this.live.following = on
    this.publishMode()
  }

  private publishMode(): void {
    this.modeSnapshot = {
      playing: this.live.playing,
      dragging: this.live.dragging,
      rate: this.live.rate,
      following: this.live.following,
      live: this.isLive,
    }
    for (const l of this.modeListeners) l()
  }

  // -- the live edge -------------------------------------------------------

  /** Re-read the wall clock. Returns false once the night is over. */
  private advanceEdge(): boolean {
    const c = this.clock
    if (!c) return false
    this.live.edgeMs = Math.min(c.endMs, wallNow())
    const second = Math.floor(this.live.edgeMs / 1000)
    if (second !== this.edgeSecond) {
      this.edgeSecond = second
      for (const l of this.edgeListeners) l()
    }
    return this.live.edgeMs < c.endMs
  }

  /**
   * One rAF loop for the life of a live night.
   *
   * While following it places the cursor on the edge every frame -- real-time
   * motion, so a satellite crosses at its true pace rather than in one-second
   * hops. While reviewing it only advances the edge. At dawn it stops, and the
   * mode flips to replay so the speed controls appear.
   */
  private startLiveLoop(): void {
    const tick = () => {
      this.liveRaf = 0
      const stillLive = this.advanceEdge()
      if (this.live.following && !this.live.dragging) this.place(this.live.edgeMs)
      if (!stillLive) {
        this.live.following = false
        this.publishMode()
        return
      }
      this.liveRaf = requestAnimationFrame(tick)
    }
    this.liveRaf = requestAnimationFrame(tick)
  }

  private stopLiveLoop(): void {
    if (this.liveRaf) cancelAnimationFrame(this.liveRaf)
    this.liveRaf = 0
  }

  /** The edge moved (at most once a second). For the nightbar's "not yet" veil. */
  subscribeEdge(l: Listener): () => void {
    this.edgeListeners.add(l)
    return () => this.edgeListeners.delete(l)
  }

  /**
   * Anything that must repaint when the cursor moves at all.
   *
   * The scene subscribes so it can WAKE its idle loop. It does not subscribe to
   * get the value -- it reads `live` directly.
   */
  subscribeFrame(l: Listener): () => void {
    this.frameListeners.add(l)
    return () => this.frameListeners.delete(l)
  }

  private emitFrame(): void {
    for (const l of this.frameListeners) l()
  }

  subscribeSlot = (l: Listener): (() => void) => {
    this.slotListeners.add(l)
    return () => this.slotListeners.delete(l)
  }

  getSlotSnapshot = (): { slot: number; tMs: number } => this.slotSnapshot

  subscribeMode = (l: Listener): (() => void) => {
    this.modeListeners.add(l)
    return () => this.modeListeners.delete(l)
  }

  getModeSnapshot = (): typeof this.modeSnapshot => this.modeSnapshot

  subscribeDecision = (l: Listener): (() => void) => {
    this.decisionListeners.add(l)
    return () => this.decisionListeners.delete(l)
  }

  getDecisionSnapshot = (): typeof this.decisionSnapshot => this.decisionSnapshot
}

export const cursor = new CursorStore()

/**
 * The slot the cursor is in, and that slot's midpoint.
 *
 * Re-renders on slot CROSSINGS only. The returned `tMs` is the slot's midpoint,
 * not the raw cursor: displayed numbers should agree with the slot they are
 * labelled with, and the midpoint is the instant the backend actually
 * evaluated.
 */
export function useCursorSlot(): { slot: number; tMs: number } {
  return useSyncExternalStore(cursor.subscribeSlot, cursor.getSlotSnapshot, cursor.getSlotSnapshot)
}

/**
 * The decision point in force, and how many there are.
 *
 * Re-renders only when the cursor crosses a publication instant -- a handful
 * of times across a whole night. This is the channel the plan and grid queries
 * are keyed on, which is what makes scrubbing inside one interval free.
 */
export function useDecisionIndex(): { index: number; count: number } {
  return useSyncExternalStore(
    cursor.subscribeDecision,
    cursor.getDecisionSnapshot,
    cursor.getDecisionSnapshot,
  )
}

export function useCursorMode(): CursorMode {
  return useSyncExternalStore(cursor.subscribeMode, cursor.getModeSnapshot, cursor.getModeSnapshot)
}
