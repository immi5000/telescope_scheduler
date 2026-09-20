/**
 * Where a night begins.
 *
 * A full-screen sheet over an already-live preview sky, so Create Schedule
 * dissolves the form into the dock while the targets and the slew path drop
 * onto a sky you were already looking at, rather than a form, then a spinner,
 * then a sudden globe.
 *
 * THE PAGE IS FOUR LINES AND A LIST -- see `Row` at the foot of this file.
 * Almost nothing here changes between two clear nights at the same backyard
 * with the same rig, so the night states itself in four sentences and every
 * control that writes one sits a click behind it. The target list is the
 * exception, and takes the room the four lines gave up.
 *
 * Two honest limitations, both of them things this file cannot fix alone.
 * Before a session exists there is no backend geometry, so the preview shows
 * stars and the Milky Way but NO HORIZON; it appears the moment the session's
 * frame arrives. And the form does not steer the preview at all: this
 * component fires `onSiteChange` on mount and on every site change, and App
 * passes `onSiteChange={() => undefined}` and mounts the scene with a null
 * session and null geometry. The seam is kept because it is the right seam,
 * not because anything is wired to it -- so do not describe the sky as
 * reacting to this form until it does.
 *
 * Times are entered as local solar time at the SITE, which is what actually
 * governs darkness, and the field says so rather than implying a civil zone.
 *
 * The site is My location or one of the presets, and Use my location is a
 * BUTTON rather than an entry in the list, because ASKING is not choosing a
 * value: it asks the browser, which asks the operating system, which asks the
 * user, and any of the three can say no. An option that silently opened a
 * permission prompt would be a button wearing a list. Once a fix exists the
 * list carries "My location" too -- the list says which site is in force, the
 * button goes and gets a fresh one.
 *
 * Three things keep it honest. Nothing on the form moves until the fix
 * arrives, so a refusal leaves the site exactly where it was instead of
 * labelling the old one as yours. The name and the ground elevation are
 * looked up afterwards, from services allowed to fail -- the row then reads
 * as coordinates, which came from the device and are exact, at sea level, and
 * says so. And the two figures no coordinate can settle are yours to set: the
 * sky brightness, which nothing can measure from here, and the altitude,
 * because a terrain model answers the ground and not the roof you are on.
 * Both are stated in the row and both survive a re-check.
 *
 * Nothing on this page carries a line of guidance under it any more. The
 * labels say what the fields are; the lines under them say what this night,
 * this site and this rig currently are, or why a value cannot be sent.
 *
 * There is no weather input. Weather is a fact about the night, not a choice:
 * the server uses archived model runs for a night that is over and the live
 * forecast otherwise, and the plan's outlook says which and what it found.
 *
 * Create Schedule is never a grey button without a reason: whatever holds it
 * back -- a field the server would reject, a window still loading, picks
 * ranked for a night no longer on the form -- is said right above it.
 */

import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { hashKey, useQuery } from '@tanstack/react-query'
import {
  ApiError,
  api,
  type EquipmentRequest,
  type Presets,
  type SessionRequest,
  type SiteRequest,
  type Tonight,
  type TonightRequest,
} from '../api/client'
import { FoldBar } from './FoldBar'
import { DEFAULT_BORTLE, DEFAULT_SNR } from './setup/defaults'
import { EquipmentEditor, defaultEquipment, type RigOrigin } from './setup/EquipmentEditor'
import { TargetPicker } from './setup/TargetPicker'
import { errorText } from './setup/errors'
import '../styles/setup.css'

/**
 * A manual window, as an absolute instant: a time of day alone is ambiguous
 * across UTC midnight, which falls near dusk for every American site. Hours
 * typed by hand stay put when the start moves; otherwise the window still
 * ends where the automatic one does.
 */
type Override = { startMs: number; hours: number; hoursByHand: boolean }

/**
 * Where the observer actually is, as the device reports it.
 *
 * The coordinates are the whole of it and arrive first; the name and the
 * elevation are looked up afterwards and may never arrive, which is why they
 * are nullable and are never waited for. A site is plannable the instant the
 * browser answers.
 *
 * `bortle` is not measured and cannot be: nothing the device knows says how
 * bright this sky is. It is an assumption the form makes out loud and lets
 * you change, because the alternative -- quietly planning a back garden as
 * though it were the preset's suburb -- is the kind of wrong that shows up
 * only as a plan that never reaches its SNR.
 */
type Here = {
  latitudeDeg: number
  longitudeDeg: number
  accuracyM: number | null
  /** Looked up: the town and its state, or null while unknown. */
  name: string | null
  /**
   * Metres above sea level: from the terrain model, or typed, or null when
   * neither -- in which case the site is planned at sea level and the row
   * says so rather than implying a lookup succeeded.
   */
  elevationM: number | null
  /** Typed. A later fix must not quietly overwrite a figure you gave. */
  elevationByHand: boolean
  bortle: number
  /**
   * Who to credit for the name and the elevation, as the server words it.
   * Null until a lookup answers, and null for ever if none does -- in which
   * case there is nothing on screen that came from anyone to credit.
   */
  attribution: string | null
}

/** The site select's value for the located site, alongside the preset ids. */
const HERE = '__here'

/** What a site with no elevation is planned at. */
const SEA_LEVEL = 0

/**
 * Bortle classes, worded as the scale itself words them. The number alone is
 * jargon and the word alone is vague, so the option carries both.
 */
const BORTLE: readonly [number, string][] = [
  [1, 'pristine'],
  [2, 'truly dark'],
  [3, 'rural'],
  [4, 'rural/suburban'],
  [5, 'suburban'],
  [6, 'bright suburban'],
  [7, 'suburban/urban'],
  [8, 'city'],
  [9, 'inner city'],
]

/**
 * The form as it was last left. Setup unmounts once a session exists, so
 * without this "New night" starts from the defaults and a rig typed in field
 * by field is gone. Module scope, not storage: it lasts as long as the page.
 */
let lastDraft: {
  siteId: string
  here: Here | null
  pickedDate: string | null
  equipment: EquipmentRequest
  origin: RigOrigin | undefined
  targets: string[] | null
  snrGoal: number
  tSubS: number
  slotMinutes: number
  override: Override | null
} | null = null

/** The number fields Setup itself owns; the rig's are the equipment editor's. */
type NumKey = 'hours' | 'snrGoal' | 'tSubS' | 'elevation'

/** SessionRequest's bounds, so a bad value is caught here and not as a 422. */
const LIMITS: Record<NumKey, { label: string; check: (n: number) => string | null }> = {
  hours: { label: 'Hours', check: (n) => (n > 0 && n <= 16 ? null : 'must be > 0 and ≤ 16') },
  snrGoal: { label: 'SNR goal', check: (n) => (n > 0 ? null : 'must be > 0') },
  tSubS: { label: 'Sub-exposure', check: (n) => (n > 0 ? null : 'must be > 0') },
  // `SiteRequest.elevation_m` is unbounded on the wire, so these are the
  // bounds of the Earth rather than the API's: the Dead Sea shore and a
  // little above the summit of Everest.
  elevation: {
    label: 'Altitude',
    check: (n) => (n >= -450 && n <= 9000 ? null : 'must be −450 to 9000 m'),
  },
}

/** Why typed text cannot be sent, or null. */
const whyNotNumber = (k: NumKey, raw: string): string | null => {
  if (raw.trim() === '') return 'required'
  const n = Number(raw)
  return Number.isFinite(n) ? LIMITS[k].check(n) : 'not a number'
}

/**
 * The night an observer at this longitude means by "tonight", as a local date.
 *
 * Not the UTC date: at 19:30 in Illinois it is already tomorrow in UTC, and
 * the default would plan the wrong night at exactly the hour people plan.
 * Local SOLAR time (longitude / 15), the same clock the server anchors a night
 * on. Before 08:00 local the night that began yesterday evening is the one
 * still in progress or just ended, so that is the one meant.
 */
const localNightOf = (longitudeDeg: number, nowMs: number = Date.now()): string =>
  new Date(nowMs + (longitudeDeg / 15 - 8) * 3_600_000).toISOString().slice(0, 10)

/**
 * A coordinate as a person reads one: 4 decimals is about 11 m, which is finer
 * than any phone's fix and far finer than anything the night depends on.
 */
const degrees = (x: number, pos: string, neg: string): string =>
  `${Math.abs(x).toFixed(4)}° ${x < 0 ? neg : pos}`

const coordinates = (h: Here): string =>
  `${degrees(h.latitudeDeg, 'N', 'S')}, ${degrees(h.longitudeDeg, 'E', 'W')}`

/** How far off the fix might be, in the unit that keeps it short. */
const accuracy = (m: number): string =>
  m >= 1000 ? `±${(m / 1000).toFixed(1)} km` : `±${Math.round(m)} m`

/**
 * Where you are, in one line, saying which parts are known and which are not.
 *
 * The coordinates are always there because they always arrived. The name is
 * omitted rather than guessed at; the elevation says what is being assumed in
 * its place; and the sky brightness says "assumed" every time, because it is
 * the one figure here that no device measured and the plan is sensitive to it.
 */
const whereYouAre = (h: Here): string =>
  [
    h.name,
    coordinates(h) + (h.accuracyM === null ? '' : ` ${accuracy(h.accuracyM)}`),
    h.elevationM === null ? 'sea level assumed' : `${Math.round(h.elevationM)} m`,
    `Bortle ${h.bortle}, assumed`,
  ]
    .filter(Boolean)
    .join(' · ')

/** What the altitude field shows: what is known, or sea level. */
const shownElevation = (h: Here): number => h.elevationM ?? SEA_LEVEL

/**
 * What the device refused, in words, and never a code.
 *
 * `PERMISSION_DENIED` is the only one worth acting on, and the action is the
 * browser's, not this form's -- so the sentence says where the switch is
 * rather than offering a button that cannot do anything.
 */
const locationProblem = (err: GeolocationPositionError): string =>
  err.code === err.PERMISSION_DENIED
    ? 'This site is not allowed to see your location; the browser’s address bar can allow it.'
    : err.code === err.TIMEOUT
      ? 'Your device did not fix a position in time.'
      : 'Your device could not work out where it is.'

export function Setup({
  presets,
  busy,
  error,
  onCreate,
  onSiteChange,
}: {
  presets: Presets
  busy: boolean
  error: string | null
  onCreate: (req: SessionRequest) => void
  onSiteChange: (siteId: string) => void
}) {
  const uid = useId()
  const ids = {
    site: `${uid}-site`,
    date: `${uid}-date`,
    start: `${uid}-start`,
    hours: `${uid}-hours`,
    snrGoal: `${uid}-snr`,
    tSubS: `${uid}-tsub`,
    slot: `${uid}-slot`,
    bortle: `${uid}-bortle`,
    elevation: `${uid}-elevation`,
  }

  const [siteId, setSiteId] = useState(() => lastDraft?.siteId ?? presets.sites[0]?.id ?? 'urbana')
  const [here, setHere] = useState<Here | null>(() => lastDraft?.here ?? null)
  const [locating, setLocating] = useState(false)
  const [locateError, setLocateError] = useState<string | null>(null)
  // Which ask is the live one. An answer to any earlier one is dropped -- see
  // `askForLocation`, and `giveUpLocating` for how an ask stops being live.
  const asked = useRef(0)
  const [equipment, setEquipment] = useState<EquipmentRequest>(
    () => lastDraft?.equipment ?? defaultEquipment(presets),
  )
  const [origin, setOrigin] = useState(() => lastDraft?.origin)
  // null until the user picks a date: until then it follows the site.
  const [pickedDate, setDate] = useState(() => lastDraft?.pickedDate ?? null)
  // null until the user picks: until then the selection FOLLOWS the
  // recommendations, so changing the site or the night re-picks for it.
  const [targets, setTargets] = useState(() => lastDraft?.targets ?? null)
  // The newest ranking's picks, with the request it answered. For half a
  // second after a change that is still the previous site's or night's.
  const [ranked, setRanked] = useState<{
    ids: string[]
    request: TonightRequest
    out: Tonight
  } | null>(null)
  const [snrGoal, setSnrGoal] = useState(() => lastDraft?.snrGoal ?? DEFAULT_SNR)
  const [tSubS, setTSubS] = useState(() => lastDraft?.tSubS ?? 90)
  const [slotMinutes, setSlotMinutes] = useState(() => lastDraft?.slotMinutes ?? 5)
  const [override, setOverride] = useState(() => lastDraft?.override ?? null)
  // Number fields' text while typed, as in the equipment editor: "" and "0"
  // stay on screen, marked, and only a valid value reaches the request.
  const [typed, setTyped] = useState<Partial<Record<NumKey, string>>>({})
  const dropTyped = (k: NumKey) =>
    setTyped((d) => {
      if (!(k in d)) return d
      const next = { ...d }
      delete next[k]
      return next
    })
  const [eqInvalid, setEqInvalid] = useState<string[]>([])

  const preset = presets.sites.find((s) => s.id === siteId) ?? presets.sites[0]
  // `here` outlives a switch back to a preset -- the fix is kept so that
  // returning to "My location" in the list does not ask the device all over
  // again -- so it is the SELECTION that decides which site is in force, not
  // whether a fix happens to exist.
  const located = siteId === HERE ? here : null

  /**
   * The site, as the four things everything below actually reads. One shape
   * for both kinds, so nothing downstream has to know which is in force.
   */
  const site = located
    ? {
        name: located.name ?? coordinates(located),
        latitudeDeg: located.latitudeDeg,
        longitudeDeg: located.longitudeDeg,
        elevationM: located.elevationM ?? SEA_LEVEL,
      }
    : {
        name: preset?.name ?? siteId,
        latitudeDeg: preset?.latitudeDeg ?? 0,
        longitudeDeg: preset?.longitudeDeg ?? 0,
        elevationM: preset?.elevationM ?? 0,
      }

  /**
   * The located site as the API takes it, or null while a preset is in force.
   *
   * Sent as `site`, which overrides `siteId` on both endpoints. Everything
   * not measured is left at the server's own default rather than invented
   * here: the floor and the lunar exclusion are the same 30° and 15° every
   * preset uses, and the extinction is the server's 0.20.
   */
  const siteBody: SiteRequest | null = located && {
    name: located.name ?? coordinates(located),
    latitudeDeg: located.latitudeDeg,
    longitudeDeg: located.longitudeDeg,
    elevationM: located.elevationM ?? SEA_LEVEL,
    bortle: located.bortle,
    extinctionK: 0.2,
    minAltitudeDeg: 30,
    minMoonSeparationDeg: 15,
  }

  /** The whole site as one comparable value, for the memo below. */
  const siteKey = siteBody ? JSON.stringify(siteBody) : siteId

  const date = pickedDate ?? localNightOf(site.longitudeDeg)

  useEffect(() => {
    lastDraft = {
      siteId,
      here,
      pickedDate,
      equipment,
      origin,
      targets,
      snrGoal,
      tSubS,
      slotMinutes,
      override,
    }
  })

  useEffect(() => {
    onSiteChange(siteId)
  }, [siteId, onSiteChange])

  // Reset a manual time override whenever the thing it was relative to moves.
  // Not on mount: a restored draft's override belongs to its restored night.
  // Keyed on the COORDINATES, not the selection: "Where I am" keeps its name
  // while the place under it changes, and a window set for a back garden in
  // Illinois is not a window for the same phone in Arizona.
  const night = `${site.latitudeDeg},${site.longitudeDeg} ${date}`
  const nightWas = useRef(night)
  useEffect(() => {
    if (nightWas.current === night) return
    nightWas.current = night
    setOverride(null)
    dropTyped('hours')
  }, [night])

  // Keyed on what the request is actually made of, and never on a site id.
  // Dusk is a function of the coordinates; keying on the id would serve one
  // cached dusk for every place "Where I am" ever stood, for ever, because
  // this answer is immutable and is cached as such.
  const window = useQuery({
    queryKey: [
      'night-window',
      date,
      site.latitudeDeg,
      site.longitudeDeg,
      // To the nearest 10 m. The altitude is typed a digit at a time -- 1,
      // 13, 135, 1350 -- and dusk is the Sun 18 degrees down, which ten
      // metres of height does not move by a measurable second.
      Math.round(site.elevationM / 10) * 10,
    ],
    queryFn: () => api.nightWindow(date, site.latitudeDeg, site.longitudeDeg, site.elevationM),
    // The previous night's answer stays up while the next is fetched, so a
    // keystroke in the altitude field does not blank the dusk line and grey
    // out Create Schedule between one digit and the next.
    placeholderData: (prev) => prev,
    staleTime: Infinity,
  })

  const auto = window.data
  const offset = auto?.utcOffsetHours ?? 0
  const startMs = override ? override.startMs : auto ? Date.parse(auto.suggestedStart) : null
  const startIso = startMs === null ? null : new Date(startMs).toISOString()
  const hours = override ? override.hours : (auto?.suggestedHours ?? 8)

  /** Typed text is kept; only a valid value is applied. */
  const typeNumber = (k: NumKey, raw: string, apply: (n: number) => void) => {
    setTyped((d) => ({ ...d, [k]: raw }))
    if (whyNotNumber(k, raw) === null) apply(Number(raw))
  }
  /** Leaving a valid field tidies it to its value; an invalid one stays marked. */
  const settleNumber = (k: NumKey) => {
    const raw = typed[k]
    if (raw !== undefined && whyNotNumber(k, raw) === null) dropTyped(k)
  }
  const whyNot = (k: NumKey): string | null => {
    const raw = typed[k]
    return raw === undefined ? null : whyNotNumber(k, raw)
  }

  /**
   * Ask the device where it is, and plan from there.
   *
   * The coordinates are the whole answer and the site is complete the moment
   * they land. The name and the elevation are a second, slower question to
   * the server, and are allowed never to be answered: the note falls back to
   * the coordinates, which are exact and came from the device itself.
   *
   * Nothing on the form moves until the fix arrives. A refusal therefore
   * leaves the site exactly where it was, rather than selecting "My location"
   * over a place nobody found and quietly planning the old one underneath it.
   *
   * An ask can also simply never come back. `getCurrentPosition`'s timeout
   * starts when permission is GRANTED, so a prompt the user walks away from
   * calls neither callback, ever. Hence `asked`: the rest of the row stays
   * live throughout, a site chosen in the meantime wins, and the answer to a
   * question that has been superseded is dropped rather than applied over
   * whatever the form says by the time it arrives.
   */
  /**
   * Stop waiting on the device. The ask is not cancellable -- the browser
   * offers no way -- so it is disowned instead: the counter moves on and any
   * answer that turns up later is dropped on arrival.
   */
  const giveUpLocating = () => {
    asked.current++
    setLocating(false)
    setLocateError(null)
  }

  const askForLocation = () => {
    if (!navigator.geolocation) {
      setLocateError('This browser cannot report a location.')
      return
    }
    const ask = ++asked.current
    setLocating(true)
    setLocateError(null)
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (asked.current !== ask) return
        const fix = {
          latitudeDeg: pos.coords.latitude,
          longitudeDeg: pos.coords.longitude,
        }
        // Built from the site as it is NOW, not as it was when the button was
        // pressed: a fix can take fifteen seconds, and the sky class or the
        // altitude set during that wait is the one the observer meant. Figures
        // you gave survive a re-check anyway -- standing up and pressing again
        // does not move you to a new town or a new floor -- and reading `here`
        // out of the closure would quietly undo whichever you set last.
        setHere((prev) => ({
          ...fix,
          accuracyM: Number.isFinite(pos.coords.accuracy) ? pos.coords.accuracy : null,
          name: null,
          elevationM: prev?.elevationByHand ? prev.elevationM : null,
          elevationByHand: prev?.elevationByHand ?? false,
          bortle: prev?.bortle ?? DEFAULT_BORTLE,
          attribution: null,
        }))
        setSiteId(HERE)
        setLocating(false)
        dropTyped('elevation')
        void api
          .locate(fix.latitudeDeg, fix.longitudeDeg)
          .then((found) =>
            // Only onto the fix it was asked about: a second press while this
            // one is in flight must not have its answer painted over. And
            // never over a typed altitude -- see `elevationByHand`.
            setHere((h) =>
              h?.latitudeDeg === fix.latitudeDeg && h.longitudeDeg === fix.longitudeDeg
                ? {
                    ...h,
                    name: found.name ?? null,
                    elevationM: h.elevationByHand ? h.elevationM : (found.elevationM ?? null),
                    // Only where something was actually looked up. Crediting
                    // OpenStreetMap under a row showing nothing but the
                    // device's own coordinates would be a citation for a
                    // claim nobody made.
                    attribution:
                      found.name === null && found.elevationM === null
                        ? null
                        : found.attribution,
                  }
                : h,
            ),
          )
          .catch(() => undefined)
      },
      (err) => {
        if (asked.current !== ask) return
        setLocating(false)
        setLocateError(locationProblem(err))
      },
      // `maximumAge: 0` and not a cached fix, which is what makes "check
      // where I am again" mean anything. The browser will otherwise answer a
      // re-check from a position minutes old -- and minutes old is exactly
      // what it is after the drive out to somewhere dark, which is the one
      // time anybody presses it.
      { enableHighAccuracy: true, timeout: 15_000, maximumAge: 0 },
    )
  }

  /**
   * A typed start. Hours typed by hand are kept; otherwise the window still
   * ends where the automatic one does -- the start moved, dawn did not.
   */
  const typeStart = (time: string) => {
    if (!auto) return
    const start = instantInNight(date, offset, timeToHour(time))
    const byHand = !!override?.hoursByHand || typed.hours !== undefined
    const autoEnd = Date.parse(auto.suggestedStart) + auto.suggestedHours * 3_600_000
    const toDawn = Math.min(16, Math.max(0.5, (autoEnd - start) / 3_600_000))
    setOverride({
      startMs: start,
      hours: byHand ? hours : Math.round(toDawn * 100) / 100,
      hoursByHand: byHand,
    })
  }

  const tonightRequest: TonightRequest = useMemo(
    () => ({
      date,
      // `site` overrides `siteId` wherever both are sent; the id is nulled
      // rather than left behind, so the request says one thing only.
      siteId: siteBody ? null : siteId,
      ...(siteBody ? { site: siteBody } : {}),
      // Required by the generated type; ignored because `equipment` is sent.
      equipmentId: presets.equipment[0]?.id ?? '',
      equipment,
      snrGoal,
      tSubS,
      // Only an explicit override narrows the window; otherwise the server
      // uses its own dusk and dawn, which is the same pair the form shows.
      ...(override
        ? { start: new Date(override.startMs).toISOString(), hours: override.hours }
        : {}),
    }),
    // `siteBody` is a fresh object every render, so it cannot be a dependency;
    // `siteKey` is the same information as a string and stands in for it, and
    // for `siteId`, which it contains.
    [date, siteKey, presets.equipment, equipment, snrGoal, tSubS, override],
  )

  /**
   * Why the ranking below is held back, or null.
   *
   * A blank or rejected rig field never reaches `equipment`, so the request
   * still carries the LAST accepted rig -- which, right after "Custom", is a
   * telescope the form has stopped showing. The hours, the framing and the
   * picks would all be that telescope's. The Rig row names the fields; this
   * only has to say which part of the form the list is waiting on.
   */
  const held = eqInvalid.length > 0 ? 'waiting for the rig' : null

  const following = targets === null
  const selected = targets ?? ranked?.ids ?? []
  // By value: a cached ranking carries its own copy of the request.
  const rankedNow = ranked !== null && hashKey([ranked.request]) === hashKey([tonightRequest])
  // The selection's best case against the night, in the ranking's own hours.
  // Not while the rig is held: the ranking these hours come from was made for
  // a rig the form has stopped showing, and a figure at full strength beside
  // a dimmed list would be the one thing on the page still asserting it.
  const fit =
    rankedNow && held === null && ranked.out.darkHours > 0
      ? {
          hours: ranked.out.targets
            .filter((t) => t.visible && t.hoursToGoal != null && selected.includes(t.id))
            .reduce((h, t) => h + (t.hoursToGoal ?? 0), 0),
          dark: ranked.out.darkHours,
        }
      : null

  // What holds Create back. A field the server would reject is named; a
  // wait says what it is waiting for.
  const invalid = [
    // The altitude only exists for a located site, and only its row can show
    // the complaint. Counting a stale one while a preset is in force would
    // grey Create out over a field that is not on the page to be corrected --
    // the same rule that makes a row with an invalid value force itself open.
    ...(['hours', 'snrGoal', 'tSubS', ...(located ? (['elevation'] as const) : [])] as const).flatMap(
      (k) => {
        const why = whyNot(k)
        return why ? [`${LIMITS[k].label}: ${why}`] : []
      },
    ),
    ...(eqInvalid.length ? [`${eqInvalid.join(', ')} ${eqInvalid.length === 1 ? 'is' : 'are'} not valid`] : []),
    ...(!auto && window.isError ? ["the night's dusk and dawn did not load"] : []),
  ]
  const waiting = !auto
    ? window.isError
      ? null
      : 'finding dusk…'
    : following && !rankedNow
      ? 'ranking the new night…'
      : selected.length === 0
        ? 'no targets selected'
        : null

  const submit = () => {
    if (startIso === null) return
    onCreate({
      name: `${site.name} · night of ${date}`,
      // The server builds the window as this UTC date plus this UTC hour.
      date: startIso.slice(0, 10),
      startHourUtc: utcHour(startIso),
      hours,
      slotMinutes,
      siteId: siteBody ? null : siteId,
      ...(siteBody ? { site: siteBody } : {}),
      equipmentId: presets.equipment[0]?.id ?? '',
      equipment,
      snrGoal,
      tSubS,
      weather: 'auto',
      targets: selected.map((id) => ({ id, priority: 1, urgency: 1 })),
      solveSeconds: 4,
    })
  }

  const dark = auto?.darkHours ?? 0
  const noDarkness = !!auto && auto.dusk === null

  /**
   * The night's window, in one sentence, in whichever of its five states it
   * is in. This is the line an observer reads INSTEAD of opening the drawer,
   * which is what makes the hand-set state load-bearing rather than cosmetic:
   * fold Start and Hours away and describe the automatic dusk-to-dawn night
   * anyway, and the page calmly reports a night it is not going to ask for.
   * The failure has to be able to appear here for the same reason.
   */
  const windowNote =
    !auto && window.isError && !window.isFetching ? (
      <span className="setup__err" role="alert">
        {windowProblem(window.error)}{' '}
        <button type="button" className="setup__link" onClick={() => void window.refetch()}>
          Try again
        </button>
      </span>
    ) : !auto ? (
      window.isFetching || window.isPending ? 'finding dusk…' : 'dusk not known yet'
    ) : override && startIso !== null ? (
      `${localHhmm(startIso, offset)} for ${hours.toFixed(1)} h, set by hand` +
      (noDarkness
        ? ''
        : ` · dusk ${localHhmm(auto.dusk ?? auto.suggestedStart, offset)} to dawn ` +
          `${localHhmm(auto.dawn ?? null, offset)}`)
    ) : noDarkness ? (
      'never fully dark here tonight, so this runs sunset to sunrise'
    ) : (
      `dusk ${localHhmm(auto.dusk ?? auto.suggestedStart, offset)} to dawn ` +
      `${localHhmm(auto.dawn ?? null, offset)} · ${dark.toFixed(1)} h dark`
    )

  return (
    <div className="setup">
      <div className="setup__sheet">
        <h1 className="setup__title">Plan a night</h1>

        <div className="setup__rows">
          <Row
            label="Site"
            labelFor={ids.site}
            summary={
              // The button first, because it is the answer for almost
              // everybody: the night is being planned from where the observer
              // is standing, and a list of four other people's back gardens is
              // the fallback, not the question. A press asks the browser,
              // which asks the operating system, which asks the user -- so it
              // has to be a press and cannot be a default.
              <div className="setup__site">
                <button
                  type="button"
                  className={`setup__locate${located ? ' setup__locate--on' : ''}`}
                  // NOT disabled while asking. A permission prompt that is
                  // dismissed rather than answered calls neither callback,
                  // ever, so a button that greyed itself out for the wait
                  // would grey itself out for good. Pressing again abandons
                  // the outstanding ask and starts a new one.
                  aria-pressed={!!located}
                  aria-busy={locating || undefined}
                  onClick={askForLocation}
                >
                  {locating ? 'Finding you…' : 'Use my location'}
                </button>
                <select
                  id={ids.site}
                  className="setup__pick"
                  // The list says which site is in force, including this one.
                  // Never `HERE` while `here` is null: until the device
                  // answers there is nowhere to select, and the old site is
                  // still the one being planned.
                  // NOT disabled while an ask is outstanding: an unanswered
                  // permission prompt never calls back, and a list that waits
                  // on it would strand the whole page on a dialog the user
                  // dismissed. Choosing here abandons the ask instead.
                  value={located ? HERE : siteId}
                  onChange={(e) => {
                    giveUpLocating()
                    setSiteId(e.target.value)
                  }}
                >
                  {/* Only once a fix exists. The button is the only way to
                      get one, because an option that silently opened a
                      permission prompt would be a button wearing a list --
                      and it is how you come back here afterwards without
                      being asked all over again. */}
                  {here && <option value={HERE}>My location</option>}
                  {presets.sites.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} &middot; Bortle {s.bortle}
                    </option>
                  ))}
                </select>
              </div>
            }
            // What is being planned, always -- and above it, if the last ask
            // failed, why. Never the failure INSTEAD of the site: a refused
            // re-check leaves the previous fix in force, and a row that showed
            // only the refusal would be a page planning a place it had stopped
            // naming.
            note={
              <>
                {locateError && (
                  <span className="setup__err" role="alert">
                    {locateError}
                    <br />
                  </span>
                )}
                {locating
                  ? 'finding where you are…'
                  : located
                    ? whereYouAre(located)
                    : preset && (
                        <>
                          {preset.zenithSkyMagArcsec2.toFixed(1)} mag/&#9633;&Prime; zenith
                          &middot; floor {preset.minAltitudeDeg.toFixed(0)}&deg;
                        </>
                      )}
              </>
            }
            // Only where there is something to set. A preset carries a
            // measured sky and an elevation of its own; a coordinate carries
            // neither, and the sky is the one nobody can measure from here.
            moreLabel="Set the sky and altitude"
            forceOpen={!!located && !!whyNot('elevation')}
            more={
              located ? (
                <div className="setup__grid">
                  <Field id={ids.bortle} label="Sky brightness">
                    <select
                      id={ids.bortle}
                      value={located.bortle}
                      onChange={(e) =>
                        setHere((h) => (h ? { ...h, bortle: Number(e.target.value) } : h))
                      }
                    >
                      {BORTLE.map(([n, word]) => (
                        <option key={n} value={n}>
                          Bortle {n} &middot; {word}
                        </option>
                      ))}
                    </select>
                  </Field>

                  {/* The terrain model fills this in where it can, and it is
                      sea level where it cannot -- but the model answers the
                      ground under the coordinate, which is not a rooftop, a
                      ridge or the pad you actually set up on. So it is a
                      field, and a figure typed here outlives a re-check. */}
                  <Field id={ids.elevation} label="Altitude (m)">
                    <input
                      id={ids.elevation}
                      type="number"
                      min={-450}
                      max={9000}
                      step={10}
                      value={typed.elevation ?? shownElevation(located)}
                      aria-invalid={!!whyNot('elevation') || undefined}
                      aria-describedby={
                        whyNot('elevation') ? `${ids.elevation}-hint` : undefined
                      }
                      onChange={(e) =>
                        typeNumber('elevation', e.target.value, (n) =>
                          setHere((h) => (h ? { ...h, elevationM: n, elevationByHand: true } : h)),
                        )
                      }
                      onBlur={() => settleNumber('elevation')}
                    />
                    <Hint id={`${ids.elevation}-hint`} error={whyNot('elevation')} />
                  </Field>

                  {/* Where the two looked-up figures came from. It lives here
                      rather than in the row because it is a credit, not a
                      reading -- and OpenStreetMap's licence asks for it
                      wherever its data is shown, which is one line above. */}
                  {located.attribution && (
                    <div className="setup__credit">{located.attribution}</div>
                  )}
                </div>
              ) : undefined
            }
          />

          <Row
            label="Night"
            labelFor={ids.date}
            summary={
              <input
                id={ids.date}
                type="date"
                className="setup__pick"
                value={date}
                onChange={(e) => setDate(e.target.value || null)}
              />
            }
            note={windowNote}
            moreLabel="Set the window"
            forceOpen={!!whyNot('hours')}
            more={
              <div className="setup__grid">
                <Field
                  id={ids.start}
                  // The offset is the server's; until it answers there is none to show.
                  label={
                    auto
                      ? `Start (local solar, UTC${offset >= 0 ? '+' : ''}${offset.toFixed(1)})`
                      : 'Start (local solar)'
                  }
                >
                  <input
                    id={ids.start}
                    type="time"
                    disabled={!auto}
                    value={startIso === null ? '' : hourToTime(utcHour(startIso) + offset)}
                    onChange={(e) => e.target.value && typeStart(e.target.value)}
                  />
                </Field>

                <Field id={ids.hours} label="Hours">
                  <input
                    id={ids.hours}
                    type="number"
                    min={0.5}
                    max={16}
                    step={0.5}
                    value={typed.hours ?? hours}
                    disabled={startMs === null}
                    aria-invalid={!!whyNot('hours') || undefined}
                    aria-describedby={whyNot('hours') ? `${ids.hours}-hint` : undefined}
                    onChange={(e) =>
                      typeNumber(
                        'hours',
                        e.target.value,
                        (n) =>
                          startMs !== null && setOverride({ startMs, hours: n, hoursByHand: true }),
                      )
                    }
                    onBlur={() => settleNumber('hours')}
                  />
                  {whyNot('hours') && (
                    <div className="field__hint setup__err" id={`${ids.hours}-hint`} role="status">
                      {whyNot('hours')}
                    </div>
                  )}
                  {(override || typed.hours !== undefined) && (
                    <button
                      type="button"
                      className="field__hint setup__link"
                      onClick={() => {
                        setOverride(null)
                        dropTyped('hours')
                      }}
                    >
                      back to dusk and dawn
                    </button>
                  )}
                </Field>
              </div>
            }
          />

          <Row
            label="Rig"
            summary={equipment.name}
            note={
              eqInvalid.length ? (
                <span className="setup__err">
                  {eqInvalid.join(', ')} {eqInvalid.length === 1 ? 'is' : 'are'} not valid
                </span>
              ) : null
            }
            moreLabel="Adjust the rig"
            forceOpen={eqInvalid.length > 0}
            more={
              <EquipmentEditor
                presets={presets}
                value={equipment}
                origin={origin}
                onChange={(next, from) => {
                  setEquipment(next)
                  setOrigin(from)
                }}
                onInvalidChange={setEqInvalid}
              />
            }
          />

          <Row
            label="Exposure"
            summary={`SNR ${snrGoal} · ${tSubS} s subs · ${slotMinutes} minute slots`}
            moreLabel="Adjust exposure"
            forceOpen={!!whyNot('snrGoal') || !!whyNot('tSubS')}
            more={
              <div className="setup__grid">
                <Field id={ids.snrGoal} label="SNR goal">
                  <input
                    id={ids.snrGoal}
                    type="number"
                    min={5}
                    max={300}
                    step={5}
                    value={typed.snrGoal ?? snrGoal}
                    aria-invalid={!!whyNot('snrGoal') || undefined}
                    aria-describedby={whyNot('snrGoal') ? `${ids.snrGoal}-hint` : undefined}
                    onChange={(e) => typeNumber('snrGoal', e.target.value, setSnrGoal)}
                    onBlur={() => settleNumber('snrGoal')}
                  />
                  <Hint id={`${ids.snrGoal}-hint`} error={whyNot('snrGoal')} />
                </Field>

                <Field id={ids.tSubS} label="Sub-exposure (s)">
                  <input
                    id={ids.tSubS}
                    type="number"
                    min={5}
                    max={900}
                    step={5}
                    value={typed.tSubS ?? tSubS}
                    aria-invalid={!!whyNot('tSubS') || undefined}
                    aria-describedby={whyNot('tSubS') ? `${ids.tSubS}-hint` : undefined}
                    onChange={(e) => typeNumber('tSubS', e.target.value, setTSubS)}
                    onBlur={() => settleNumber('tSubS')}
                  />
                  <Hint id={`${ids.tSubS}-hint`} error={whyNot('tSubS')} />
                </Field>

                <Field id={ids.slot} label="Slot length">
                  <select
                    id={ids.slot}
                    value={slotMinutes}
                    onChange={(e) => setSlotMinutes(Number(e.target.value))}
                  >
                    {[2, 5, 10, 15].map((m) => (
                      <option key={m} value={m}>
                        {m} minutes
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            }
          />
        </div>

        {/* The one part of the page that genuinely differs from last clear
            night, so it gets the room the four lines above gave up. */}
        <section className="setup__targets">
          <div className="setup__targets-head">
            <h2>Targets</h2>
            <span className="setup__targets-note">{selected.length} selected</span>
            {fit && (
              <span
                className={`setup__targets-note${fit.hours > fit.dark ? ' setup__fit--over' : ''}`}
                title="The ranking's hours to the SNR goal for the selected targets that are up, summed. No cloud, no Moon, no target changes: the plan will need more."
              >
                {fit.hours.toFixed(1)} h of {fit.dark.toFixed(1)} h dark, at best
              </span>
            )}
            {following && selected.length > 0 && (
              <span className="setup__targets-note">following tonight&rsquo;s picks</span>
            )}
          </div>
          <TargetPicker
            request={tonightRequest}
            held={held}
            selected={selected}
            onChange={setTargets}
            onRecommended={(ids, request, out) => setRanked({ ids, request, out })}
          />
        </section>

        {/* Wraps, unlike a chip: a server message can be long, and must not
            widen the sheet past a phone's width. */}
        {error && (
          <div className="setup__error" role="alert">
            Could not create the schedule: {errorText(error)}
          </div>
        )}

        <div className="setup__foot">
          <div className="setup__go">
            {busy ? (
              // The fold is three to seven seconds of CP-SAT and there is no
              // session to ask about it: the server reports each stage down
              // the response it will deliver the night on, and this is where
              // that lands. See `plan/foldProgress.ts`.
              <FoldBar label="Planning the night" />
            ) : (
              <span
                className={`setup__why${invalid.length ? ' setup__why--bad' : ''}`}
                aria-live="polite"
              >
                {invalid.length ? invalid.join(' · ') : (waiting ?? '')}
              </span>
            )}
            <button
              type="button"
              className="primary"
              // Not before the night window has answered: the only fallback
              // would be a guessed window, and a guess is not a plan. Nor on
              // picks ranked for a night no longer on the form.
              disabled={busy || invalid.length > 0 || waiting !== null || startIso === null}
              onClick={submit}
            >
              {busy ? 'Folding the night…' : 'Create Schedule'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * One line of the night: a name, what the night currently says, and a
 * disclosure holding the controls that say it.
 *
 * The page used to be four headed sections and twenty-four resident controls,
 * which asked an observer to re-read their own aperture every time they
 * planned. Almost none of it changes between two clear nights at the same
 * backyard with the same rig, so the resting page is four sentences and the
 * one list that does change, and every knob is one click away rather than
 * gone.
 *
 * Two rules keep the fold honest. A row whose contents have gone invalid
 * opens itself and stays open, because a drawer must never be the reason a
 * blocker is invisible -- the summary carries the same complaint in words, so
 * the line is readable without opening anything. And a closed drawer is
 * HIDDEN, not unmounted: unmounting would throw away half-typed text and
 * would retract the very invalidity that forced it open, which is a loop.
 */
function Row({
  label,
  labelFor,
  summary,
  note,
  more,
  moreLabel = 'Adjust',
  forceOpen = false,
}: {
  label: string
  /** The control the row's name names, where the summary IS a single control. */
  labelFor?: string
  summary: React.ReactNode
  note?: React.ReactNode
  more?: React.ReactNode
  moreLabel?: string
  forceOpen?: boolean
}) {
  const [open, setOpen] = useState(false)
  // A row forced open IS open. Without this the button's invisible toggles
  // decide what happens when the force is released: press it an even number
  // of times while pinned and the drawer collapses the instant the value
  // becomes valid, taking the fields out from under the hands that just
  // corrected them, and press it an odd number and it stays. Parity is not a
  // thing a disclosure should depend on.
  useEffect(() => {
    if (forceOpen) setOpen(true)
  }, [forceOpen])
  const shown = open || forceOpen
  return (
    <section className={`setup__row${shown ? ' setup__row--open' : ''}`}>
      {labelFor ? (
        <label className="setup__row-label" htmlFor={labelFor}>
          {label}
        </label>
      ) : (
        <div className="setup__row-label">{label}</div>
      )}
      <div className="setup__row-main">
        <div className="setup__row-summary">{summary}</div>
        {note && <div className="setup__row-note">{note}</div>}
      </div>
      {more && (
        <button
          type="button"
          className="setup__row-more"
          aria-expanded={shown}
          // The one press that cannot do anything is not offered. What holds
          // it open is said in the note beside it, so this is never a dead
          // control with no explanation.
          disabled={forceOpen}
          title={forceOpen ? 'Stays open until the values here are valid' : undefined}
          onClick={() => setOpen((o) => !o)}
        >
          {moreLabel}
          <span className="setup__row-caret" aria-hidden="true">
            &rsaquo;
          </span>
        </button>
      )}
      {more && (
        <div className="setup__drawer" hidden={!shown}>
          {more}
        </div>
      )}
    </section>
  )
}

/** A labelled control: `id` is the control's, so the label names it. */
function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  )
}

/**
 * Why a value cannot be sent, under the field it cannot be sent from.
 *
 * This used to carry a line of guidance as well ("9 or more frames per
 * block"), and the guidance is gone: a form that explains itself on every
 * line is read once and skimmed for ever after, and the numbers here are
 * ones an observer already knows. What is left is the half that is about
 * THIS form in THIS state, which is the half worth the room.
 */
function Hint({ id, error }: { id: string; error: string | null }) {
  return error ? (
    <div className="field__hint setup__err" id={id} role="status">
      {error}
    </div>
  ) : null
}

/** Why the night window did not load, as the target picker words its own failures. */
const windowProblem = (err: unknown): string =>
  err instanceof ApiError && err.status < 500
    ? `Cannot find this night's dusk and dawn: ${errorText(err.message)}`
    : err instanceof ApiError
      ? `The server could not find this night's dusk and dawn (HTTP ${err.status}).`
      : "Could not reach the server for this night's dusk and dawn."

const utcHour = (iso: string): number => {
  const d = new Date(iso)
  return d.getUTCHours() + d.getUTCMinutes() / 60 + d.getUTCSeconds() / 3600
}

/**
 * A local-solar time of day, as the instant it names within the night of
 * `localDate`: the night runs from local noon on that date to local noon the
 * next, the same anchor the server uses. So 21:00 is that evening and 03:00
 * the following morning, wherever UTC midnight happens to fall between them.
 */
const instantInNight = (localDate: string, offsetHours: number, localHour: number): number => {
  const noon = Date.parse(`${localDate}T12:00:00Z`) - offsetHours * 3_600_000
  const ms = noon + normaliseHour(localHour - 12) * 3_600_000
  // The solar offset is not a whole number of minutes; the instant should be.
  return Math.round(ms / 60_000) * 60_000
}

const normaliseHour = (h: number): number => ((h % 24) + 24) % 24

const hourToTime = (h: number): string => {
  const x = normaliseHour(h)
  const hh = Math.floor(x)
  const mm = Math.round((x - hh) * 60)
  return `${String(mm === 60 ? hh + 1 : hh).padStart(2, '0')}:${String(mm === 60 ? 0 : mm).padStart(2, '0')}`
}

const timeToHour = (t: string): number => {
  const [h = '0', m = '0'] = t.split(':')
  return Number(h) + Number(m) / 60
}

const localHhmm = (iso: string | null, offsetHours: number): string => {
  if (!iso) return 'unknown'
  // Rounded, as the Start field rounds, so "dusk 19:35" and a start of 19:35 agree.
  const d = new Date(Math.round((Date.parse(iso) + offsetHours * 3600_000) / 60_000) * 60_000)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}
