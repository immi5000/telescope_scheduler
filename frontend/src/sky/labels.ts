/**
 * Labels, as DOM, placed greedily so they never pile up.
 *
 * DOM rather than text in WebGL: crisp at any pixel ratio, real fonts, and no
 * runtime font fetch (a label that silently fails to load at a dark site with
 * no signal is worse than none). Imperative rather than React, because the set
 * changes every frame -- a satellite rises, a star crosses the limit as you
 * zoom -- and a reconciler diffing a hundred nodes per frame buys nothing.
 *
 * Placement is one greedy pass in priority order. A label whose box would
 * overlap one already placed is hidden, not nudged: nudged labels drift away
 * from their objects and then name the wrong star.
 */

export type LabelKind =
  | 'target'
  | 'target-active'
  | 'planet'
  | 'moon'
  | 'sun'
  | 'star'
  | 'satellite'
  | 'constellation'
  | 'cardinal'
  | 'grid'
  | 'catalog'

export interface LabelCandidate {
  id: string
  text: string
  kind: LabelKind
  /** Anchor in CSS pixels: the object's centre. */
  x: number
  y: number
  /** Drawn radius of the object, so the label clears it. */
  r: number
  /** Lower goes first. */
  priority: number
  /** Optional CSS colour, e.g. a star's own tint. */
  color?: string
  /** Centre the label ON the anchor instead of above it. */
  centred?: boolean
}

/** Rough glyph widths per kind, px per character, for the overlap test. */
const CHAR_W: Record<LabelKind, number> = {
  target: 7,
  'target-active': 7.4,
  planet: 7.8,
  moon: 7.8,
  sun: 7.8,
  star: 7,
  satellite: 6.4,
  constellation: 7.6,
  cardinal: 14,
  grid: 6,
  catalog: 6.4,
}

interface Placed {
  x0: number
  y0: number
  x1: number
  y1: number
}

export class LabelLayer {
  private readonly nodes = new Map<string, HTMLDivElement>()
  private readonly shown = new Set<string>()
  private readonly placed: Placed[] = []

  constructor(private readonly host: HTMLElement) {}

  update(candidates: LabelCandidate[], width: number, height: number): void {
    candidates.sort((a, b) => a.priority - b.priority)
    this.placed.length = 0
    const next = new Set<string>()

    for (const c of candidates) {
      const w = c.text.length * CHAR_W[c.kind] + 6
      const h = c.kind === 'cardinal' ? 22 : 15
      const cx = c.x
      const cy = c.centred ? c.y : c.y - c.r - 3 - h / 2
      const box = { x0: cx - w / 2, y0: cy - h / 2, x1: cx + w / 2, y1: cy + h / 2 }
      if (box.x1 < 0 || box.x0 > width || box.y1 < 0 || box.y0 > height) continue
      // The compass is never crowded out: it is what everything else is read against.
      if (c.kind !== 'cardinal') {
        if (this.placed.some((p) => box.x0 < p.x1 && box.x1 > p.x0 && box.y0 < p.y1 && box.y1 > p.y0)) {
          continue
        }
      }
      this.placed.push(box)
      next.add(c.id)
      const el = this.node(c)
      el.style.transform = `translate(${cx.toFixed(1)}px, ${cy.toFixed(1)}px) translate(-50%, -50%)`
      if (el.textContent !== c.text) el.textContent = c.text
      if (c.color && el.style.color !== c.color) el.style.color = c.color
      if (!this.shown.has(c.id)) el.style.opacity = '1'
    }

    for (const id of this.shown) {
      if (!next.has(id)) {
        const el = this.nodes.get(id)
        if (el) el.style.opacity = '0'
      }
    }
    this.shown.clear()
    for (const id of next) this.shown.add(id)
  }

  clear(): void {
    for (const el of this.nodes.values()) el.remove()
    this.nodes.clear()
    this.shown.clear()
  }

  private node(c: LabelCandidate): HTMLDivElement {
    let el = this.nodes.get(c.id)
    if (!el) {
      el = document.createElement('div')
      el.className = `sky-label sky-label--${c.kind}`
      el.dataset.id = c.id
      el.style.opacity = '0'
      this.host.appendChild(el)
      this.nodes.set(c.id, el)
    } else if (!el.classList.contains(`sky-label--${c.kind}`)) {
      el.className = `sky-label sky-label--${c.kind}`
    }
    return el
  }
}
