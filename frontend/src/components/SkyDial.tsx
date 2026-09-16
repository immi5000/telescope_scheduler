import type { QualityGrid, Session } from "../api/client";
import { shortName } from "../format";

/**
 * An alt-az dial, not an overlay on a star map.
 *
 * Two reasons this is its own thing. Aladin Lite v3's `cooFrame` supports ICRS
 * and galactic only -- there is no horizontal frame, so the obvious approach is
 * ruled out at the API level rather than by taste. And the observer's actual
 * question is "where do I point, and is it high enough", which is a question in
 * the coordinate system their body uses.
 *
 * Division of labour: a star map would answer "what is there", this answers
 * "what is up now", the timeline answers "when".
 */
export function SkyDial({
  session,
  grid,
  cursor,
  minAltitude = 30,
}: {
  session: Session;
  grid: QualityGrid | undefined;
  cursor: number;
  minAltitude?: number;
}) {
  const size = 260;
  const c = size / 2;
  const R = c - 26;
  const t0 = Date.parse(session.grid.start);
  const slotMs = session.grid.slotMinutes * 60_000;
  const slot = Math.max(
    0,
    Math.min(session.grid.nSlots - 1, Math.floor((cursor - t0) / slotMs)),
  );

  // Zenith at the centre, horizon at the rim: r scales with (90 - altitude), so
  // the radial axis is altitude and the angular axis is azimuth.
  const project = (altDeg: number, azDeg: number) => {
    const r = (R * (90 - altDeg)) / 90;
    const a = ((azDeg - 90) * Math.PI) / 180;
    return [c + r * Math.cos(a), c + r * Math.sin(a)] as const;
  };

  const moonAlt = session.moon.altitudeDeg[slot] ?? -90;
  const moonAz = session.moon.azimuthDeg[slot] ?? 0;
  const [mx, my] = project(Math.max(moonAlt, -5), moonAz);

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>Sky right now</h2>
        <span className="faint mono-num" style={{ fontSize: 10 }}>
          {new Date(cursor).toISOString().slice(11, 16)} UTC
        </span>
      </div>
      <svg viewBox={`0 0 ${size} ${size}`} style={{ width: "100%", maxWidth: size, height: "auto" }}>
        {[0, 30, 60].map((alt) => (
          <circle
            key={alt}
            cx={c}
            cy={c}
            r={(R * (90 - alt)) / 90}
            fill="none"
            stroke="var(--line)"
            strokeDasharray={alt === minAltitude ? "none" : "2 3"}
            strokeWidth={alt === 0 ? 1.2 : 1}
          />
        ))}
        {/* the altitude floor: below this line nothing may be scheduled */}
        <circle
          cx={c}
          cy={c}
          r={(R * (90 - minAltitude)) / 90}
          fill="none"
          stroke="var(--warn)"
          strokeWidth={1}
          opacity={0.55}
        />
        {(["N", "E", "S", "W"] as const).map((label, i) => {
          const [lx, ly] = project(-7, i * 90);
          return (
            <text key={label} x={lx} y={ly + 3} textAnchor="middle">
              {label}
            </text>
          );
        })}

        {moonAlt > -5 && (
          <g opacity={moonAlt > 0 ? 1 : 0.35}>
            <circle cx={mx} cy={my} r={7} fill="var(--warn)" opacity={0.28} />
            <circle cx={mx} cy={my} r={3.5} fill="var(--warn)" />
            <text x={mx} y={my - 10} textAnchor="middle">
              {`moon ${Math.round((session.moon.illumination[slot] ?? 0) * 100)}%`}
            </text>
          </g>
        )}

        {grid?.rows.map((row) => {
          const alt = row.altitudeDeg[slot];
          const az = row.azimuthDeg?.[slot];
          if (alt === undefined || alt < -2) return null;
          const [px, py] = project(alt, az ?? 0);
          const up = row.visible[slot];
          return (
            <g key={row.targetId} opacity={up ? 1 : 0.32}>
              <circle cx={px} cy={py} r={3} fill={up ? "var(--ok)" : "var(--ink-faint)"} />
              <text x={px + 6} y={py + 3}>
                {shortName(row.name)}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="faint" style={{ fontSize: 10, marginTop: 2 }}>
        zenith at centre, horizon at the rim &middot; amber ring is the {minAltitude}&deg; floor
      </div>
    </div>
  );
}
