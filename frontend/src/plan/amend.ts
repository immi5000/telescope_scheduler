/**
 * Adding an object to the night, from the sky.
 *
 * There is no session on the server to amend, so this adds the object to the
 * request the night was folded from and folds it again -- the whole night, not
 * a patch to it. The planning rules are unchanged, because they live in the
 * fold: every plan is still built at its own `as_of` and the past is still
 * locked at each one.
 *
 * What survives verbatim is the contract the old endpoint made, and it is the
 * part worth keeping: an object the optimiser cannot give time to is REFUSED,
 * and the night is left exactly as it was. A fold that comes back without the
 * new target in it is discarded rather than shown, so a caller never has to
 * undo an add it was told had not happened.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { api, type FullNight, type SessionRequest, type Target } from '../api/client'
import type { components } from '../api/schema'
import { applyNight, nightKey, nightRequest } from '../hooks'

export type Catalog = components['schemas']['CatalogOut']
export type CatalogObject = components['schemas']['CatalogObjectOut']
export type AmendResult = components['schemas']['AmendOut']

/** Every catalogue object, positioned. Fixed for the life of the server. */
export function useCatalog() {
  return useQuery({
    queryKey: ['catalog'],
    queryFn: async (): Promise<Catalog> => {
      const res = await fetch('/api/catalog', { headers: { accept: 'application/json' } })
      if (!res.ok) throw new Error(`catalogue: HTTP ${res.status}`)
      return (await res.json()) as Catalog
    },
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  })
}

/** The three fields the server needs together, plus how to read the third. */
export interface OwnCoordinates {
  raDeg: number
  decDeg: number
  magnitude: number
  name?: string
  isPointSource?: boolean
}

/**
 * `add(target, atMs, own)` asks the server to add `target` and re-plan. While
 * it runs, `busy` names the target, so the button that asked can say so.
 *
 * `own` carries the object's own coordinates, for the one case the server
 * cannot look anything up: a star, which exists only in the browser's
 * `stars.bin`. Omit it and the server resolves `target` as a name.
 */
/** `TargetOut` carries every field `TargetRequest` takes, so a folded night
 * can be turned back into the request that would fold it again. */
function asTargetRequest(t: Target): components['schemas']['TargetRequest'] {
  return {
    id: t.id,
    name: t.name,
    raDeg: t.raDeg,
    decDeg: t.decDeg,
    magnitude: t.magnitude,
    priority: t.priority,
    urgency: t.urgency,
    snrGoal: t.snrGoal,
  }
}

export function useAddTarget(sessionId: string | null) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)

  const add = useCallback(
    async (target: string, atMs: number, own?: OwnCoordinates): Promise<AmendResult> => {
      if (!sessionId) throw new Error('no session')
      const base = nightRequest(sessionId)
      const current = qc.getQueryData<FullNight>(nightKey(sessionId))
      if (!base || !current) throw new Error('this night is no longer loaded')

      setBusy(target)
      try {
        const req: SessionRequest = {
          ...base,
          targets: [
            ...current.session.targets.map(asTargetRequest),
            {
              id: target,
              name: own?.name ?? null,
              raDeg: own?.raDeg ?? null,
              decDeg: own?.decDeg ?? null,
              magnitude: own?.magnitude ?? null,
              // The server's own defaults, restated because the generated
              // type makes them required on the way in.
              priority: 1,
              urgency: 1,
            },
          ],
        }
        const night = await api.night(req)

        // Which target is new is answered by difference, not by name: the
        // server resolves `target` through the catalogue and the id it comes
        // back with need not be the string that was sent.
        const before = new Set(current.session.targets.map((t) => t.id))
        const added = night.session.targets.find((t) => !before.has(t.id))
        const last = night.decisions[night.decisions.length - 1]?.plan
        const got = added ? last?.progress.find((p) => p.targetId === added.id) : undefined

        if (!added || !got?.included) {
          // Refused. `night` is dropped on the floor and the cache is not
          // touched, so the observer keeps looking at the night they had.
          throw new Error(
            `${own?.name ?? target} could not be given time tonight without taking it ` +
              'from something already scheduled. The night is unchanged.',
          )
        }

        applyNight(qc, sessionId, req, night)
        return {
          sessionId,
          targetId: added.id,
          targetName: added.name,
          at: new Date(atMs).toISOString(),
          decisionIndex: last?.decisionIndex ?? 0,
          message: `${added.name} added; the night was re-planned around it.`,
        }
      } finally {
        setBusy(null)
      }
    },
    [qc, sessionId],
  )

  return { add, busy }
}
