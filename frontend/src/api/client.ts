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
/** A whole night in one payload -- see `POST /api/night`. */
export type FullNight = Schemas["FullNightOut"];
export type DecisionPlan = Schemas["DecisionPlanOut"];
/** A night to fold, plus the object to add to it afterwards, if any. */
export type NightStreamRequest = Schemas["NightStreamRequest"];
export type AddTarget = Schemas["AddTargetRequest"];
export type Amend = Schemas["AmendOut"];
type NightFrame = Schemas["NightFrameOut"];

/** One stage of a fold: how far it has got, and what it is doing. */
export interface FoldStage {
  /** 0..1, monotone. */
  fraction: number;
  /** The stage, in the server's words. */
  message: string;
}

export type OnFoldStage = (stage: FoldStage) => void;

/** A stream that stopped mid-frame. The night is unchanged; nothing arrived. */
const CUT_OFF =
  "The fold was cut off before it finished. Nothing has changed \u2014 try again, " +
  "or plan a shorter night.";

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

/**
 * Fold a night, reading the progress it reports on the way.
 *
 * The response is NDJSON -- one `NightFrameOut` per line -- because a fold
 * takes seconds and there is nowhere else for a percentage to come from: no
 * session is kept, so there is nothing to poll. Progress and the night itself
 * arrive over the SAME connection, which is what makes this work on a host
 * where the next request would reach a different process.
 *
 * It degrades rather than breaks. A proxy that buffers the whole body delivers
 * every frame at once at the end: the night is still correct, and only the bar
 * loses its motion. `onStage` is therefore a hint about a wait, never the way
 * anything is learnt.
 *
 * An `error` frame is raised as the `ApiError` it stands in for. The status
 * code it carries is the one the response would have had, had the fold failed
 * before its headers went out -- 409 for an object the night cannot fit, and
 * it means exactly what 409 means on any other endpoint here.
 */
async function nightStream(
  body: NightStreamRequest,
  onStage?: OnFoldStage,
  signal?: AbortSignal,
): Promise<FullNight> {
  const res = await fetch("/api/night/stream", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/x-ndjson",
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new ApiError(res.status, await detail(res));

  let night: FullNight | null = null;
  const take = (line: string): void => {
    const text = line.trim();
    if (!text) return;
    const frame = JSON.parse(text) as NightFrame;
    if (frame.type === "progress") {
      onStage?.({ fraction: frame.fraction ?? 0, message: frame.message ?? "" });
    } else if (frame.type === "night" && frame.night) {
      night = frame.night;
    } else if (frame.type === "error") {
      throw new ApiError(frame.status ?? 500, frame.detail ?? "the fold failed");
    }
  };

  if (res.body) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (err) {
        // The connection went away with the 200 already committed -- which is
        // what a function hitting its time limit looks like from here, and
        // what a dropped link looks like too. Under gzip the truncated member
        // rejects the read rather than ending it, so this is where that lands;
        // an ApiError is thrown for both, because "the fold was cut off" is
        // the thing to say, not a decoder's word for it.
        if (err instanceof ApiError) throw err;
        throw new ApiError(504, CUT_OFF);
      }
      const { done, value } = chunk;
      // `stream: true` matters: a frame is ~900 KB and WILL be split across
      // chunks, sometimes mid-character. Decoding each chunk independently
      // mangles the byte that straddles the boundary.
      if (value) buffer += decoder.decode(value, { stream: true });
      if (done) buffer += decoder.decode();
      let nl = buffer.indexOf("\n");
      while (nl >= 0) {
        take(buffer.slice(0, nl));
        buffer = buffer.slice(nl + 1);
        nl = buffer.indexOf("\n");
      }
      if (done) break;
    }
    // Every frame is newline terminated, so anything left over is half of one:
    // the same cut-off, arriving as a clean end instead of a rejected read.
    // Parsing it would report a truncated fold as a JSON syntax error.
    if (buffer.trim()) throw new ApiError(504, CUT_OFF);
  } else {
    // No streams here (an old browser, or a test double). The body is the same
    // lines either way, so reading it whole still yields the night.
    for (const line of (await res.text()).split("\n")) take(line);
  }

  if (night === null) throw new ApiError(502, "the fold ended without a night");
  return night;
}

export const api = {
  health: () => get<Health>("/api/health"),

  /** {@link nightStream} -- fold a night, and report the wait while it lasts. */
  nightStream,

  /**
   * Fold a night and get all of it at once.
   *
   * The stateless replacement for `create` plus the five per-session GETs
   * below. It blocks for the length of the fold -- three to seven seconds --
   * and answers with every plan, grid, geometry row and satellite pass the
   * night will ever have, because the server keeps none of it to be asked
   * for later. Sending the same body again re-folds against current data,
   * which is what "refresh" now means.
   */
  night: (body: SessionRequest, signal?: AbortSignal) =>
    post<FullNight>("/api/night", body, signal),
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
