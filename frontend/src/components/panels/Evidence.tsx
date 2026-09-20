/**
 * What changed, and what it cost.
 *
 * This panel used to carry the ledger as well: the plan's id, the count of
 * past slots rewritten, the solver's status, gap and milliseconds, how many
 * records were known and which run was newest. All of that is gone from the
 * interface by request -- it is how the scheduler reasons about itself, not
 * anything an observer does at the eyepiece, and a card of it beside the sky
 * made the app read as a telemetry console.
 *
 * None of it stopped being true. `pastSlotsRewritten` is still computed, still
 * in the API payload, and still asserted in tests/api/test_api.py and
 * test_amend.py; the replay's no-lookahead guarantee is enforced where it is
 * enforceable rather than where it is legible. What survives here is what the
 * night actually did: the sentence saying what moved, and which targets did
 * not make it and why.
 *
 * The oracle flag is the one piece of machinery still shown, and only ever
 * when it fires. It means the plan was built from data that did not exist
 * yet, which is not a diagnostic -- it means everything on screen is wrong.
 */

import type { Plan, Session } from '../../api/client'
import { More, Panel } from '../Panel'

export function Evidence({ session, plan }: { session: Session; plan: Plan }) {
  void session
  const ch = plan.changes
  const ev = plan.evidence

  return (
    <Panel
      title="What changed"
      right={
        plan.decisionCount > 1 ? (
          <span className="chip">
            {plan.decisionIndex + 1} of {plan.decisionCount}
          </span>
        ) : undefined
      }
    >
      <div style={{ fontSize: 12.5, lineHeight: 1.45 }}>{ch.headline}</div>

      {ev.hasOracle && (
        <div className="row">
          <span className="chip chip--crit">Built on data from the future — this is a bug</span>
        </div>
      )}

      {plan.dropped.length > 0 && (
        <More label={`${plan.dropped.length} not scheduled`}>
          {plan.dropped.map((d) => (
            <div key={d.targetId}>
              <span style={{ color: 'var(--ink)' }}>{d.name}</span>
              {' — '}
              {/* The server's own sentence, verbatim. This used to rewrite
                  "outranked" to "the night filled up first", because the
                  solver's message was a note to a developer. It now says which
                  of three things went wrong and quotes the figures behind it --
                  never rises, never up for long enough, or costs more than the
                  night has left -- and those differ in what the observer should
                  do about it, so rewriting them to one phrase threw away the
                  only part that was actionable. */}
              {d.message}
            </div>
          ))}
        </More>
      )}
    </Panel>
  )
}
