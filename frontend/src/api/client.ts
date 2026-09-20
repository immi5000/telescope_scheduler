/**
 * The only place this app talks to the server.
 *
 * Types come from `schema.d.ts`, which is generated from the backend's own
 * OpenAPI document (`npm run gen:types`) and checked against the live app by a
 * pytest. Hand-written mirrors of a wire format drift silently; a generated one
 * fails the build instead.
 */
import type { components } from "./schema";

type Schemas = components["schemas"];

export type Presets = Schemas["PresetsOut"];
export type SitePreset = Schemas["SitePresetOut"];
export type Equipment = Schemas["EquipmentOut"];
/** @deprecated the same shape now describes custom rigs too; use `Equipment`. */
export type EquipmentPreset = Equipment;
export type EquipmentRequest = Schemas["EquipmentRequest"];
export type TelescopePreset = Schemas["TelescopePresetOut"];
export type CameraPreset = Schemas["CameraPresetOut"];
export type SiteRequest = Schemas["SiteRequest"];
export type TonightRequest = Schemas["TonightRequest"];
export type Tonight = Schemas["TonightOut"];
export type TargetSuggestion = Schemas["TargetSuggestionOut"];
export type WeatherSource = Schemas["WeatherSourceOut"];
export type WeatherOutlook = Schemas["WeatherOutlookOut"];
export type Session = Schemas["SessionOut"];
export type SessionListItem = Schemas["SessionListItemOut"];
export type SessionRequest = Schemas["SessionRequest"];
export type Plan = Schemas["PlanOut"];
export type Block = Schemas["BlockOut"];
export type Slot = Schemas["SlotOut"];
export type TargetProgress = Schemas["TargetProgressOut"];
export type DecisionPoint = Schemas["DecisionPointOut"];
export type TwilightBand = Schemas["TwilightBandOut"];
export type QualityGrid = Schemas["QualityGridOut"];
export type GridRow = Schemas["GridRowOut"];
export type Geometry = Schemas["GeometryOut"];
export type GeometryRow = Schemas["GeometryRowOut"];
export type Moon = Schemas["MoonOut"];
export type Target = Schemas["TargetOut"];
export type Grid = Schemas["GridOut"];
export type Warning = Schemas["WarningOut"];
export type Health = Schemas["HealthOut"];
export type NightWindow = Schemas["NightWindowOut"];
export type Location = Schemas["LocationOut"];
export type Sky = Schemas["SkyOut"];
export type Planet = Schemas["PlanetOut"];
export type Satellites = Schemas["SatellitesOut"];
export type SatellitePass = Schemas["SatellitePassOut"];
export type Live = Schemas["LiveOut"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json" }, signal });
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return (await res.json()) as T;
}

async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return (await res.json()) as T;
}

async function detail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    return JSON.stringify(body.detail ?? body);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

export const api = {
  health: () => get<Health>("/api/health"),
  presets: () => get<Presets>("/api/presets"),
  sessions: () => get<SessionListItem[]>("/api/sessions"),
  session: (id: string) => get<Session>(`/api/sessions/${id}`),

  /** The plan in force at an instant. `asOf` is an ISO string. */
  plan: (id: string, asOf: string) =>
    get<Plan>(`/api/sessions/${id}/plan?as_of=${encodeURIComponent(asOf)}`),

  grid: (id: string, asOf: string) =>
    get<QualityGrid>(`/api/sessions/${id}/grid?as_of=${encodeURIComponent(asOf)}`),

  /**
   * Where everything is, all night. Takes no instant, by construction.
   *
   * Nothing in this payload depends on published data, so there is no clock in
   * the question and therefore nothing that could leak. It is also immutable
   * for the life of the session, so the browser cache serves it from then on
   * and a scrub across the whole night costs one request, once.
   */
  geometry: (id: string) => get<Geometry>(`/api/sessions/${id}/geometry`),

  /** Planets and how dark the sky is, per slot. Display only; no instant. */
  sky: (id: string) => get<Sky>(`/api/sessions/${id}/sky`),

  /**
   * Satellite passes across the night, every ten seconds. Answers
   * `available: false` with a reason, never an error, when offline.
   */
  satellites: (id: string) => get<Satellites>(`/api/sessions/${id}/satellites`),

  /** Astronomical dusk and dawn for a site. The client never derives these. */
  nightWindow: (date: string, lat: number, lon: number, elevationM: number) =>
    get<NightWindow>(
      `/api/night-window?date=${encodeURIComponent(date)}&lat=${lat}&lon=${lon}` +
        `&elevation_m=${elevationM}`,
    ),

  /**
   * What the browser's coordinates are called, and how high they are.
   *
   * Decoration only: the coordinates came from the device and are the site
   * whatever this answers. Both fields are nullable and a lookup that could
   * not be made answers 200 with nulls, so a caller shows the coordinates
   * either way rather than treating this as a failure.
   */
  locate: (lat: number, lon: number, signal?: AbortSignal) =>
    get<Location>(`/api/site/locate?lat=${lat}&lon=${lon}`, signal),

  /**
   * Focal ratio, resolving power, star size, sampling and field for whatever
   * rig the form currently holds. The form shows these beside its inputs, and
   * they are the server's arithmetic -- the same the scheduler will use.
   */
  deriveEquipment: (body: EquipmentRequest, signal?: AbortSignal) =>
    post<Equipment>("/api/equipment/derive", body, signal),

  /** The whole catalogue, ranked for one site, night and rig. No weather. */
  tonight: (body: TonightRequest, signal?: AbortSignal) =>
    post<Tonight>("/api/targets/tonight", body, signal),

  create: (body: SessionRequest) => post<Session>("/api/sessions", body),

  /**
   * Ask the server to check a live night for new data now rather than at its
   * next scheduled check. 409 for a replay night, 429 within a minute of the
   * last check.
   */
  refresh: (id: string) => post<Session>(`/api/sessions/${id}/refresh`, {}),
};

/**
 * The decision point in force at `t` -- a binary search, not a request.
 *
 * This is the client half of "fold once, then index". Between two consecutive
 * decision points the plan is identical by construction, so dragging the cursor
 * inside one interval must not touch the network at all. Keying the plan query
 * on the *index* rather than the cursor is what makes that true.
 */
export function decisionIndexAt(points: DecisionPoint[], t: number): number {
  let lo = 0;
  let hi = points.length - 1;
  let best = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const at = Date.parse(points[mid]!.at);
    if (at <= t) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best;
}
