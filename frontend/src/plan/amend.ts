/**
 * Adding an object to the night, from the sky.
 *
 * The server does the planning (see `api/amend.py`): on a night still
 * happening it re-plans from now, on a night that is over from the cursor,
 * and the past stays locked either way. This module only asks, then refreshes
 * everything the answer changed -- the session (new decision point, new target
 * list), every cached plan and grid (a replay re-solves the rest of the night),
 * and the geometry (the new target's row).
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import type { components } from '../api/schema'

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

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
  } catch {
    // fall through to the status line
  }
  return res.statusText || `HTTP ${res.status}`
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
export function useAddTarget(sessionId: string | null) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)

  const add = useCallback(
    async (target: string, atMs: number, own?: OwnCoordinates): Promise<AmendResult> => {
      if (!sessionId) throw new Error('no session')
      setBusy(target)
      try {
        const res = await fetch(`/api/sessions/${sessionId}/targets`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ target, at: new Date(atMs).toISOString(), ...own }),
        })
        if (!res.ok) throw new Error(await readError(res))
        const out = (await res.json()) as AmendResult
        await Promise.all(
          ['session', 'plan', 'grid', 'geometry'].map((k) =>
            qc.invalidateQueries({ queryKey: [k, sessionId] }),
          ),
        )
        return out
      } finally {
        setBusy(null)
      }
    },
    [qc, sessionId],
  )

  return { add, busy }
}
