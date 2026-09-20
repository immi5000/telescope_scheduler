/**
 * The one React boundary around the imperative sky.
 *
 * React owns the canvas, the label layer and the reticle element, and nothing
 * that moves inside them: the camera, the labels and the reticle are written
 * straight to WebGL or to a node's `transform` every frame, never through
 * state. Pointer input goes straight to the view controller for the same
 * reason -- a drag is sixty updates a second, and none of them should render
 * a component.
 *
 * Interaction, after Stellarium Web:
 *   drag         grab the sky and pull it; the point under the pointer stays put
 *   wheel/pinch  zoom toward the pointer
 *   click        select what is under it (or clear the selection)
 *   double-click centre on it and follow it
 *   arrows, +/-  pan and zoom from the keyboard
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { Geometry, Plan, Satellites, Session, Sky } from '../api/client'
import { loadSkyAssets, type SkyAssets } from './data'
import { describeResidual, validateFrame, type FrameResidual } from './frames/frame'
import type { PlannableSelection, SkyOptions } from './layers'
import {
  SkyRenderer,
  type FrameInfo,
  type ObjectDescription,
  type SceneData,
  type SearchHit,
  type SkySelection,
} from './scene'
import { publishView } from './viewStore'

/** Cached across mounts: the catalogue does not change between sessions. */
let assetsPromise: Promise<SkyAssets> | null = null
const assets = (): Promise<SkyAssets> => (assetsPromise ??= loadSkyAssets())

/** What the rest of the app may ask of the sky. */
export interface SkyApi {
  flyTo(sel: SkySelection, opts?: { follow?: boolean; fovDeg?: number }): void
  follow(sel: SkySelection | null): void
  describe(sel: SkySelection): ObjectDescription | null
  /** Coordinates for an object the server cannot name; null when it can. */
  plannable(sel: SkySelection): PlannableSelection | null
  search(query: string): SearchHit[]
  zoomBy(factor: number): void
}

const CLICK_SLOP_PX = 5

export function SkyScene({
  session,
  geometry,
  plan,
  sky,
  satellites,
  catalog,
  selectedTarget,
  selection,
  onSelect,
  options,
  onLost,
  onReady,
}: {
  /** Absent during setup: the preview sky, with no ground and no plan. */
  session: Session | null
  geometry: Geometry | null
  plan: Plan | undefined
  sky: Sky | undefined
  satellites: Satellites | undefined
  /** Every catalogue object, drawn faint so any of them can be picked and added. */
  catalog: SceneData['catalog']
  selectedTarget: string | null
  selection: SkySelection | null
  onSelect: (sel: SkySelection | null) => void
  options: SkyOptions
  onLost: () => void
  onReady: (api: SkyApi | null) => void
}) {
  const host = useRef<HTMLDivElement | null>(null)
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const labelHost = useRef<HTMLDivElement | null>(null)
  const reticle = useRef<HTMLDivElement | null>(null)
  const renderer = useRef<SkyRenderer | null>(null)
  const [ready, setReady] = useState(false)
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect

  /**
   * Validate the frame BEFORE drawing anything with it. A wrong sky frame
   * renders beautifully, so this is the check the whole scene rests on.
   */
  const residual: FrameResidual = useMemo(
    () =>
      geometry
        ? validateFrame(geometry)
        : { worstDeg: 0, worstTarget: '', worstSlot: -1, samples: 0, ok: true },
    [geometry],
  )
  useEffect(() => {
    if (residual.samples && !residual.ok) {
      console.error(
        `[sky] ${describeResidual(residual)} exceeds tolerance. The sky is being ` +
          'drawn in a rotated frame. Do not trust anything on it.',
      )
    }
  }, [residual])

  // -- create once -----------------------------------------------------------
  useEffect(() => {
    const el = canvas.current
    const labels = labelHost.current
    if (!el || !labels) return
    let r: SkyRenderer
    const onFrame = (info: FrameInfo) => {
      publishView(info.fovDeg, info.following)
      const ret = reticle.current
      if (!ret) return
      if (!info.reticle) {
        ret.style.opacity = '0'
        return
      }
      const size = Math.max(16, info.reticle.r * 2 + 12)
      ret.style.opacity = '1'
      ret.style.width = `${size}px`
      ret.style.height = `${size}px`
      ret.style.transform = `translate(${info.reticle.x}px, ${info.reticle.y}px) translate(-50%, -50%)`
    }
    try {
      r = new SkyRenderer(el, labels, onFrame)
    } catch (err) {
      console.warn('[sky] WebGL unavailable', err)
      onLost()
      return
    }
    renderer.current = r
    r.resize()
    r.start()
    void assets().then((a) => {
      if (renderer.current !== r) return
      r.setAssets(a)
      setReady(true)
    })

    onReady({
      flyTo: (sel, opts) => r.flyTo(sel, opts),
      follow: (sel) => {
        r.view.setFollow(sel ? r.aimFor(sel) : null)
        r.markDirty()
      },
      describe: (sel) => r.describe(sel),
      plannable: (sel) => r.plannable(sel),
      search: (q) => r.search(q),
      zoomBy: (f) => {
        const p = r.projection()
        r.view.zoomAt(f, p.width / 2, p.height / 2, p)
        r.markDirty()
      },
    })

    const ro = new ResizeObserver(() => r.resize())
    if (host.current) ro.observe(host.current)

    // If the context is lost and never comes back, tell the app rather than
    // leave a black rectangle with no explanation. A laptop waking from sleep
    // loses and regains it routinely, so only a loss that outlives the timer
    // counts.
    let lostTimer: ReturnType<typeof setTimeout> | undefined
    const lost = () => {
      lostTimer = setTimeout(() => {
        if (el.getContext('webgl2') === null) onLost()
      }, 4000)
    }
    const restored = () => clearTimeout(lostTimer)
    el.addEventListener('webglcontextlost', lost)
    el.addEventListener('webglcontextrestored', restored)

    return () => {
      ro.disconnect()
      el.removeEventListener('webglcontextlost', lost)
      el.removeEventListener('webglcontextrestored', restored)
      clearTimeout(lostTimer)
      onReady(null)
      r.dispose()
      renderer.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // -- pointer, wheel, keyboard ---------------------------------------------
  useEffect(() => {
    const el = host.current
    if (!el) return
    const pointers = new Map<number, { x: number; y: number }>()
    let down: { x: number; y: number; dragging: boolean } | null = null
    let pinch: { dist: number } | null = null
    let hoverRaf = 0

    const local = (e: PointerEvent | WheelEvent | MouseEvent) => {
      const rect = el.getBoundingClientRect()
      return { x: e.clientX - rect.left, y: e.clientY - rect.top }
    }

    const onDown = (e: PointerEvent) => {
      const r = renderer.current
      if (!r || e.button > 0) return
      el.setPointerCapture(e.pointerId)
      const p = local(e)
      pointers.set(e.pointerId, p)
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()]
        pinch = { dist: Math.hypot(a!.x - b!.x, a!.y - b!.y) }
        r.view.endGrab()
        down = null
        return
      }
      down = { x: p.x, y: p.y, dragging: false }
    }

    const onMove = (e: PointerEvent) => {
      const r = renderer.current
      if (!r) return
      const p = local(e)
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, p)

      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()]
        const dist = Math.hypot(a!.x - b!.x, a!.y - b!.y)
        if (dist > 10 && pinch.dist > 10) {
          const proj = r.projection()
          r.view.zoomAt(dist / pinch.dist, (a!.x + b!.x) / 2, (a!.y + b!.y) / 2, proj)
          pinch.dist = dist
          r.markDirty()
        }
        return
      }
      if (down) {
        if (!down.dragging && Math.hypot(p.x - down.x, p.y - down.y) > CLICK_SLOP_PX) {
          down.dragging = true
          r.view.beginGrab(down.x, down.y, r.projection())
          el.classList.add('stage--grabbing')
        }
        if (down.dragging) {
          r.view.dragTo(p.x, p.y, r.projection())
          r.markDirty()
        }
        return
      }
      // Hover: is there something clickable here? Once per frame at most.
      if (!hoverRaf) {
        hoverRaf = requestAnimationFrame(() => {
          hoverRaf = 0
          el.classList.toggle('stage--pointable', renderer.current?.pick(p.x, p.y) != null)
        })
      }
    }

    const onUp = (e: PointerEvent) => {
      const r = renderer.current
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
      pointers.delete(e.pointerId)
      if (pointers.size < 2) pinch = null
      el.classList.remove('stage--grabbing')
      if (!r || !down) return
      const wasDrag = down.dragging
      down = null
      r.view.endGrab()
      if (!wasDrag && e.type === 'pointerup') {
        const p = local(e)
        onSelectRef.current(r.pick(p.x, p.y))
      }
    }

    const onDouble = (e: MouseEvent) => {
      const r = renderer.current
      if (!r) return
      const p = local(e)
      const sel = r.pick(p.x, p.y)
      if (sel) {
        onSelectRef.current(sel)
        r.flyTo(sel, { follow: true })
      }
    }

    const onWheel = (e: WheelEvent) => {
      const r = renderer.current
      if (!r) return
      e.preventDefault()
      const p = local(e)
      // Lines, pixels or pages: normalise to "notches" of about 100 px.
      const px = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY
      r.view.zoomAt(Math.exp(-px * 0.0018), p.x, p.y, r.projection())
      r.markDirty()
    }

    const onKey = (e: KeyboardEvent) => {
      const r = renderer.current
      if (!r) return
      const s = r.view.state
      const step = Math.max(0.2, s.fovDeg * 0.05)
      switch (e.key) {
        case 'ArrowLeft':
          r.view.set({ azDeg: s.azDeg - step })
          break
        case 'ArrowRight':
          r.view.set({ azDeg: s.azDeg + step })
          break
        case 'ArrowUp':
          r.view.set({ altDeg: s.altDeg + step })
          break
        case 'ArrowDown':
          r.view.set({ altDeg: s.altDeg - step })
          break
        case '+':
        case '=':
          r.view.zoomAt(1.25, el.clientWidth / 2, el.clientHeight / 2, r.projection())
          break
        case '-':
        case '_':
          r.view.zoomAt(0.8, el.clientWidth / 2, el.clientHeight / 2, r.projection())
          break
        case 'Escape':
          onSelectRef.current(null)
          break
        default:
          return
      }
      e.preventDefault()
      r.markDirty()
    }

    el.addEventListener('pointerdown', onDown)
    el.addEventListener('pointermove', onMove)
    el.addEventListener('pointerup', onUp)
    el.addEventListener('pointercancel', onUp)
    el.addEventListener('lostpointercapture', onUp)
    el.addEventListener('dblclick', onDouble)
    el.addEventListener('wheel', onWheel, { passive: false })
    el.addEventListener('keydown', onKey)
    return () => {
      cancelAnimationFrame(hoverRaf)
      el.removeEventListener('pointerdown', onDown)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerup', onUp)
      el.removeEventListener('pointercancel', onUp)
      el.removeEventListener('lostpointercapture', onUp)
      el.removeEventListener('dblclick', onDouble)
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('keydown', onKey)
    }
  }, [])

  // -- feed it -----------------------------------------------------------------
  useEffect(() => {
    renderer.current?.setData(
      session && geometry
        ? { session, geometry, plan, sky, satellites, catalog, selectedTarget }
        : null,
    )
  }, [session, geometry, plan, sky, satellites, catalog, selectedTarget, ready])

  useEffect(() => {
    renderer.current?.setOptions(options)
  }, [options])

  useEffect(() => {
    renderer.current?.setSelection(selection)
  }, [selection])

  // Only what is WRONG or still arriving. Provenance for the vendored sky data
  // travels with it in public/sky/LICENSES.md, which ships in the build; the
  // provenance that CHANGES -- which orbital elements, published when -- is on
  // the satellite's own card. Neither belongs over the sky every night.
  const notes: string[] = []
  if (!ready) notes.push('Loading sky')
  if (satellites && !satellites.available) notes.push('No satellite elements')

  return (
    <div
      ref={host}
      className={`sky-stage${options.focus ? ' sky-stage--focus' : ''}`}
      tabIndex={0}
      aria-label="The night sky. Drag to look around, scroll to zoom, click to identify."
    >
      <canvas ref={canvas} />
      <div ref={labelHost} className="label-layer" aria-hidden />
      <div ref={reticle} className="reticle" aria-hidden>
        <i />
        <i />
        <i />
        <i />
      </div>

      {/*
        Only what is wrong or still arriving. The frame residual used to be
        shown here in development; it is a number about the renderer, not
        about the night, and it is already console.error'd above when it
        exceeds tolerance -- which is the only time anyone needs it.
      */}
      {notes.length > 0 && (
        <div className="skynote">
          {notes.map((n) => (
            <span key={n} className="chip">
              {n}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
