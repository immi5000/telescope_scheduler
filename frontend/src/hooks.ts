import {
  type QueryClient,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback } from "react";
import {
  api,
  type DecisionPoint,
  type FullNight,
  type SessionRequest,
} from "./api/client";

/**
 * Every hook below reads one cache entry.
 *
 * The server used to hold a folded night and answer questions about it:
 * `/plan?as_of=`, `/grid?as_of=`, `/geometry`, `/sky`, `/satellites`. It holds
 * nothing now -- a serverless instance forgets between requests -- so the
 * night arrives complete from a single `POST /api/night` and lives here
 * instead, under `["night", id]`.
 *
 * The hooks kept their old names and signatures, and that is the point: what
 * changed is where the answer comes from, not what any component asks for. Six
 * `useQuery` calls share the one key and differ only in `select`, which React
 * Query dedupes into a single entry, so a scrub that used to cost a request
 * per decision point now costs a property lookup.
 */

/**
 * The request each night was folded from, so it can be folded again.
 *
 * Module-scoped rather than in the query cache because it is an input, not an
 * answer. A reload empties it, which is correct: a reload also empties the
 * night it would refetch, and neither outlives the tab -- nothing about this
 * night is durable anywhere, by design.
 */
const REQUESTS = new Map<string, SessionRequest>();

export const nightKey = (id: string | null) => ["night", id] as const;

/**
 * Stamp a folded night with the id it is cached under.
 *
 * Every fold mints a fresh server-side id, including a re-fold of a night
 * already on screen. The id a component holds must not change underneath it
 * for that, so the cache key wins and the payload is made to agree with it.
 * The id names "the night being looked at", which is a fact about this tab;
 * it stopped naming anything on the server the moment the server stopped
 * keeping one.
 */
function withId(night: FullNight, id: string): FullNight {
  if (night.session.id === id) return night;
  return { ...night, session: { ...night.session, id } };
}

/** The request a night was folded from, if this tab still holds it. */
export function nightRequest(id: string | null): SessionRequest | undefined {
  return id ? REQUESTS.get(id) : undefined;
}

/** Install a re-folded night in place, under the id it is already known by. */
export function applyNight(
  qc: QueryClient,
  id: string,
  req: SessionRequest,
  night: FullNight,
): void {
  REQUESTS.set(id, req);
  qc.setQueryData(nightKey(id), withId(night, id));
}

/**
 * The cached night. Refetching it re-folds against current data.
 *
 * `staleTime: Infinity` because a fold is expensive and never spontaneously
 * wrong -- every plan in it is stamped with the instant it was built at. It is
 * refetched only when something asks, which is what `useRefreshNight` is.
 */
function useNightQuery<T>(id: string | null, select: (n: FullNight) => T, enabled = true) {
  return useQuery({
    queryKey: nightKey(id),
    queryFn: () => api.night(REQUESTS.get(id!)!).then((n) => withId(n, id!)),
    enabled: !!id && enabled && REQUESTS.has(id ?? ""),
    staleTime: Infinity,
    gcTime: Infinity,
    select,
  });
}

export function usePresets() {
  return useQuery({ queryKey: ["presets"], queryFn: api.presets, staleTime: Infinity });
}

/**
 * Fold a night.
 *
 * Resolves to the night's id once the whole payload is in cache, so a caller
 * that sets `sessionId` from it can never render a frame where the id exists
 * and the night does not. The id is the server's own -- it still names the
 * session it built, it simply no longer has anywhere to look it up.
 */
export function useCreateNight() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (req: SessionRequest) => {
      const night = await api.night(req);
      const id = night.session.id;
      REQUESTS.set(id, req);
      qc.setQueryData(nightKey(id), night);
      return night;
    },
  });
}

/**
 * Re-fold this night against whatever the forecast says now.
 *
 * What is left of the live edge. The server used to watch a night that had not
 * ended and append a decision point when a new model run landed; nothing can
 * watch anything here, so the question is asked on demand instead. The answer
 * is the same either way -- each plan is still built at its own `as_of`, and
 * the past is still locked -- only the asking moved to the client.
 */
export function useRefreshNight(id: string | null) {
  const qc = useQueryClient();
  return useCallback(async () => {
    if (!id || !REQUESTS.has(id)) return;
    await qc.refetchQueries({ queryKey: nightKey(id), exact: true });
  }, [qc, id]);
}

export function useSession(id: string | null) {
  return useNightQuery(id, (n) => n.session);
}

/**
 * The plan in force at the cursor.
 *
 * `points` and `revision` are still taken so call sites did not have to
 * change, but the index is all that is read: every plan is already here, so
 * there is no instant to send and no cache interval to respect.
 */
export function usePlan(
  id: string | null,
  _points: DecisionPoint[],
  index: number,
  _revision = 0,
) {
  const query = useNightQuery(id, (n) => n.decisions[clamp(index, n.decisions.length)]?.plan);
  return { ...query, decisionIndex: index };
}

export function useGrid(
  id: string | null,
  _points: DecisionPoint[],
  index: number,
  _revision = 0,
) {
  return useNightQuery(id, (n) => n.decisions[clamp(index, n.decisions.length)]?.grid);
}

/** The night's geometry. As_of-independent, as it always was. */
export function useGeometry(id: string | null, ready = true) {
  return useNightQuery(id, (n) => n.geometry, ready);
}

/** Planets and sky darkness. */
export function useSky(id: string | null, ready = true) {
  return useNightQuery(id, (n) => n.sky, ready);
}

/**
 * Satellite passes. Shares the night's cache entry with everything else, so
 * the alerts module reading this alongside the sky view costs no second fetch.
 */
export function useSatellites(id: string | null) {
  return useNightQuery(id, (n) => n.satellites);
}

function clamp(index: number, length: number): number {
  if (length === 0) return 0;
  return Math.max(0, Math.min(index, length - 1));
}
