import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Presets, type SessionRequest } from "../api/client";

/** Everything needed to fold a night, in one POST. */
export function SetupPanel({
  presets,
  onCreated,
  busy,
}: {
  presets: Presets;
  onCreated: (id: string) => void;
  busy: boolean;
}) {
  const qc = useQueryClient();
  const [form, setForm] = useState<SessionRequest>({
    date: yesterday(),
    hours: 9,
    startHourUtc: 1,
    slotMinutes: 5,
    siteId: presets.sites[0]?.id ?? "urbana",
    equipmentId: presets.equipment[0]?.id ?? "sct8-2600mm",
    snrGoal: 35,
    tSubS: 90,
    weather: "synthetic",
    solveSeconds: 4,
  });
  const [picked, setPicked] = useState<Set<string>>(new Set(presets.defaultTargetIds));

  const create = useMutation({
    mutationFn: () =>
      api.create({
        ...form,
        targets: [...picked].map((id) => ({ id, priority: 1, urgency: 1 })),
      }),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      onCreated(s.id);
    },
  });

  const set = <K extends keyof SessionRequest>(k: K, v: SessionRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const site = presets.sites.find((s) => s.id === form.siteId);
  const eq = presets.equipment.find((e) => e.id === form.equipmentId);

  return (
    <div className="panel">
      <h2>Session</h2>
      <div className="row">
        <label>
          Night (UTC)
          <input
            type="date"
            value={form.date}
            onChange={(e) => set("date", e.target.value)}
            style={{ width: 138 }}
          />
        </label>
        <label>
          Hours
          <input
            type="number"
            min={1}
            max={16}
            step={1}
            value={form.hours}
            onChange={(e) => set("hours", Number(e.target.value))}
            style={{ width: 62 }}
          />
        </label>
        <label>
          Site
          <select value={form.siteId ?? ""} onChange={(e) => set("siteId", e.target.value)}>
            {presets.sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} &mdash; Bortle {s.bortle}
              </option>
            ))}
          </select>
        </label>
        <label>
          Equipment
          <select
            value={form.equipmentId}
            onChange={(e) => set("equipmentId", e.target.value)}
          >
            {presets.equipment.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          SNR goal
          <input
            type="number"
            min={5}
            max={300}
            step={5}
            value={form.snrGoal}
            onChange={(e) => set("snrGoal", Number(e.target.value))}
            style={{ width: 70 }}
          />
        </label>
        <label>
          Sub (s)
          <input
            type="number"
            min={5}
            max={900}
            step={15}
            value={form.tSubS}
            onChange={(e) => set("tSubS", Number(e.target.value))}
            style={{ width: 70 }}
          />
        </label>
        <label>
          Weather
          <select value={form.weather} onChange={(e) => set("weather", e.target.value)}>
            <option value="synthetic">synthetic (offline)</option>
            <option value="open_meteo">Open-Meteo (real runs)</option>
          </select>
        </label>
        <div className="grow" />
        <button
          className="primary"
          disabled={busy || create.isPending || picked.size === 0}
          onClick={() => create.mutate()}
        >
          {create.isPending || busy ? "folding…" : "plan the night"}
        </button>
      </div>

      <div className="row" style={{ marginTop: 8, gap: 5 }}>
        {presets.catalog.map((t) => {
          const on = picked.has(t.id);
          return (
            <button
              key={t.id}
              className={on ? "primary" : ""}
              style={{ textTransform: "none", letterSpacing: 0, fontSize: 11 }}
              title={`${t.ra} ${t.dec}  ·  ${t.magnitude} mag/arcsec²`}
              onClick={() =>
                setPicked((p) => {
                  const n = new Set(p);
                  if (n.has(t.id)) n.delete(t.id);
                  else n.add(t.id);
                  return n;
                })
              }
            >
              {t.name}
            </button>
          );
        })}
      </div>

      <div className="faint" style={{ fontSize: 10, marginTop: 7 }}>
        {site && (
          <>
            {site.latitudeDeg.toFixed(3)}, {site.longitudeDeg.toFixed(3)} &middot; zenith sky{" "}
            {site.zenithSkyMagArcsec2} mag/arcsec&sup2;
          </>
        )}
        {eq && (
          <>
            {" · "}
            {eq.pixelScaleArcsec.toFixed(2)}&Prime;/px &middot; field{" "}
            {eq.fovWidthDeg.toFixed(2)}&times;{eq.fovHeightDeg.toFixed(2)}&deg; &middot; read noise{" "}
            {eq.readNoiseE} e&minus;
          </>
        )}
        {form.weather === "synthetic" && " · synthetic forecast: several deterministic runs with real publication times"}
      </div>

      {create.isError && (
        <div className="chip crit" style={{ marginTop: 8 }}>
          {(create.error as Error).message}
        </div>
      )}
    </div>
  );
}

function yesterday(): string {
  const d = new Date(Date.now() - 86_400_000);
  return d.toISOString().slice(0, 10);
}
