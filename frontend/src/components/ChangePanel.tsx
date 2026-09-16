import type { Plan, Session } from "../api/client";

/**
 * What changed, why, and on what evidence.
 *
 * Every line here cites a real record. The explanation is generated without a
 * language model: because the quality score is a product of named factors, the
 * change between two grids is an exact attribution rather than a plausible
 * story, and the evidence ledger says precisely which forecast run was known at
 * the time.
 *
 * `pastSlotsRewritten` is displayed rather than merely asserted in a test. It
 * must always be zero, and the observer is entitled to watch it stay zero while
 * they scrub -- a guarantee nobody can see is a guarantee nobody trusts.
 */
export function ChangePanel({
  session,
  plan,
  cursor,
}: {
  session: Session;
  plan: Plan;
  cursor: number;
}) {
  const ch = plan.changes;
  const ev = plan.evidence;
  const slotMs = session.grid.slotMinutes * 60_000;
  const lockedUntil = new Date(Date.parse(session.grid.start) + ch.lockedThroughSlot * slotMs);

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>
          Decision {plan.decisionIndex + 1} of {plan.decisionCount}
        </h2>
        <span className="chip">{plan.planId.slice(0, 8)}</span>
      </div>

      <div style={{ fontSize: 13, marginBottom: 8 }}>{ch.headline}</div>

      <div className="faint mono-num" style={{ fontSize: 11 }}>
        in force {plan.validFrom.slice(11, 16)}&ndash;{plan.validUntil.slice(11, 16)} UTC
        {" · "}
        {plan.status.toLowerCase()}
        {plan.gap > 0.005 && ` · within ${(plan.gap * 100).toFixed(1)}% of optimal`}
        {" · "}
        {plan.solveMs.toFixed(0)} ms
      </div>

      <div className="row" style={{ marginTop: 9, gap: 6 }}>
        <span className={ch.pastSlotsRewritten === 0 ? "chip ok" : "chip crit"}>
          {ch.pastSlotsRewritten === 0
            ? "0 past slots rewritten"
            : `${ch.pastSlotsRewritten} PAST SLOTS REWRITTEN`}
        </span>
        {ch.lockedThroughSlot > 0 && (
          <span className="chip">history locked to {lockedUntil.toISOString().slice(11, 16)}</span>
        )}
        {ch.changedSlots > 0 && <span className="chip warn">{ch.changedSlots} slots moved</span>}
      </div>

      <h2 style={{ marginTop: 12 }}>Knew at this moment</h2>
      <div style={{ fontSize: 11 }} className="dim">
        <div>
          {ev.records} records from {ev.sources.join(", ") || "—"}
        </div>
        <div>
          newest published{" "}
          <span className="mono-num" style={{ color: "var(--ink)" }}>
            {ev.newestPublished ? ev.newestPublished.replace("T", " ").slice(0, 16) : "—"}
          </span>{" "}
          UTC
        </div>
        <div>
          {(ev.estimatedFraction * 100).toFixed(0)}% of it has an <em>estimated</em> publication
          time
        </div>
        {ev.hasOracle && <div className="chip crit">oracle-tainted — this is a bug</div>}
      </div>

      {plan.dropped.length > 0 && (
        <>
          <h2 style={{ marginTop: 12 }}>Not scheduled</h2>
          {plan.dropped.map((d) => {
            const p = plan.progress.find((x) => x.targetId === d.targetId);
            return (
              <div key={d.targetId} style={{ fontSize: 11, marginBottom: 3 }}>
                <span style={{ color: "var(--ink)" }}>{d.name}</span>
                <span className="dim">
                  {" — "}
                  {p && p.requiredRefMinutes < 1e5
                    ? `needs ${p.requiredRefMinutes.toFixed(0)} reference-min, banked ${p.accumulatedRefMinutes.toFixed(0)}`
                    : d.message}
                </span>
              </div>
            );
          })}
        </>
      )}

      <h2 style={{ marginTop: 12 }}>Conditions at the cursor</h2>
      <Conditions session={session} plan={plan} cursor={cursor} />
    </div>
  );
}

function Conditions({
  session,
  plan,
  cursor,
}: {
  session: Session;
  plan: Plan;
  cursor: number;
}) {
  const t0 = Date.parse(session.grid.start);
  const slotMs = session.grid.slotMinutes * 60_000;
  const s = Math.max(0, Math.min(session.grid.nSlots - 1, Math.floor((cursor - t0) / slotMs)));
  const sun = session.sunAltitudeDeg[s];
  const moonAlt = session.moon.altitudeDeg[s];
  const cloud = plan.cloudFraction[s];
  const seeing = plan.seeingFwhmArcsec[s];
  return (
    <div className="dim mono-num" style={{ fontSize: 11 }}>
      <div>sun {sun?.toFixed(1)}&deg;</div>
      <div>
        moon {moonAlt?.toFixed(1)}&deg;, {((session.moon.illumination[s] ?? 0) * 100).toFixed(0)}%
        illuminated
      </div>
      <div>forecast cloud {((cloud ?? 0) * 100).toFixed(0)}%</div>
      <div>seeing {seeing?.toFixed(1)}&Prime;</div>
    </div>
  );
}
