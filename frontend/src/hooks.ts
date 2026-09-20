import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { ApiError, api, type DecisionPoint } from "./api/client";

export function usePresets() {
  return useQuery({ queryKey: ["presets"], queryFn: api.presets, staleTime: Infinity });
}

export function useSession(id: string | null) {
  return useQuery({
    queryKey: ["session", id],
    queryFn: () => api.session(id!),
    enabled: !!id,
    // While the fold runs, poll. The SSE stream also invalidates, but a poll
    // means the page still works with the stream blocked by a proxy -- which
    // is the documented fallback, so it had better actually be in place. A
    // live night keeps a slow poll for the same reason: its updates arrive
    // over the stream, and a blocked stream must not freeze the night.
    refetchInterval: (q) =>
      q.state.data?.status === "building" ? 700 : q.state.data?.live?.following ? 60_000 : false,
  });
}

/**
 * The plan in force at the cursor.
 *
 * Keyed on the decision INDEX, not the cursor. Dragging within one decision
 * interval therefore hits the same cache entry and issues no request: the
 * server already told us, via validFrom/validUntil, that the answer cannot
 * change until the next decision point.
 */
export function usePlan(
  id: string | null,
  points: DecisionPoint[],
  index: number,
  revision = 0,
) {
  const at = points[Math.min(index, points.length - 1)]?.at;
  const query = useQuery({
    // points.length is part of the key on purpose. A plan fetched while the
    // fold is still running reports the decision count KNOWN AT THAT MOMENT,
    // and its validUntil runs to the end of the night because no later point
    // exists yet. With staleTime: Infinity and no count in the key, that first
    // answer would be cached forever and the panel would sit on "decision 1 of
    // 1" for a night that has four. Observed, not hypothetical.
    //
    // `revision` is the live watch's counter. Before dusk a new forecast
    // re-makes the opening plan IN PLACE -- same index, same count, different
    // plan -- and only this tells the cache.
    queryKey: ["plan", id, index, points.length, revision],
    queryFn: () => api.plan(id!, at!),
    enabled: !!id && !!at,
    staleTime: Infinity,
    placeholderData: (prev) => prev,
  });
  return { ...query, decisionIndex: index };
}

/**
 * The night's geometry. One fetch per session, then never again.
 *
 * No `asOf` in the key because there is none in the request: this is the
 * as_of-INDEPENDENT half of the wire, and keying it on the cursor would
 * reintroduce exactly the refetch-per-scrub the split removed.
 */
export function useGeometry(id: string | null, ready: boolean) {
  return useQuery({
    queryKey: ["geometry", id],
    queryFn: () => api.geometry(id!),
    enabled: !!id && ready,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: (count, err) =>
      // 425 means the fold has not produced geometry yet, which is a matter of
      // waiting rather than an error. Anything else is worth failing fast on.
      err instanceof ApiError && err.status === 425 ? count < 30 : count < 2,
    retryDelay: 500,
  });
}

/**
 * Planets and sky darkness. Immutable for the session, like the geometry.
 */
export function useSky(id: string | null, ready: boolean) {
  return useQuery({
    queryKey: ["sky", id],
    queryFn: () => api.sky(id!),
    enabled: !!id && ready,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: (count, err) =>
      err instanceof ApiError && err.status === 425 ? count < 30 : count < 2,
    retryDelay: 500,
  });
}

/**
 * Satellite passes. The key and options are shared with the alerts module,
 * which reads the same payload -- keep them identical or it fetches twice.
 */
export function useSatellites(id: string | null) {
  return useQuery({
    queryKey: ["satellites", id],
    queryFn: () => api.satellites(id!),
    enabled: !!id,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
}

export function useGrid(
  id: string | null,
  points: DecisionPoint[],
  index: number,
  revision = 0,
) {
  const at = points[Math.min(index, points.length - 1)]?.at;
  return useQuery({
    queryKey: ["grid", id, index, points.length, revision],
    queryFn: () => api.grid(id!, at!),
    enabled: !!id && !!at,
    staleTime: Infinity,
    placeholderData: (prev) => prev,
  });
}

export interface StreamEvent {
  type: string;
  sessionId?: string;
  progress?: number;
  message?: string;
}

/**
 * Server-sent notifications.
 *
 * The handler does exactly one thing: invalidate. No event carries plan data,
 * so there is a single fetching path shared with replay, and this whole hook
 * could be replaced by a 15-second poll without touching a component.
 */
export function useEventStream(onEvent?: (e: StreamEvent) => void) {
  const qc = useQueryClient();
  useEffect(() => {
    const es = new EventSource("/api/events");
    const handle = (raw: MessageEvent<string>) => {
      let data: StreamEvent;
      try {
        data = JSON.parse(raw.data) as StreamEvent;
      } catch {
        return;
      }
      if (data.sessionId) {
        void qc.invalidateQueries({ queryKey: ["session", data.sessionId] });
      }
      onEvent?.(data);
    };
    for (const kind of [
      "session.progress",
      "plan.available",
      "session.ready",
      "session.failed",
      // A live night's watch finished a check: new data or not, its status moved.
      "session.live",
    ]) {
      es.addEventListener(kind, handle as EventListener);
    }
    return () => es.close();
    // onEvent is intentionally not a dependency: re-subscribing on every render
    // would tear down and rebuild the stream continuously.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qc]);
}
