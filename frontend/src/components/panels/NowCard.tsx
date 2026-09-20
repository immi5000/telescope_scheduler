/**
 * What do I do, right now.
 *
 * This is the instruction card, kept and rewritten. The old one was a report:
 * it told you a great deal that was true and left you to work out the action.
 * This one leads with the action and puts the evidence underneath.
 *
 * The ordering is the order of physical actions:
 *
 *   what to point at  →  what to watch for  →  where in the sky
 *                     →  the recipe         →  how far along
 *
 * Alerts come straight after the name. They used to sit below the recipe, on
 * the theory that by now the plan is accepted and a warning is something to
 * do DURING the block -- but in a scrolling dock "below the recipe" meant
 * below the fold, and nobody saw them. Each is one line; what to do about it
 * opens on click, so they cost a line each, not a paragraph.
 *
 * Only what you act on is visible. The decimal coordinates are under Details,
 * for the mount that wants them typed in; the numbers the scheduler reasons
 * with -- airmass, block efficiency, reference-minutes banked -- are not in
 * the card at all any more. The SNR meter stays: a goal is not bookkeeping.
 *
 * Coordinates have copy buttons, because nobody retypes `23h 37m 04.7s`
 * correctly at 2 a.m.
 */

import { useState } from 'react'
import type { Block, Plan, Session, TargetProgress } from '../../api/client'
import { AlertRow } from '../../alerts/AlertViews'
import { bySeverity, type Alert } from '../../alerts/model'
import { cursor, useCursorMode } from '../../state/cursorStore'
import { More, Panel } from '../Panel'
import '../../styles/schedule.css'

export function NowCard({
  session,
  plan,
  slot,
  pinnedTargetId,
  onClearPin,
  alerts,
  onSeek,
}: {
  session: Session
  plan: Plan | undefined
  slot: number
  pinnedTargetId: string | null
  onClearPin: () => void
  /** The whole night's alerts; the card shows only its own block's. */
  alerts: Alert[]
  /** Seeking from a schedule row, the way the activity log and the nightbar do. */
  onSeek: (tMs: number) => void
}) {
  const [showAll, setShowAll] = useState(false)
  const { live } = useCursorMode()

  if (!plan) {
    return (
      <Panel title="Right now" variant="now">
        <div className="dim">Waiting for the first plan.</div>
      </Panel>
    )
  }

  const assigned = plan.slots[slot]
  const shownId = pinnedTargetId ?? assigned?.targetId ?? null
  const block =
    plan.blocks.find((b) =>
      pinnedTargetId
        ? b.targetId === pinnedTargetId
        : slot >= b.slotStart && slot < b.slotEnd,
    ) ?? null
  const progress = plan.progress.find((p) => p.targetId === shownId) ?? null

  const pinned = pinnedTargetId ? describePin(plan, session, pinnedTargetId) : null

  if (!block) {
    return (
      <Panel
        title={pinnedTargetId ? 'Selected target' : 'Right now'}
        variant="now"
        right={pinnedTargetId ? <PinBadge onClear={onClearPin} /> : undefined}
      >
        {/*
          Two different silences, and they had the same words. "Nothing
          scheduled" under a pin used to be a report on the CURRENT slot while
          the observer was asking about the object they had just clicked --
          and the schedule list underneath would be highlighting a block in
          force at that very moment, flatly contradicting it.
        */}
        {pinned ? (
          <>
            <div className="now__target">{pinned.name}</div>
            <div className="dim">{pinned.why}</div>
          </>
        ) : (
          <>
            <div className="dim">Nothing scheduled.</div>
            {assigned?.kind === 'idle' && (
              <More label="Why">
                No target clears the altitude floor and the lunar exclusion at the same time.
              </More>
            )}
          </>
        )}
        <FullSchedule
          plan={plan}
          slot={slot}
          live={live}
          open={showAll}
          onOpen={setShowAll}
          onSeek={onSeek}
        />
      </Panel>
    )
  }

  const mine = alerts
    .filter((a) => a.block?.targetId === block.targetId && a.block.slotStart === block.slotStart)
    .sort(bySeverity)

  return (
    <Panel
      title={pinnedTargetId ? 'Selected target' : 'Right now'}
      variant="now"
      right={pinnedTargetId ? <PinBadge onClear={onClearPin} /> : <Clock block={block} />}
    >
      <div className="now__target">{block.name}</div>
      {mine.length > 0 && (
        <div className="alerts">
          {mine.map((a) => (
            <AlertRow key={a.id} alert={a} />
          ))}
        </div>
      )}
      <Instruction block={block} slot={slot} session={session} />

      <div className="now__coords">
        <span className="now__coord">{block.ra}</span>
        <Copy value={block.ra} label="RA" />
        <span className="now__coord">{block.dec}</span>
        <Copy value={block.dec} label="Dec" />
      </div>

      <Recipe block={block} />
      {progress && <Progress progress={progress} />}

      <FullSchedule
        plan={plan}
        slot={slot}
        live={live}
        open={showAll}
        onOpen={setShowAll}
        onSeek={onSeek}
      />

      {/*
        Airmass, block efficiency and reference-minutes used to sit here. They
        came out at the request of the design pass: they are how the scheduler
        reasons, not anything the observer does at the eyepiece. The SNR meter
        above stays, because that is a goal rather than bookkeeping.
      */}
      <More>
        <dl className="kv">
          <dt>Decimal</dt>
          <dd className="mono">
            {block.raDeg.toFixed(4)}, {block.decDeg.toFixed(4)}{' '}
            <Copy value={`${block.raDeg.toFixed(5)} ${block.decDeg.toFixed(5)}`} label="deg" />
          </dd>
        </dl>
      </More>
    </Panel>
  )
}

/** What a pinned target is, when the plan gives it no block to describe it. */
function describePin(
  plan: Plan,
  session: Session,
  targetId: string,
): { name: string; why: string } {
  const dropped = plan.dropped.find((d) => d.targetId === targetId)
  if (dropped) return { name: dropped.name, why: dropped.message }
  const target = session.targets.find((t) => t.id === targetId)
  return {
    name: target?.name ?? 'This target',
    why: 'Not in tonight’s plan from here on.',
  }
}

/**
 * The whole night, under the block you are on.
 *
 * One row per BLOCK in time order, not one per target: a target split across
 * two sittings is two rows, because the observer's question is "what am I
 * doing at 01:00" and a 130-minute total for M31 does not answer it. The cost
 * is that a split target's name appears twice, which is the truth.
 *
 * The duration is wall clock -- the slew is part of what the block costs you --
 * and the exposure count is what the plan asks of that block. Hovering says
 * how the minutes divide.
 *
 * Clicking a row seeks there, like the activity log and the nightbar -- but
 * only where the cursor can actually go. On a night still happening the store
 * clamps every seek to the live edge AND takes a seek past it as "back to
 * now", so a clickable 03:00 row would have yanked an observer reviewing
 * 22:10 forward to the wall clock and resumed following, which is the
 * opposite of what clicking a future row looks like it should do. Those rows
 * are shown -- the plan ahead is the point of the list -- and are not
 * controls.
 *
 * The fold's open state is owned by the card, not by this <details>: the card
 * returns a different tree for a block, for a gap and for a pin, so an
 * uncontrolled fold closed itself every time the cursor crossed a gap.
 */
function FullSchedule({
  plan,
  slot,
  live,
  open,
  onOpen,
  onSeek,
}: {
  plan: Plan
  slot: number
  /** A night still happening: the cursor cannot go past the wall clock. */
  live: boolean
  open: boolean
  onOpen: (open: boolean) => void
  onSeek: (tMs: number) => void
}) {
  // Solver order is not guaranteed to be night order, and a list that reads
  // out of sequence is worse than no list.
  const blocks = [...plan.blocks].sort((a, b) => a.slotStart - b.slotStart)
  if (blocks.length === 0) return null
  const onTarget = blocks.reduce((total, b) => total + b.integratingMinutes, 0)
  const edgeMs = cursor.live.edgeMs

  return (
    <details className="more" open={open} onToggle={(e) => onOpen(e.currentTarget.open)}>
      <summary>Show full schedule</summary>
      <div className="more__body">
        <div className="sched">
          <div className="sched__total">
            {blocks.length} block{blocks.length === 1 ? '' : 's'} &middot;{' '}
            {(onTarget / 60).toFixed(1)} h on target &middot; times UTC
          </div>
          {blocks.map((b) => {
            const here = slot >= b.slotStart && slot < b.slotEnd
            const startMs = Date.parse(b.startsAt)
            const reachable = !live || startMs <= edgeMs
            const minutes = Math.round((Date.parse(b.endsAt) - startMs) / 60_000)
            const split = `${b.switchMinutes.toFixed(0)} min slew, then ${b.integratingMinutes.toFixed(0)} min integrating`
            const cells = (
              <>
                <span className="sched__time num">
                  {b.startsAt.slice(11, 16)}&ndash;{b.endsAt.slice(11, 16)}
                </span>
                <span className="sched__name">{b.name}</span>
                <span className="sched__subs num">
                  {b.nSubs} &times; {b.tSubS.toFixed(0)} s
                </span>
                <span className="sched__min num">{minutes} min</span>
              </>
            )
            const cls = `sched__row${here ? ' sched__row--now' : ''}`
            return reachable ? (
              <button
                key={`${b.targetId}-${b.slotStart}`}
                type="button"
                className={cls}
                aria-current={here ? 'true' : undefined}
                title={split}
                onClick={() => onSeek(startMs)}
              >
                {cells}
              </button>
            ) : (
              <div
                key={`${b.targetId}-${b.slotStart}`}
                className={`${cls} sched__row--ahead`}
                title={`${split} — the night has not reached this block yet`}
              >
                {cells}
              </div>
            )
          })}
        </div>
      </div>
    </details>
  )
}

function Clock({ block }: { block: Block }) {
  return (
    <span className="chip num">
      {block.startsAt.slice(11, 16)}&ndash;{block.endsAt.slice(11, 16)}
    </span>
  )
}

function PinBadge({ onClear }: { onClear: () => void }) {
  return (
    <button type="button" className="chip" onClick={onClear} title="Back to the current block">
      pinned &times;
    </button>
  )
}

/**
 * The sentence that replaces reading four numbers off a table.
 *
 * Alt/az and a compass word, because RA/Dec is for the machine and alt/az is
 * for the person: "look east-north-east, two thirds of the way up" is
 * something a body can act on.
 */
function Instruction({
  block,
  slot,
  session,
}: {
  block: Block
  slot: number
  session: Session
}) {
  const rising = block.altitudeEndDeg > block.altitudeStartDeg
  const inSwitch = slot < block.slotStart + Math.round(block.switchMinutes / session.grid.slotMinutes)
  return (
    <div className="now__do">
      {inSwitch ? (
        <>
          <strong>Slew and settle</strong> &middot; shutter closed{' '}
          {block.switchMinutes.toFixed(0)} min
        </>
      ) : (
        <>
          <strong>Point {block.compass}</strong>, {Math.round(block.altitudeStartDeg)}&deg; up,{' '}
          {rising ? 'rising' : 'falling'} to {Math.round(block.altitudeEndDeg)}&deg;
        </>
      )}
    </div>
  )
}

function Recipe({ block }: { block: Block }) {
  return (
    <div className="row spread">
      <span className="num" style={{ fontSize: 14 }}>
        {block.nSubs} &times; {block.tSubS.toFixed(0)} s
      </span>
      <span className="faint num" style={{ fontSize: 11 }}>
        {block.integratingMinutes.toFixed(0)} min on target
      </span>
    </div>
  )
}

/**
 * Progress against the goal, in SNR.
 *
 * `expectedSnr` is what this block alone delivers; blocks combine in
 * QUADRATURE, not linearly, so the meter shows the target's accumulated
 * fraction from the server rather than adding block SNRs together here.
 */
function Progress({ progress }: { progress: TargetProgress }) {
  const pct = Math.min(1, progress.fraction)
  return (
    <div>
      <div className="row spread" style={{ marginBottom: 4 }}>
        <span className="faint" style={{ fontSize: 11 }}>
          SNR {progress.expectedSnr.toFixed(0)} of {progress.snrGoal.toFixed(0)}
        </span>
        <span className="faint num" style={{ fontSize: 11 }}>
          {(pct * 100).toFixed(0)}%
        </span>
      </div>
      <div className="meter">
        <div className="meter__fill" style={{ width: `${pct * 100}%` }} />
      </div>
    </div>
  )
}

function Copy({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      className="copy"
      aria-label={`Copy ${label}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(
          () => {
            setDone(true)
            setTimeout(() => setDone(false), 1200)
          },
          () => undefined,
        )
      }}
    >
      {done ? 'copied' : label}
    </button>
  )
}
