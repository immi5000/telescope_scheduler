/**
 * Adding an object to the night, from the sky.
 *
 * There is no session on the server to amend, so this sends the request the
 * night was folded from and names the object separately, in `add`. The server
 * then folds the night WITHOUT it and adds it to the folded result -- which is
 * not the same thing as folding a night that had it all along, and the
 * difference is the whole point:
 *
 * **An object added at 02:00 may only be given time after 02:00.** Every slot
 * before the instant it was asked for is locked to the plan already handed
 * over, so the re-plan can extend the night but never rewrite it. This module
 * used to append the object to `targets` and re-fold from dusk, which meant an
 * object asked for at 02:00 could come back scheduled for 22:15 -- an answer
 * about a night that has been and gone.
 *
 * On a night still under way the server holds that instant at the wall clock
 * however far back the cursor has been dragged, because a plan the observer
 * was already given is not a plan to rewrite.
 *
 * What survives verbatim is the contract the old endpoint made, and it is the
 * part worth keeping: an object the optimiser cannot give time to is REFUSED,
 * and the night is left exactly as it was. The refusal now carries the
 * server's own reason -- how many usable minutes are left, or that the object
 * never clears the altitude floor again tonight -- instead of a guess made
 * here from the absence of the target in the answer.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import {
  api,
  ApiError,
  type FullNight,
  type NightStreamRequest,
  type Target,
} from '../api/client'
import type { components } from '../api/schema'
import { applyNight, nightKey, nightRequest } from '../hooks'
import { foldProgress } from './foldProgress'

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

/**
 * `add(target, atMs, label, own)` asks the server to add `target` from `atMs`
 * onward and re-plan. While it runs, `busy` names the target, so the button
 * that asked can say so, and `foldProgress` carries how far along it is under
 * `label` -- what the observer has been reading it called, which is not the
 * catalogue id that travels on the wire.
 *
 * `atMs` is the cursor -- the instant the observer is looking at, which on a
 * night still happening is the present. It is the instant the new object may
 * first be given time at, not merely a label on the answer.
 *
 * `own` carries the object's own coordinates, for the one case the server
 * cannot look anything up: a star, which exists only in the browser's
 * `stars.bin`. Omit it and the server resolves `target` as a name.
 */
export function useAddTarget(sessionId: string | null) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)

  const add = useCallback(
    async (
      target: string,
      atMs: number,
      label: string,
      own?: OwnCoordinates,
    ): Promise<AmendResult> => {
      if (!sessionId) throw new Error('no session')
      const base = nightRequest(sessionId)
      const current = qc.getQueryData<FullNight>(nightKey(sessionId))
      if (!base || !current) throw new Error('this night is no longer loaded')

      setBusy(target)
      const stage = foldProgress.start('add', label, 'planning the night as it stands')
      try {
        const req: NightStreamRequest = {
          ...base,
          // The night as it stands. The object being added is NOT here: it
          // joins through `add`, after the fold, which is what confines it to
          // the time still ahead.
          targets: current.session.targets.map(asTargetRequest),
          add: {
            target,
            at: new Date(atMs).toISOString(),
            name: own?.name ?? null,
            raDeg: own?.raDeg ?? null,
            decDeg: own?.decDeg ?? null,
            magnitude: own?.magnitude ?? null,
            isPointSource: own?.isPointSource ?? false,
          },
        }
        // A refusal arrives as a 409 and is thrown, so nothing below runs and
        // the cache is never touched: the observer keeps the night they had,
        // which is also the night the server still describes.
        const night = await api.nightStream(req, stage)
        if (!night.amend) {
          throw new ApiError(502, `${label} was not added, and the server did not say why.`)
        }

        // What this night would be folded from AGAIN carries the object in
        // `targets`, not in `add`: an amendment applies to a night that
        // already exists, and replaying it onto a fresh fold would ask for the
        // same addition a second time. Re-planning then does what the control
        // that offers it says it does -- folds the whole night from dusk, for
        // every target, added or original -- so the added object competes on
        // its merits rather than keeping the slot the amendment gave it.
        applyNight(
          qc,
          sessionId,
          { ...base, targets: night.session.targets.map(asTargetRequest) },
          night,
        )
        // The id the SERVER minted for this fold names nothing that outlives
        // the response, and `applyNight` has already replaced it on the night
        // itself. The amendment has to agree with it, because the toast it
        // raises is keyed on the same pair the news alert for that decision
        // point is -- and two spellings of the id mean the observer is told
        // twice.
        return { ...night.amend, sessionId }
      } finally {
        foldProgress.finish()
        setBusy(null)
      }
    },
    [qc, sessionId],
  )

  return { add, busy }
}
