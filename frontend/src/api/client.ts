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
export type EquipmentPreset = Schemas["EquipmentPresetOut"];
export type CatalogEntry = Schemas["CatalogEntryOut"];
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
export type Warning = Schemas["WarningOut"];
export type Health = Schemas["HealthOut"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json" } });
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

  async create(body: SessionRequest): Promise<Session> {
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new ApiError(res.status, await detail(res));
    return (await res.json()) as Session;
  },
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
