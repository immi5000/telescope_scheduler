import { useState } from "react";
import type { Block, Plan } from "../api/client";
import { hhmm } from "../format";

/**
 * Instruction cards: a checklist, not a report.
 *
 * Hierarchy is chronological -- the order of physical actions -- rather than by
 * abstract data importance. When, then what to point at, then where in the sky,
 * then the recipe, then progress, then warnings.
 *
 * Two choices that look wrong and are not:
 *
 * Coordinates appear in BOTH sexagesimal and decimal, each with a copy button,
 * because nobody retypes 23h 37m 04.7s correctly at 2 a.m. And alt/az is given
 * as prominently as RA/Dec, because RA/Dec is for the machine and alt/az is for
 * the person standing next to the telescope.
 *
 * Warnings sit BELOW the recipe. By the time the card is on screen the plan has
 * been accepted, so a warning is something to do during the block, not an input
 * to a decision -- and putting it above the coordinates delays the very action
 * the card exists to enable. Only `critical` is promoted to the top strip.
 */

export function Cards({ plan, cursor }: { plan: Plan; cursor: number }) {
  if (!plan.blocks.length) {
    return (
      <div className="panel empty">
        Nothing is schedulable in this window. See &ldquo;not scheduled&rdquo; below for why.
      </div>
    );
  }
  return (
    <div className="cards">
      {plan.blocks.map((b) => (
        <Card key={`${b.targetId}-${b.slotStart}`} block={b} plan={plan} cursor={cursor} />
      ))}
    </div>
  );
}

function Card({ block: b, plan, cursor }: { block: Block; plan: Plan; cursor: number }) {
  const start = Date.parse(b.startsAt);
  const end = Date.parse(b.endsAt);
  const past = end <= cursor;
  const current = start <= cursor && cursor < end;
  const prog = plan.progress.find((p) => p.targetId === b.targetId);
  const crit = b.warnings.filter((w) => w.severity === "critical");
  const rest = b.warnings.filter((w) => w.severity !== "critical");

  return (
    <div className={`card ${past ? "past" : ""} ${current ? "current" : ""}`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong className="mono-num" style={{ fontSize: 15 }}>
          {hhmm(b.startsAt)}&ndash;{hhmm(b.endsAt)}
        </strong>
        {current && <span className="chip ok">now</span>}
        {past && <span className="chip">done</span>}
      </div>

      <div style={{ fontSize: 15, margin: "4px 0 6px" }}>{b.name}</div>

      {crit.length > 0 && (
        <div style={{ marginBottom: 6 }}>
          {crit.map((w, i) => (
            <div key={i} className="chip crit" style={{ display: "flex", marginBottom: 3 }}>
              {w.text}
            </div>
          ))}
        </div>
      )}

      <div className="coord">
        <span className="mono-num">{b.ra}</span>
        <Copy value={b.ra} />
        <span className="mono-num">{b.dec}</span>
        <Copy value={b.dec} />
      </div>
      <div className="row faint" style={{ fontSize: 11, gap: 6, marginTop: 2 }}>
        <span className="mono-num">{b.raDeg.toFixed(4)}&deg;</span>
        <Copy value={b.raDeg.toFixed(5)} />
        <span className="mono-num">{b.decDeg.toFixed(4)}&deg;</span>
        <Copy value={b.decDeg.toFixed(5)} />
      </div>

      <div className="dim" style={{ marginTop: 7, fontSize: 12 }}>
        Look <strong style={{ color: "var(--ink)" }}>{b.compass}</strong>, altitude{" "}
        <span className="mono-num">
          {Math.round(b.altitudeStartDeg)}&deg; &rarr; {Math.round(b.altitudeEndDeg)}&deg;
        </span>{" "}
        &middot; airmass <span className="mono-num">{b.airmassMin.toFixed(2)}</span>
      </div>

      <div style={{ marginTop: 7, fontSize: 13 }}>
        {b.switchMinutes > 0 && (
          <span className="faint">{b.switchMinutes} min to move &amp; focus, then </span>
        )}
        <strong>
          {b.nSubs} &times; {Math.round(b.tSubS)} s
        </strong>{" "}
        <span className="dim">({b.integratingMinutes} min integrating)</span>
      </div>

      {prog && (
        <div style={{ marginTop: 7 }}>
          <div className="row" style={{ justifyContent: "space-between", fontSize: 11 }}>
            <span className="dim">
              block SNR <span className="mono-num">{b.expectedSnr.toFixed(0)}</span>
              {" · "}night total <span className="mono-num">{prog.expectedSnr.toFixed(0)}</span>
              {" / "}
              <span className="mono-num">{prog.snrGoal.toFixed(0)}</span>
            </span>
            <span className={prog.included ? "chip ok" : "chip warn"}>
              {prog.included ? "reaches goal" : "short"}
            </span>
          </div>
          <div className="meter" style={{ marginTop: 3 }}>
            <i
              style={{
                width: `${Math.min(100, (prog.expectedSnr / prog.snrGoal) * 100)}%`,
                background: prog.included ? "var(--ok)" : "var(--warn)",
              }}
            />
          </div>
        </div>
      )}

      {rest.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {rest.slice(0, 3).map((w, i) => (
            <div key={i} className="warnrow">
              <span className={w.severity === "warn" ? "chip warn" : "chip"} style={{ flex: "0 0 auto" }}>
                {w.severity}
              </span>
              <span>
                <span style={{ color: "var(--ink)" }}>{w.text}</span>
                <span className="dim"> &mdash; {w.action}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Copy({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="copy"
      title={`copy ${value}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(
          () => {
            setDone(true);
            setTimeout(() => setDone(false), 900);
          },
          () => undefined,
        );
      }}
    >
      {done ? "ok" : "copy"}
    </button>
  );
}
