import { scaleLinear } from "d3-scale";
import { useCallback, useMemo, useRef } from "react";
import type { DecisionPoint, Plan, Session } from "../api/client";
import { shortName } from "../format";

/**
 * The night as a Gantt, with the replay cursor on the same axis.
 *
 * One axis serves both jobs, and that is the point: the cursor's position is
 * *when you are* as well as *what you are looking at*. Everything left of it is
 * history the scheduler may not touch, which is why the hatch and the cursor
 * are the same line rather than two controls that can disagree.
 *
 * Plain SVG with d3-scale, no chart library. What is drawn here -- twilight
 * bands, hatched locked regions, decision ticks, ghost outlines of the previous
 * plan -- is not a chart type, so a layout engine would be pure friction. The
 * node budget is a few hundred elements, comfortably SVG territory.
 */

const TWILIGHT_FILL: Record<string, string> = {
  day: "#1a2634",
  civil: "#141d29",
  nautical: "#0d141d",
  astronomical: "#080d13",
  night: "#04070a",
};

const ROW_H = 20;
const PAD_L = 78;
const PAD_R = 14;
const HEAD_H = 34;
const FOOT_H = 20;

interface Props {
  session: Session;
  plan: Plan | undefined;
  cursor: number;
  onCursor: (t: number) => void;
}

export function Timeline({ session, plan, cursor, onCursor }: Props) {
  const ref = useRef<SVGSVGElement>(null);
  const grid = session.grid;
  const t0 = Date.parse(grid.start);
  const t1 = Date.parse(grid.end);
  const targets = session.targets;

  const width = 1100;
  const height = HEAD_H + targets.length * ROW_H + FOOT_H + 10;
  const x = useMemo(
    () => scaleLinear().domain([t0, t1]).range([PAD_L, width - PAD_R]),
    [t0, t1],
  );

  const slotMs = grid.slotMinutes * 60_000;
  const rowOf = new Map(targets.map((t, i) => [t.id, i]));

  const seek = useCallback(
    (clientX: number) => {
      const box = ref.current?.getBoundingClientRect();
      if (!box) return;
      const px = ((clientX - box.left) / box.width) * width;
      const t = x.invert(Math.min(Math.max(px, PAD_L), width - PAD_R));
      onCursor(Math.min(Math.max(t, t0), t1));
    },
    [x, onCursor, t0, t1],
  );

  const drag = (e: React.PointerEvent<SVGSVGElement>) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    seek(e.clientX);
  };
  const move = (e: React.PointerEvent<SVGSVGElement>) => {
    if (e.buttons === 1) seek(e.clientX);
  };

  const hourTicks = useMemo(() => {
    const out: number[] = [];
    const first = Math.ceil(t0 / 3_600_000) * 3_600_000;
    for (let t = first; t <= t1; t += 3_600_000) out.push(t);
    return out;
  }, [t0, t1]);

  const lockedUntil = plan
    ? t0 + plan.changes.lockedThroughSlot * slotMs
    : t0;

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>Night timeline</h2>
        <span className="faint" style={{ fontSize: 10 }}>
          drag anywhere to scrub &middot; hatched = locked history &middot; ticks = new data
          published
        </span>
      </div>
      <div className="scrollx">
        <svg
          ref={ref}
          viewBox={`0 0 ${width} ${height}`}
          style={{ width: "100%", minWidth: 760, height, touchAction: "none", cursor: "ew-resize" }}
          onPointerDown={drag}
          onPointerMove={move}
          role="slider"
          tabIndex={0}
          aria-label="Replay cursor"
          aria-valuemin={t0}
          aria-valuemax={t1}
          aria-valuenow={cursor}
          aria-valuetext={new Date(cursor).toISOString().slice(11, 16) + " UTC"}
          onKeyDown={(e) => {
            const step = e.shiftKey ? slotMs * 12 : slotMs;
            if (e.key === "ArrowLeft") onCursor(Math.max(t0, cursor - step));
            if (e.key === "ArrowRight") onCursor(Math.min(t1, cursor + step));
          }}
        >
          <defs>
            <pattern id="hatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <rect width="6" height="6" fill="var(--locked)" opacity="0.5" />
              <line x1="0" y1="0" x2="0" y2="6" stroke="var(--line)" strokeWidth="2" />
            </pattern>
          </defs>

          {/* twilight bands: the observer's real constraint, drawn first */}
          {session.twilight.map((b) => (
            <rect
              key={`${b.kind}-${b.fromSlot}`}
              x={x(Date.parse(b.startsAt))}
              y={HEAD_H - 12}
              width={Math.max(x(Date.parse(b.endsAt)) - x(Date.parse(b.startsAt)), 0)}
              height={targets.length * ROW_H + 12}
              fill={TWILIGHT_FILL[b.kind] ?? "#04070a"}
            />
          ))}

          {hourTicks.map((t) => (
            <g key={t}>
              <line
                x1={x(t)}
                x2={x(t)}
                y1={HEAD_H - 12}
                y2={HEAD_H + targets.length * ROW_H}
                stroke="var(--line)"
                strokeWidth={1}
              />
              <text x={x(t)} y={HEAD_H - 18} textAnchor="middle">
                {new Date(t).toISOString().slice(11, 16)}
              </text>
            </g>
          ))}

          {/* locked history */}
          {lockedUntil > t0 && (
            <rect
              x={x(t0)}
              y={HEAD_H - 12}
              width={x(lockedUntil) - x(t0)}
              height={targets.length * ROW_H + 12}
              fill="url(#hatch)"
            />
          )}

          {/* target rows */}
          {targets.map((tgt, i) => {
            const y = HEAD_H + i * ROW_H;
            const done = plan?.progress.find((p) => p.targetId === tgt.id);
            return (
              <g key={tgt.id}>
                <line
                  x1={PAD_L}
                  x2={width - PAD_R}
                  y1={y + ROW_H - 1}
                  y2={y + ROW_H - 1}
                  stroke="var(--line)"
                  strokeWidth={0.5}
                />
                <text
                  x={PAD_L - 6}
                  y={y + 13}
                  textAnchor="end"
                  fill={done?.included ? "var(--ink)" : "var(--ink-faint)"}
                >
                  {shortName(tgt.name)}
                </text>
              </g>
            );
          })}

          {/* the plan itself */}
          {plan?.blocks.map((b) => {
            const i = rowOf.get(b.targetId);
            if (i === undefined) return null;
            const y = HEAD_H + i * ROW_H;
            const bx = x(Date.parse(b.startsAt));
            const bw = Math.max(x(Date.parse(b.endsAt)) - bx, 1);
            const sw = Math.max(x(Date.parse(b.integratingFrom)) - bx, 0);
            const worst = severity(b.warnings.map((w) => w.severity));
            const past = Date.parse(b.endsAt) <= cursor;
            return (
              <g key={`${b.targetId}-${b.slotStart}`} opacity={past ? 0.55 : 1}>
                {sw > 0 && (
                  <rect x={bx} y={y + 3} width={sw} height={ROW_H - 7} fill="var(--ink-faint)" opacity={0.5} />
                )}
                <rect
                  x={bx + sw}
                  y={y + 3}
                  width={Math.max(bw - sw, 1)}
                  height={ROW_H - 7}
                  rx={2}
                  fill={worst === "critical" ? "var(--crit)" : worst === "warn" ? "var(--warn)" : "var(--ok)"}
                  opacity={0.85}
                />
                {bw > 46 && (
                  <text x={bx + sw + 4} y={y + 14} fill="var(--bg)" style={{ fontWeight: 600 }}>
                    {Math.round((Date.parse(b.endsAt) - Date.parse(b.integratingFrom)) / 60000)}m
                  </text>
                )}
              </g>
            );
          })}

          {/* decision points: the only instants at which the plan can change */}
          {session.decisionPoints.map((d: DecisionPoint) => (
            <g key={d.index}>
              <line
                x1={x(Date.parse(d.at))}
                x2={x(Date.parse(d.at))}
                y1={HEAD_H + targets.length * ROW_H}
                y2={HEAD_H + targets.length * ROW_H + 9}
                stroke={d.changedSlots > 0 ? "var(--accent)" : "var(--ink-faint)"}
                strokeWidth={d.changedSlots > 0 ? 2 : 1}
              />
              {d.changedSlots > 0 && (
                <circle cx={x(Date.parse(d.at))} cy={HEAD_H + targets.length * ROW_H + 13} r={2.5} fill="var(--accent)" />
              )}
            </g>
          ))}

          {/* the cursor */}
          <g>
            <line
              x1={x(cursor)}
              x2={x(cursor)}
              y1={HEAD_H - 14}
              y2={HEAD_H + targets.length * ROW_H + 16}
              stroke="var(--accent)"
              strokeWidth={1.5}
            />
            <polygon
              points={`${x(cursor) - 5},${HEAD_H - 20} ${x(cursor) + 5},${HEAD_H - 20} ${x(cursor)},${HEAD_H - 13}`}
              fill="var(--accent)"
            />
          </g>
        </svg>
      </div>
    </div>
  );
}

function severity(list: string[]): "critical" | "warn" | "info" {
  if (list.includes("critical")) return "critical";
  if (list.includes("warn")) return "warn";
  return "info";
}
