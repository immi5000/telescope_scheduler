/**
 * The catalogue, ranked for this site, night and rig: a search box over all of
 * it, and tonight's recommendations when the box is empty.
 *
 * Four rules shape this file.
 *
 *  1. The server does the astronomy. Every altitude, hour and time below is a
 *     field of POST /api/targets/tonight, formatted and never derived. The
 *     only thing computed here is ORDER: which rows match a query, and in what
 *     sequence to show them.
 *  2. The catalogue is large (~1,800 objects), so nothing renders it whole.
 *     A query or a type chip narrows it first, then at most MAX_ROWS rows are
 *     drawn. Matching is a plain substring test on the server's `searchText`,
 *     which already carries every name and number with and without spaces --
 *     no alias table lives here.
 *  3. The list never blanks. The request is debounced (the equipment editor
 *     changes it on every keystroke), the previous ranking stays up, dimmed,
 *     while the next is fetched, and an error keeps the last good one.
 *  4. The parent owns the selection. `onChange` is called only for something
 *     the user did; `onRecommended` whenever a fresh ranking arrives, with its
 *     default picks and the request they were ranked for. Until the user acts,
 *     the parent follows those, which is how a new site or night re-picks by
 *     itself.
 */

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ApiError,
  api,
  type TargetSuggestion,
  type Tonight,
  type TonightRequest,
} from '../../api/client'
import { hhmm } from '../../format'
import { DEFAULT_SNR } from './defaults'
import { detailText } from './errors'
import '../../styles/targets.css'

/** The equipment editor changes the request per keystroke; wait for a pause. */
const DEBOUNCE_MS = 400
/** How many of the recommendations become the default selection. */
const N_DEFAULT = 6
/** Fewer feasible picks than this, and the best of the rest pad them out. */
const N_DEFAULT_MIN = 3
/**
 * The default picks' hours are budgeted at this multiple of the ranking's.
 * Those are the best case -- no cloud, no Moon, the quickest moment -- and even
 * a clear night took the solver about 1.23x them (Sh2-155 on a 72ED).
 */
const PLAN_MARGIN = 1.25
/** Rows drawn at once. Past this, narrowing is faster than scrolling. */
const MAX_ROWS = 50
/** The full attribution: creator, changes made, sources. Served as plain text. */
const NOTICE_URL = '/api/catalog/notice'

/** A ranking, with the request that produced it (the SNR goal it assumed). */
type Ranked = { request: TonightRequest; out: Tonight }

/**
 * Type chips. An object may sit under two (a cluster with nebulosity is both);
 * anything whose type is not listed falls under "Other". Chips are built only
 * for groups with something up tonight.
 */
const GROUPS: { key: string; label: string; long: string; types: readonly string[] }[] = [
  { key: 'galaxy', label: 'Galaxies', long: 'Galaxies', types: ['galaxy'] },
  {
    key: 'emission',
    label: 'Emission nebulae',
    long: 'Emission nebulae',
    types: ['emission-nebula', 'cluster-nebula'],
  },
  {
    key: 'planetary',
    label: 'Planetary',
    long: 'Planetary nebulae',
    types: ['planetary-nebula'],
  },
  {
    key: 'cluster',
    label: 'Clusters',
    long: 'Clusters',
    types: ['open-cluster', 'globular-cluster', 'cluster-nebula'],
  },
  {
    key: 'snr',
    label: 'Supernova remnants',
    long: 'Supernova remnants',
    types: ['supernova-remnant'],
  },
  {
    key: 'reflection',
    label: 'Reflection',
    long: 'Reflection nebulae',
    types: ['reflection-nebula'],
  },
]
const KNOWN_TYPES = new Set(GROUPS.flatMap((g) => g.types))

const FRAMING_TONE: Record<string, string> = {
  fits: 'chip--ok',
  tight: '',
  small: '',
  mosaic: 'chip--warn',
}

export function TargetPicker({
  request,
  held,
  selected,
  onChange,
  onRecommended,
}: {
  /** Everything the ranking depends on. A new value refetches. */
  request: TonightRequest
  /**
   * Why the ranking is being held back, or null.
   *
   * The rig is half of the ranking -- hours to the SNR goal, how much of the
   * frame an object fills, how many targets fit in the night -- and while a
   * field of it is empty or rejected the request still carries the LAST
   * accepted rig. Ranking anyway would put a column of hours on screen that
   * belong to a telescope the form has stopped showing. So the list stops,
   * dimmed, and says what it is waiting for.
   */
  held: string | null
  /** The ids currently in the plan. */
  selected: string[]
  /** A user action changed the selection. */
  onChange: (ids: string[]) => void
  /**
   * A fresh ranking arrived; these are its default picks, best first, with the
   * request they were ranked for. The picks are only this night's while that
   * request is still the one on the form.
   */
  onRecommended: (ids: string[], request: TonightRequest, out: Tonight) => void
}) {
  const uid = useId()
  const listId = `${uid}-list`
  const optionId = (i: number) => `${uid}-opt-${i}`

  // -- the ranking ----------------------------------------------------------

  const settled = useDebounced(request, DEBOUNCE_MS)
  const q = useQuery({
    queryKey: ['tonight', settled],
    queryFn: async ({ signal }): Promise<Ranked> => ({
      request: settled,
      out: await api.tonight(settled, signal),
    }),
    enabled: held === null,
    placeholderData: (prev) => prev,
    staleTime: Infinity,
    // A 4xx is an answer about the request, not a hiccup; asking again changes nothing.
    retry: (n, err) => n < 1 && !(err instanceof ApiError && err.status < 500),
  })

  // An error clears `data`; keep the last good ranking on screen, dimmed.
  const [last, setLast] = useState<Ranked | null>(null)
  if (q.data && !q.isPlaceholderData && q.data !== last) setLast(q.data)
  const ranked = q.data ?? last

  // Report each fresh ranking's picks exactly once. Held in a ref so a parent
  // that passes an inline callback cannot turn this into a render loop.
  const reportPicks = useRef(onRecommended)
  useEffect(() => {
    reportPicks.current = onRecommended
  })
  const fresh = q.isPlaceholderData ? undefined : q.data
  useEffect(() => {
    if (fresh) reportPicks.current(defaultPicks(fresh), fresh.request, fresh.out)
  }, [fresh])

  const waiting = held === null && (request !== settled || q.isFetching)
  const problem = q.isError && !waiting && held === null ? problemOf(q.error) : null
  // A held list is shown dimmed for the same reason a fetching one is: what is
  // on it was true of a rig or a night that is no longer the one on the form.
  const stale = !!ranked && (waiting || !!problem || held !== null)

  const index = useMemo(() => (ranked ? buildIndex(ranked) : null), [ranked])

  // -- what the list shows --------------------------------------------------

  const [query, setQuery] = useState('')
  const [mode, setMode] = useState('recommended')
  const [active, setActive] = useState(0)
  const listRef = useRef<HTMLUListElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const scrollToActive = useRef(false)
  // The highlight is drawn only while the search box or the list is in use,
  // so at rest the first row does not look chosen.
  const [typing, setTyping] = useState(false)
  const [pointing, setPointing] = useState(false)
  const engaged = typing || pointing

  const view = useMemo(() => (index ? viewOf(index, query, mode) : null), [index, query, mode])
  const rows = view?.items.slice(0, MAX_ROWS) ?? []
  const act = rows.length ? Math.min(active, rows.length - 1) : -1
  const focus = act >= 0 ? rows[act] : undefined

  useEffect(() => {
    if (!scrollToActive.current || act < 0) return
    scrollToActive.current = false
    document.getElementById(optionId(act))?.scrollIntoView({ block: 'nearest' })
  })

  const resetList = () => {
    setActive(0)
    if (listRef.current) listRef.current.scrollTop = 0
  }
  const changeQuery = (v: string) => {
    setQuery(v)
    resetList()
  }
  const changeMode = (m: string) => {
    setMode(m)
    resetList()
  }

  // -- the selection --------------------------------------------------------

  const selectedSet = useMemo(() => new Set(selected), [selected])
  /**
   * Whether a target may be PUT IN THE LIST -- which is not the same question
   * as whether tonight can image it. Anything the exposure model can price is
   * selectable, including an object that never rises from this site: the plan
   * then says so in its own words, with the numbers, instead of the picker
   * silently refusing the click and leaving the observer to guess why.
   *
   * `plannable` stays a real gate. A double star, an asterism and a dark
   * nebula have no surface brightness to expose for, so there is no SNR to aim
   * at and nothing for the scheduler to be right or wrong about; the server
   * refuses them with a reason and offers raDeg/decDeg/magnitude as the way to
   * plan one anyway.
   */
  const addable = (t: TargetSuggestion) => t.plannable
  /** Selectable, but tonight cannot image it. Warned about, never blocked. */
  const wontImage = (t: TargetSuggestion) => !t.visible || !t.plannable

  const toggle = (t: TargetSuggestion) => {
    if (selectedSet.has(t.id)) onChange(selected.filter((id) => id !== t.id))
    else if (addable(t)) onChange([...selected, t.id])
  }

  const picks = index?.picks ?? []
  const onPicks = picks.length > 0 && sameIds(picks, selected)
  const lost = index
    ? selected.filter((id) => {
        const t = index.byId.get(id)
        return !t || wontImage(t)
      })
    : []

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    const n = rows.length
    const key = e.key
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      if (!n) return
      e.preventDefault()
      scrollToActive.current = true
      setActive(key === 'ArrowDown' ? Math.min(act + 1, n - 1) : Math.max(act - 1, 0))
    } else if (key === 'Enter') {
      if (!focus) return
      e.preventDefault()
      toggle(focus)
      // Keep the query on screen, showing the new state, but let the next
      // keystroke replace it: "m31 ⏎ m42 ⏎" adds both.
      e.currentTarget.select()
    } else if (key === 'Escape') {
      if (!query) return
      e.preventDefault()
      e.stopPropagation()
      changeQuery('')
    }
  }

  const out = ranked?.out
  // The goal the shown ranking assumed; before the first one, the one asked for.
  const goal = fmtGoal((ranked?.request ?? request).snrGoal ?? DEFAULT_SNR)

  return (
    <div className={`tp${stale ? ' tp--stale' : ''}`}>
      {/* -- what is in the plan ------------------------------------------ */}
      <div className="tp__picked">
        <div className="tp__chips" role="group" aria-label="Selected targets">
          {selected.length === 0 ? (
            <span className="tp__none">
              {index ? 'Nothing selected' : (held ?? 'Ranking the sky…')}
            </span>
          ) : (
            selected.map((id) => {
              const t = index?.byId.get(id)
              const bad = !!index && (!t || wontImage(t))
              const why = dropReason(t)
              return (
                <button
                  key={id}
                  type="button"
                  className={`target-toggle tp-pick${bad ? ' tp-pick--warn' : ''}`}
                  aria-pressed="true"
                  title={
                    bad
                      ? `${t?.name ?? id}: ${why}. It will be dropped from the plan. Click to remove.`
                      : `${t?.designation ?? id}. Click to remove.`
                  }
                  onClick={() => onChange(selected.filter((x) => x !== id))}
                >
                  {bad && (
                    <span className="tp-pick__warn" aria-hidden="true">
                      !
                    </span>
                  )}
                  <span className="tp-pick__name">{t?.name ?? id}</span>
                  <span className="tp-pick__x" aria-hidden="true">
                    ×
                  </span>
                </button>
              )
            })
          )}
        </div>
        <div className="tp__actions">
          {picks.length > 0 && !onPicks && (
            <button type="button" className="tp-link" onClick={() => onChange(picks)}>
              Use tonight's picks
            </button>
          )}
          {selected.length > 0 && (
            <button type="button" className="tp-link" onClick={() => onChange([])}>
              Clear
            </button>
          )}
        </div>
      </div>

      {/* The reasons are written out, not left to a hover: on a touch screen
          the chip's only action is to remove it. */}
      {lost.length > 0 && (
        <div className="tp__lost" role="status">
          <span aria-hidden="true">!</span>{' '}
          {lost.length === 1
            ? '1 selected target cannot be imaged on this night and will be dropped:'
            : `${lost.length} selected targets cannot be imaged on this night and will be dropped:`}
          <ul className="tp__lost-list">
            {lost.map((id) => {
              const t = index?.byId.get(id)
              return (
                <li key={id}>
                  {t?.name ?? id}: {dropReason(t)}
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {/* -- search ------------------------------------------------------- */}
      <div className="tp__search">
        <span className="tp__search-icon" aria-hidden="true">
          ⌕
        </span>
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={act >= 0 ? optionId(act) : undefined}
          aria-label="Search the catalogue"
          placeholder="Search M31, NGC 7000, Heart Nebula, galaxy, Cygnus…"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => changeQuery(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => setTyping(true)}
          onBlur={() => setTyping(false)}
        />
        {query && (
          <button
            type="button"
            className="tp__clear"
            aria-label="Clear the search"
            title="Clear (Esc)"
            onClick={() => {
              changeQuery('')
              inputRef.current?.focus()
            }}
          >
            ×
          </button>
        )}
      </div>

      {/* -- the night, in one line --------------------------------------- */}
      <div className="tp__summary">
        <span className="tp__summary-main num">
          {out ? (
            <>
              <b>{fmtInt(out.visibleCount)}</b> of {fmtInt(out.catalogSize)} objects are up
              tonight · Moon {Math.round(out.moonIllumination * 100)}% lit
              {out.moonUpHours > 0
                ? `, up ${fmtHours(out.moonUpHours)}`
                : ', below the horizon throughout'}{' '}
              ·{' '}
              <span title="Hours with the Sun at least 18° down (12° when it never gets that low; the note below says so). Up counts from 12° down, so it can be longer.">
                {out.darkHours > 0 ? `${fmtHours(out.darkHours)} dark` : 'never dark'}
              </span>
            </>
          ) : (
            ' '
          )}
        </span>
        <span className="tp__status" aria-live="polite">
          {held ?? (waiting && ranked ? 'updating…' : '')}
        </span>
      </div>

      {problem && (
        <div className={`tp__problem tp__problem--${problem.tone}`} role="alert">
          <span className="tp__problem-text">{problem.text}</span>
          {ranked && <span className="tp__problem-aside">Showing the last good ranking.</span>}
          {problem.tone === 'warn' && (
            <button type="button" className="tp-link" onClick={() => void q.refetch()}>
              Try again
            </button>
          )}
        </div>
      )}

      {/* -- browse: recommendations and type chips ----------------------- */}
      {index && view && (
        <div
          className="tp__filters"
          role={view.kind === 'search' ? 'status' : 'group'}
          aria-label={view.kind === 'search' ? undefined : 'Browse by type'}
        >
          {view.kind === 'search' ? (
            <span className="tp__hint num">
              {view.items.length === 0
                ? 'No matches'
                : `${fmtInt(view.items.length)} ${view.items.length === 1 ? 'match' : 'matches'}, ${fmtInt(view.up)} up tonight`}
            </span>
          ) : (
            <>
              <FilterChip
                label="Recommended"
                n={index.recommended.length}
                on={view.mode === 'recommended'}
                onClick={() => changeMode('recommended')}
              />
              <FilterChip
                label="All"
                n={index.up.length}
                on={view.mode === 'all'}
                onClick={() => changeMode('all')}
              />
              {index.groups.map((g) => (
                <FilterChip
                  key={g.key}
                  label={g.label}
                  n={g.items.length}
                  on={view.mode === g.key}
                  onClick={() => changeMode(g.key)}
                />
              ))}
            </>
          )}
        </div>
      )}

      {/* -- the list ----------------------------------------------------- */}
      <ul
        ref={listRef}
        id={listId}
        className="tp__list"
        role="listbox"
        aria-multiselectable="true"
        aria-label="Catalogue objects"
        aria-busy={waiting}
        onMouseEnter={() => setPointing(true)}
        onMouseLeave={() => setPointing(false)}
      >
        <li className="tp-row tp-row--head" role="presentation" aria-hidden="true">
          <span />
          <span className="tp-row__title">{view?.title ?? 'Tonight'}</span>
          <span
            className="tp-r"
            title="Hours above the site's floor with the Sun at least 12° down, clear of the Moon"
          >
            Up
          </span>
          <span
            className="tp-r"
            title="Highest altitude while usable (dark, above the floor and clear of the Moon), and when (UTC)"
          >
            Highest
          </span>
          <span title="Against this rig's field of view">Frame</span>
          <span title="Imaging hours to the SNR goal, downloads included, at the quickest usable moment tonight, from this site's sky and any twilight then. Ignores cloud and moonlight: the best case.">
            SNR {goal} · best case
          </span>
        </li>

        {!index ? (
          <li className="tp__empty" role="presentation">
            {problem ? (
              'No ranking yet.'
            ) : held !== null ? (
              held
            ) : (
              <span className="tp__pulse">Ranking the sky…</span>
            )}
          </li>
        ) : rows.length === 0 ? (
          <li className="tp__empty" role="presentation">
            {view?.kind === 'search'
              ? `Nothing in the catalogue matches “${query.trim()}”.`
              : 'Nothing in the catalogue is up in this window from here.'}
          </li>
        ) : (
          rows.map((t, i) => (
            <Row
              key={t.id}
              id={optionId(i)}
              t={t}
              goal={goal}
              on={selectedSet.has(t.id)}
              active={engaged && i === act}
              onHover={() => i !== act && setActive(i)}
              onToggle={() => toggle(t)}
            />
          ))
        )}

        {view && view.items.length > MAX_ROWS && (
          <li className="tp__more" role="presentation">
            {MAX_ROWS} of {fmtInt(view.items.length)}
          </li>
        )}
      </ul>

      {/* -- the highlighted row, in full --------------------------------- */}
      <div className="tp__detail" aria-live="off">
        {focus ? <Detail t={focus} /> : null}
      </div>

      {/*
        The catalogue is OpenNGC, adapted, under CC BY-SA 4.0, and the licence
        asks for the creator, the licence and the fact of modification wherever
        the material is shown -- which is HERE, not on some later screen: this
        list is eighteen hundred of its objects, and a user may never leave it.
        4.0 3(a)(2) lets all of that be satisfied by a link, so it is one link
        and six words rather than the paragraph that used to sit here.
      */}
      {out && (
        <a className="tp__attrib" href={NOTICE_URL} target="_blank" rel="noopener">
          OpenNGC &middot; CC BY-SA 4.0
        </a>
      )}

    </div>
  )
}

// --------------------------------------------------------------------------
// rows
// --------------------------------------------------------------------------

function Row({
  id,
  t,
  goal,
  on,
  active,
  onHover,
  onToggle,
}: {
  id: string
  t: TargetSuggestion
  goal: string
  on: boolean
  active: boolean
  onHover: () => void
  onToggle: () => void
}) {
  // `available` decides what the row SHOWS: an object that is not up tonight
  // has no hours, no best altitude and no time-to-goal to put in those
  // columns, so it shows the reason instead. `canToggle` decides what the row
  // DOES, and the two part company deliberately -- not being up tonight is a
  // fact worth stating, not grounds for refusing the click.
  const available = t.visible && t.plannable
  const canToggle = t.plannable || on
  const cls = [
    'tp-row',
    on && 'tp-row--on',
    active && 'tp-row--active',
    !available && 'tp-row--off',
    !canToggle && 'tp-row--locked',
  ]
    .filter(Boolean)
    .join(' ')
  const others = otherDesignations(t)
  const meta = [t.typeLabel, t.constellation, size(t), t.vMag != null ? `V ${t.vMag.toFixed(1)}` : '']
    .filter(Boolean)
    .join(' · ')

  return (
    <li
      id={id}
      role="option"
      aria-selected={on}
      aria-disabled={!canToggle}
      className={cls}
      title={tooltip(t)}
      onMouseMove={onHover}
      // Keep focus in the search box, so the keyboard carries on from a click.
      onMouseDown={(e) => e.preventDefault()}
      onClick={canToggle ? onToggle : undefined}
    >
      <span className="tp-row__tick" aria-hidden="true">
        {on ? (available ? '✓' : '!') : canToggle ? '+' : '·'}
      </span>

      <span className="tp-row__id">
        <span className="tp-row__line">
          <span className="tp-row__name">{t.name}</span>
          {others && <span className="tp-row__desig">{others}</span>}
        </span>
        <span className="tp-row__meta">{meta}</span>
      </span>

      {available ? (
        <>
          <span className="tp-row__up tp-r num">{fmtHours(t.usableHours)}</span>
          <span
            className="tp-row__alt tp-r num"
            title={t.bestAltitudeDeg == null ? 'No peak altitude in the catalogue' : undefined}
          >
            {t.bestAltitudeDeg != null && `${Math.round(t.bestAltitudeDeg)}°`}
            {t.bestAt && <span className="tp-row__at"> {hhmm(t.bestAt)} UTC</span>}
          </span>
          <span className="tp-row__frame">
            <Framing t={t} />
          </span>
          <span className="tp-row__goal num">
            <Goal t={t} goal={goal} />
          </span>
        </>
      ) : (
        <span className="tp-row__why">{t.whyNot ?? (t.visible ? 'cannot be planned' : 'not up tonight')}</span>
      )}
    </li>
  )
}

function Framing({ t }: { t: TargetSuggestion }) {
  const fill = t.framingFill
  if (fill == null)
    return (
      <span className="tp-row__none" title="No catalogued size">
        no size
      </span>
    )
  const title =
    fill >= 1
      ? `${fill.toFixed(1)}× the field's short side`
      : `${Math.round(fill * 100)}% of the field's short side`
  return (
    <span className={`chip ${FRAMING_TONE[t.framing] ?? ''}`} title={title}>
      {t.framing}
    </span>
  )
}

function Goal({ t, goal }: { t: TargetSuggestion; goal: string }) {
  if (t.hoursToGoal == null) return <span className="tp-goal tp-goal--none">no estimate</span>
  if (t.feasible)
    return (
      <span className="tp-goal tp-goal--ok">
        {t.hoursToGoal < 1 / 60 ? '' : '≈'}
        {fmtDuration(t.hoursToGoal)} to SNR {goal}
      </span>
    )
  return (
    <span className="tp-goal tp-goal--warn">
      needs ~{fmtDuration(t.hoursToGoal)}, only {fmtHours(t.usableHours)} up
    </span>
  )
}

/** The highlighted row, with every reason the server gave for it. */
function Detail({ t }: { t: TargetSuggestion }) {
  const others = otherDesignations(t)
  const extra: string[] = []
  if (t.usableFrom && t.usableUntil)
    extra.push(`usable ${hhmm(t.usableFrom)}–${hhmm(t.usableUntil)} UTC`)
  if (t.moonSeparationDeg != null) extra.push(`Moon ${Math.round(t.moonSeparationDeg)}° away`)
  // The dark-sky peak, only where it is not the usable best already given:
  // the two differ only when the Moon is too close at the peak.
  const peak = Math.round(t.maxAltitudeDeg)
  if (t.bestAltitudeDeg != null && peak !== Math.round(t.bestAltitudeDeg))
    extra.push(`dark-sky peak ${peak}°, too near the Moon`)
  const parts = [...(t.whyNot ? [t.whyNot] : []), ...t.reasons, ...extra]
  return (
    <>
      <span className="tp__detail-name">{t.name}</span>
      {others && <span className="tp__detail-desig"> {others}</span>}
      {parts.length > 0 && <span className="tp__detail-text">: {parts.join(' · ')}</span>}
    </>
  )
}

function FilterChip({
  label,
  n,
  on,
  onClick,
}: {
  label: string
  n: number
  on: boolean
  onClick: () => void
}) {
  return (
    <button type="button" className="target-toggle tp-filter" aria-pressed={on} onClick={onClick}>
      {label}
      <span className="tp-filter__n num">{fmtInt(n)}</span>
    </button>
  )
}

// --------------------------------------------------------------------------
// ordering and matching -- the only logic here that is not formatting
// --------------------------------------------------------------------------

interface Entry {
  t: TargetSuggestion
  /** Server order, the tie-break of last resort. */
  i: number
  text: string
  /** Id and every designation, with and without spaces: an exact hit. */
  keys: Set<string>
  /** Name and designations: a prefix hit. */
  heads: string[]
  available: boolean
}

interface Index {
  byId: Map<string, TargetSuggestion>
  entries: Entry[]
  recommended: TargetSuggestion[]
  /** Visible and plannable, best first. */
  up: TargetSuggestion[]
  groups: { key: string; label: string; long: string; items: TargetSuggestion[] }[]
  picks: string[]
}

const normalise = (s: string) => s.toLowerCase().trim().replace(/\s+/g, ' ')
const compact = (s: string) => s.replace(/ /g, '')

function buildIndex(ranked: Ranked): Index {
  const out = ranked.out
  const byId = new Map(out.targets.map((t) => [t.id, t]))
  const entries = out.targets.map((t, i): Entry => {
    const desig = t.designation.split('·').map(normalise).filter(Boolean)
    const name = normalise(t.name)
    return {
      t,
      i,
      text: t.searchText.toLowerCase(),
      keys: new Set([t.id.toLowerCase(), ...desig, ...desig.map(compact)]),
      heads: [name, ...desig, ...desig.map(compact)],
      available: t.visible && t.plannable,
    }
  })
  const up = out.targets
    .filter((t) => t.visible && t.plannable)
    .sort((a, b) => b.score - a.score)
  const groups = [
    ...GROUPS.map((g) => ({
      key: g.key,
      label: g.label,
      long: g.long,
      items: up.filter((t) => g.types.includes(t.type)),
    })),
    {
      key: 'other',
      label: 'Other',
      long: 'Other objects',
      items: up.filter((t) => !KNOWN_TYPES.has(t.type)),
    },
  ].filter((g) => g.items.length > 0)
  const recommended = out.recommendedIds
    .map((id) => byId.get(id))
    .filter((t): t is TargetSuggestion => !!t)
  return { byId, entries, recommended, up, groups, picks: defaultPicks(ranked) }
}

interface View {
  kind: 'search' | 'browse'
  /** The effective chip: a type chip with nothing up on this night falls back. */
  mode: string
  /** What the list is, for its header. */
  title: string
  items: TargetSuggestion[]
  /** How many of `items` can be added. */
  up: number
}

function viewOf(index: Index, raw: string, mode: string): View {
  const q = normalise(raw)
  if (q) {
    const items = search(index, q)
    const up = items.filter((t) => t.visible && t.plannable).length
    return { kind: 'search', mode, title: 'Matches, best first', items, up }
  }
  const all = (title: string, items: TargetSuggestion[]): View => ({
    kind: 'browse',
    mode,
    title,
    items,
    up: items.length,
  })
  if (mode === 'all') return all('Everything up tonight, best first', index.up)
  // A chip for a type with nothing up on this night no longer exists.
  const group = index.groups.find((g) => g.key === mode)
  if (group) return all(`${group.long} up tonight, best first`, group.items)
  return { ...all('Recommended tonight', index.recommended), mode: 'recommended' }
}

/**
 * Substring matches on `searchText`, then ranked: an exact id or designation,
 * then a name or designation that starts with the query, then everything up
 * tonight by score, then the rest in the server's order. A multi-word query
 * that matches nothing as a phrase ("galaxy cygnus") falls back to requiring
 * every word.
 */
function search(index: Index, q: string): TargetSuggestion[] {
  const qc = compact(q)
  let hits = index.entries.filter((e) => e.text.includes(q) || e.text.includes(qc))
  if (hits.length === 0 && q.includes(' ')) {
    const words = q.split(' ')
    hits = index.entries.filter((e) => words.every((w) => e.text.includes(w)))
  }
  const tier = (e: Entry) =>
    e.keys.has(q) || e.keys.has(qc)
      ? 0
      : e.heads.some((h) => h.startsWith(q) || h.startsWith(qc))
        ? 1
        : e.available
          ? 2
          : 3
  return hits
    .map((e) => ({ e, tier: tier(e) }))
    .sort(
      (a, b) =>
        a.tier - b.tier ||
        Number(b.e.available) - Number(a.e.available) ||
        b.e.t.score - a.e.t.score ||
        a.e.i - b.e.i,
    )
    .map((x) => x.e.t)
}

/**
 * The default selection, in the server's order: up to N_DEFAULT
 * recommendations that are up, plannable and can reach the SNR goal tonight.
 * A target that cannot finish only takes time from one that can, so the
 * rest are used just to reach N_DEFAULT_MIN on a short or bright night.
 *
 * And only as many as the night holds: each pick costs PLAN_MARGIN times its
 * hours to the goal plus one target change, and picks stop once the next
 * would take the total past the dark hours. Six picks of four hours each on a
 * small refractor is a plan that drops five. The first is always kept.
 */
function defaultPicks({ request, out }: Ranked): string[] {
  const byId = new Map(out.targets.map((t) => [t.id, t]))
  const ok = out.recommendedIds
    .map((id) => byId.get(id))
    .filter((t): t is TargetSuggestion => !!t && t.visible && t.plannable)
  const feasible = ok.filter((t) => t.feasible).slice(0, N_DEFAULT)
  const pad = ok.filter((t) => !t.feasible).slice(0, Math.max(0, N_DEFAULT_MIN - feasible.length))
  const switchHours = (request.equipment?.mount?.switchMinutes ?? 0) / 60
  const chosen = new Set<string>()
  let hours = 0
  // Feasible first: a padding pick must not crowd out one that can finish.
  for (const t of [...feasible, ...pad]) {
    // No estimate: it can take at most the hours it is up.
    hours += PLAN_MARGIN * (t.hoursToGoal ?? t.usableHours) + switchHours
    if (chosen.size > 0 && hours > out.darkHours) break
    chosen.add(t.id)
  }
  return ok.filter((t) => chosen.has(t.id)).map((t) => t.id)
}

/** Why a selected target will be dropped, in the server's words where it gave any. */
const dropReason = (t: TargetSuggestion | undefined): string =>
  !t ? 'not in the catalogue' : (t.whyNot ?? (t.visible ? 'cannot be planned' : 'not up tonight'))

const sameIds = (a: string[], b: string[]) =>
  a.length === b.length && a.every((id, i) => id === b[i])

// --------------------------------------------------------------------------
// formatting
// --------------------------------------------------------------------------

const fmtInt = (n: number) => n.toLocaleString('en-US')

function fmtHours(h: number): string {
  if (h >= 100) return `${fmtInt(Math.round(h))} h`
  if (h >= 10) return `${h.toFixed(0)} h`
  return `${h.toFixed(1)} h`
}

/** An exposure time, as the server's own reasons phrase one: minutes under an hour. */
function fmtDuration(h: number): string {
  if (h < 1 / 60) return 'under a minute'
  if (h < 1) return `${Math.round(h * 60)} min`
  return fmtHours(h)
}

const fmtGoal = (g: number) => (Number.isInteger(g) ? String(g) : g.toFixed(1))

function fmtArcmin(x: number): string {
  const s = x >= 10 ? x.toFixed(0) : x.toFixed(1)
  return `${s.replace(/\.0$/, '')}′`
}

function size(t: TargetSuggestion): string {
  if (t.majorArcmin == null) return ''
  if (t.minorArcmin == null || Math.abs(t.minorArcmin - t.majorArcmin) < 0.05)
    return fmtArcmin(t.majorArcmin)
  return `${fmtArcmin(t.majorArcmin)}×${fmtArcmin(t.minorArcmin)}`
}

/** The designations that are not already the name: "M 3 · NGC 5272" under "M 3". */
function otherDesignations(t: TargetSuggestion): string {
  const name = normalise(t.name)
  return t.designation
    .split('·')
    .map((d) => d.trim())
    .filter((d) => d && normalise(d) !== name)
    .join(' · ')
}

function tooltip(t: TargetSuggestion): string {
  const others = otherDesignations(t)
  const lines = [others ? `${t.name} (${others})` : t.name]
  if (t.whyNot) lines.push(t.whyNot)
  lines.push(...t.reasons)
  return lines.join('\n')
}

// --------------------------------------------------------------------------
// plumbing
// --------------------------------------------------------------------------

function useDebounced<T>(v: T, ms: number): T {
  const [settled, setSettled] = useState(v)
  useEffect(() => {
    const t = window.setTimeout(() => setSettled(v), ms)
    return () => window.clearTimeout(t)
  }, [v, ms])
  return settled
}

function problemOf(err: unknown): { tone: 'crit' | 'warn'; text: string } {
  if (err instanceof ApiError && err.status < 500)
    return { tone: 'crit', text: `Cannot rank this night: ${detailText(err.message)}` }
  if (err instanceof ApiError)
    return { tone: 'warn', text: `The server could not rank the sky (HTTP ${err.status}).` }
  return { tone: 'warn', text: 'Could not reach the server to rank the sky.' }
}
