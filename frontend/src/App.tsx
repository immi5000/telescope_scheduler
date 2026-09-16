import { useEffect, useState } from "react";
import { Cards } from "./components/Cards";
import { ChangePanel } from "./components/ChangePanel";
import { Heatmap } from "./components/Heatmap";
import { SetupPanel } from "./components/SetupPanel";
import { SkyDial } from "./components/SkyDial";
import { Timeline } from "./components/Timeline";
import { useEventStream, useGrid, usePlan, usePresets, useSession } from "./hooks";
import { useUi } from "./store";

export function App() {
  const { sessionId, setSession, cursor, setCursor, nightMode, toggleNightMode, brightness,
    setBrightness, heatmapLayer, setHeatmapLayer } = useUi();

  const presets = usePresets();
  const session = useSession(sessionId);
  const points = session.data?.decisionPoints ?? [];
  const plan = usePlan(sessionId, points, cursor);
  const grid = useGrid(sessionId, points, cursor);
  const [live, setLive] = useState<string>("");

  useEventStream((e) => {
    if (e.message) setLive(e.message);
  });

  useEffect(() => {
    document.documentElement.dataset.night = nightMode ? "on" : "off";
    document.documentElement.style.filter = brightness < 1 ? `brightness(${brightness})` : "";
  }, [nightMode, brightness]);

  // Park the cursor at dusk whenever a new night finishes folding.
  const start = session.data?.grid.start;
  useEffect(() => {
    if (start) setCursor(Date.parse(start));
  }, [start, setCursor]);

  const building = session.data?.status === "building";

  return (
    <div className="app">
      <header className="row" style={{ padding: "14px 0 2px" }}>
        <h1>Telescope night scheduler</h1>
        <span className="faint" style={{ fontSize: 11 }}>
          the plan at any instant uses only data published by then
        </span>
        <div className="grow" />
        {live && (
          <span className="faint mono-num" style={{ fontSize: 10 }}>
            {live}
          </span>
        )}
        <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          brightness
          <input
            type="range"
            min={0.35}
            max={1}
            step={0.05}
            value={brightness}
            onChange={(e) => setBrightness(Number(e.target.value))}
            style={{ width: 88 }}
          />
        </label>
        <button onClick={toggleNightMode}>{nightMode ? "night mode: on" : "night mode: off"}</button>
      </header>

      {presets.isLoading && <div className="panel empty">loading presets&hellip;</div>}
      {presets.isError && (
        <div className="panel empty">
          Cannot reach the API. Start it with <code>uv run python -m tscheduler.api</code>.
        </div>
      )}
      {presets.data && (
        <SetupPanel presets={presets.data} onCreated={setSession} busy={building} />
      )}

      {session.data && building && (
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>{session.data.message}</span>
            <span className="mono-num faint">{Math.round(session.data.progress * 100)}%</span>
          </div>
          <div className="meter" style={{ marginTop: 6 }}>
            <i style={{ width: `${session.data.progress * 100}%` }} />
          </div>
        </div>
      )}

      {session.data?.status === "failed" && (
        <div className="panel">
          <span className="chip crit">fold failed</span>
          <div style={{ marginTop: 6 }}>{session.data.error}</div>
        </div>
      )}

      {session.data && session.data.decisionPoints.length > 0 && (
        <>
          <Timeline
            session={session.data}
            plan={plan.data}
            cursor={cursor || Date.parse(session.data.grid.start)}
            onCursor={setCursor}
          />

          <div className="split">
            <div style={{ display: "grid", gap: 12 }}>
              {plan.data && <Cards plan={plan.data} cursor={cursor} />}
              {grid.data && (
                <>
                  <div className="row" style={{ gap: 6 }}>
                    <button
                      className={heatmapLayer === "efficiency" ? "primary" : ""}
                      onClick={() => setHeatmapLayer("efficiency")}
                    >
                      efficiency
                    </button>
                    <button
                      className={heatmapLayer === "preference" ? "primary" : ""}
                      onClick={() => setHeatmapLayer("preference")}
                    >
                      preference
                    </button>
                    <span className="faint" style={{ fontSize: 10 }}>
                      shown separately on purpose &mdash; efficiency decides what is possible,
                      preference decides what is nice
                    </span>
                  </div>
                  <Heatmap
                    session={session.data}
                    grid={grid.data}
                    plan={plan.data}
                    layer={heatmapLayer}
                    cursor={cursor}
                  />
                </>
              )}
            </div>

            <div style={{ display: "grid", gap: 12 }}>
              {plan.data && (
                <ChangePanel session={session.data} plan={plan.data} cursor={cursor} />
              )}
              <SkyDial
                session={session.data}
                grid={grid.data}
                cursor={cursor}
                minAltitude={30}
              />
            </div>
          </div>
        </>
      )}

      {!session.data && !presets.isLoading && (
        <div className="panel empty">
          Pick a night and press &ldquo;plan the night&rdquo;. The server folds the whole night
          once; the slider then indexes into the result rather than re-solving.
        </div>
      )}
    </div>
  );
}
