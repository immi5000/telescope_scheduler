import type { Plan, QualityGrid, Session } from "../api/client";
import { shortName } from "../format";

/**
 * The efficiency grid, which is the one number in the whole system with an
 * actionable meaning: eta = 0.4 means one second here buys 0.4 seconds of
 * progress against a perfect slot.
 *
 * Efficiency is shown by default and preference is a separate layer, never a
 * blend. Merging them is the modelling error this project deliberately avoids:
 * efficiency decides what is POSSIBLE, preference decides what is NICE, and a
 * single number cannot say "same expected yield, much worse risk" -- which is
 * exactly the distinction a scheduler exists to make.
 */
export function Heatmap({
  session,
  grid,
  plan,
  layer,
  cursor,
}: {
  session: Session;
  grid: QualityGrid;
  plan: Plan | undefined;
  layer: "efficiency" | "preference";
  cursor: number;
}) {
  const n = grid.nSlots;
  const cell = 9;
  const rowH = 15;
  const padL = 84;
  const width = padL + n * cell + 10;
  const height = 22 + grid.rows.length * rowH + 30;
  const t0 = Date.parse(session.grid.start);
  const slotMs = session.grid.slotMinutes * 60_000;
  const cursorSlot = Math.floor((cursor - t0) / slotMs);

  const scheduled = new Map<string, Set<number>>();
  for (const s of plan?.slots ?? []) {
    if (s.kind !== "observe" || !s.targetId) continue;
    if (!scheduled.has(s.targetId)) scheduled.set(s.targetId, new Set());
    scheduled.get(s.targetId)!.add(s.slot);
  }

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>
          {layer === "efficiency" ? "Efficiency η" : "Preference"} by target and slot
        </h2>
        <span className="faint" style={{ fontSize: 10 }}>
          {layer === "efficiency"
            ? "seconds of progress bought per second observed"
            : "soft policy score; never affects feasibility"}
          {" · outlined = scheduled"}
        </span>
      </div>
      <div className="scrollx">
        <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", minWidth: 640, height }}>
          {grid.rows.map((row, i) => {
            const y = 22 + i * rowH;
            const vals = layer === "efficiency" ? row.efficiency : row.preference;
            const picked = scheduled.get(row.targetId);
            return (
              <g key={row.targetId}>
                <text x={padL - 6} y={y + 10} textAnchor="end">
                  {shortName(row.name)}
                </text>
                {vals.map((v, s) => {
                  const vis = row.visible[s];
                  return (
                    <rect
                      key={s}
                      x={padL + s * cell}
                      y={y}
                      width={cell - 1}
                      height={rowH - 3}
                      fill={vis ? "var(--accent)" : "transparent"}
                      fillOpacity={vis ? ramp(v) : 0}
                      stroke={picked?.has(s) ? "var(--accent)" : "none"}
                      strokeWidth={picked?.has(s) ? 1 : 0}
                    >
                      <title>
                        {`${row.name} @ slot ${s}\n${layer} ${v.toFixed(3)}\naltitude ${row.altitudeDeg[s]?.toFixed(1)}°` +
                          `  airmass ${row.airmass[s]?.toFixed(2)}\nmoon ${row.moonSeparationDeg[s]?.toFixed(0)}°` +
                          `  cloud ${((grid.cloudFraction[s] ?? 0) * 100).toFixed(0)}%`}
                      </title>
                    </rect>
                  );
                })}
              </g>
            );
          })}

          {/* cloud strip: the thing that moves between decision points */}
          <text x={padL - 6} y={22 + grid.rows.length * rowH + 12} textAnchor="end">
            cloud
          </text>
          {grid.cloudFraction.map((c, s) => (
            <rect
              key={s}
              x={padL + s * cell}
              y={22 + grid.rows.length * rowH + 3}
              width={cell - 1}
              height={11}
              fill="var(--ink)"
              fillOpacity={0.08 + 0.72 * c}
            >
              <title>{`slot ${s}: ${(c * 100).toFixed(0)}% cloud`}</title>
            </rect>
          ))}

          {cursorSlot >= 0 && cursorSlot < n && (
            <line
              x1={padL + cursorSlot * cell + (cell - 1) / 2}
              x2={padL + cursorSlot * cell + (cell - 1) / 2}
              y1={16}
              y2={height - 8}
              stroke="var(--accent)"
              strokeWidth={1.5}
            />
          )}
        </svg>
      </div>
    </div>
  );
}

/**
 * A single-hue luminance ramp, expressed as opacity over the theme's accent
 * rather than a literal colour. A hard-coded rgba() would stay blue in night
 * mode, which is the one place a stray blue actually costs something: it
 * destroys dark adaptation that took twenty minutes to build.
 */
function ramp(v: number): number {
  const c = Math.max(0, Math.min(1, v));
  return 0.05 + 0.85 * c;
}
