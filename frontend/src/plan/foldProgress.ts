/**
 * How far along the fold is, for the waits long enough to need saying.
 *
 * All of them are CP-SAT and all take seconds: planning the night when it is
 * first created, re-planning it around an object added during it, and folding
 * it again against the current forecast. The server reports each stage as it
 * finishes one (`POST /api/night/stream`), and this holds the latest report so
 * the bar can be drawn anywhere without the wait being threaded through half
 * the component tree.
 *
 * A MODULE STORE, not React state, for the same reason `state/cursorStore.ts`
 * is one: the bar ticks several times a second and the things it sits inside
 * -- the setup sheet, the object card -- are large and have no business
 * re-rendering for it. Only the bar subscribes.
 *
 * WHAT THE BAR IS ALLOWED TO CLAIM. Between two server reports it drifts
 * forward, because the gaps are uneven -- a cold forecast fetch is one stage
 * and so is a solve that lands in 40 ms -- and a bar frozen for six seconds
 * reads as a hang. The drift is bounded so it stays a statement about waiting
 * rather than a claim about progress: never more than `LEAD` ahead of what the
 * server actually said, never past `CEILING`, and it approaches its bound
 * asymptotically so it visibly slows rather than arriving. A real report snaps
 * it forward. Only the night's arrival fills it.
 */

import { useSyncExternalStore } from 'react'
import type { FoldStage } from '../api/client'

/**
 * Which wait this is. The wording differs; the bar does not.
 *
 * `create` is the first fold, behind the setup sheet. `add` is the re-plan
 * around a new object, and `refold` the one the Activity panel's "Re-plan now"
 * asks for -- both happen over the sky, with nothing in front of them, so both
 * get the banner.
 */
export type FoldKind = 'create' | 'add' | 'refold'

export interface FoldJob {
  /** Null when nothing is being waited for. */
  kind: FoldKind | null
  /** What is being added, for an add. Null otherwise. */
  subject: string | null
  /** 0..1, monotone within a job. What to draw. */
  fraction: number
  /** The stage, in the server's words. */
  message: string
}

const IDLE: FoldJob = { kind: null, subject: null, fraction: 0, message: '' }

/** How far ahead of the last thing the server said the bar may drift. */
const LEAD = 0.08
/** ...and the highest it may drift to. Only the night itself reaches 1. */
const CEILING = 0.96
/** Time constant of the drift: it covers ~63% of the remaining gap in this. */
const TAU_MS = 2500
/**
 * The redraw cadence. `.meter__fill` already eases width over 200 ms, so four
 * a second is continuous to the eye and costs four renders of one leaf.
 */
const TICK_MS = 250

type Listener = () => void

class FoldProgressStore {
  private job: FoldJob = IDLE
  /** The last fraction the SERVER reported. The drift is measured from here. */
  private reported = 0
  private shown = 0
  private timer: ReturnType<typeof setInterval> | null = null
  private last = 0
  private listeners = new Set<Listener>()

  /**
   * Begin a wait, and hand back the sink for `api.nightStream`.
   *
   * Starting a second wait replaces the first rather than queueing: two folds
   * cannot be outstanding at once (creating replaces the night, and the add
   * button is disabled while one is running), so a leftover job would only
   * ever be one that failed to end.
   */
  start(kind: FoldKind, subject: string | null, message: string): (stage: FoldStage) => void {
    this.reported = 0
    this.shown = 0
    this.job = { kind, subject, fraction: 0, message }
    this.last = now()
    if (this.timer === null) this.timer = setInterval(() => this.drift(), TICK_MS)
    this.publish()
    return (stage) => this.report(stage)
  }

  /** The wait is over, however it ended. */
  finish(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
    this.job = IDLE
    this.reported = 0
    this.shown = 0
    this.publish()
  }

  private report(stage: FoldStage): void {
    if (this.job.kind === null) return
    // Monotone: a stage that reports less than the drift has already shown
    // must not walk the bar backwards.
    this.reported = Math.max(this.reported, clamp01(stage.fraction))
    this.shown = Math.max(this.shown, this.reported)
    this.last = now()
    this.job = { ...this.job, fraction: this.shown, message: stage.message || this.job.message }
    this.publish()
  }

  private drift(): void {
    if (this.job.kind === null) return
    const t = now()
    const dt = t - this.last
    this.last = t
    const target = Math.min(this.reported + LEAD, CEILING)
    if (this.shown >= target) return
    this.shown += (target - this.shown) * (1 - Math.exp(-dt / TAU_MS))
    this.job = { ...this.job, fraction: this.shown }
    this.publish()
  }

  private publish(): void {
    for (const l of this.listeners) l()
  }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l)
    return () => this.listeners.delete(l)
  }

  getSnapshot = (): FoldJob => this.job
}

const now = (): number =>
  typeof performance === 'undefined' ? Date.now() : performance.now()

const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v)

export const foldProgress = new FoldProgressStore()

/** The wait in progress, redrawn as it advances. Idle when `kind` is null. */
export function useFoldProgress(): FoldJob {
  return useSyncExternalStore(
    foldProgress.subscribe,
    foldProgress.getSnapshot,
    foldProgress.getSnapshot,
  )
}
