# Traveling Telescope

Given a site, equipment and a target list, produce a night plan that gives each
target its best available time — and re-plan in real time as clouds move,
the Moon rises, and new transients are discovered.

Two modes:

- **Live** — tonight, real-time data, offered re-plans starting from *now*.
- **Replay** — any night in the past 7 days, scrubbed with a time slider, where
  the plan shown at slider time `T` uses **only data published at or before `T`**.

## Status

Backend core is built and tested. Milestones 0–6 of 10 are complete.

| | Milestone | State |
|---|---|---|
| 0 | Credential hygiene | done |
| 1 | As-of clock, provider gate, guardrails | done |
| 2 | Time grid, domain types, night geometry | done |
| 3 | Pipeline builder (grid assembly) | done |
| 4 | Physics: sky brightness, moonlight, twilight, ETC | done |
| 5 | CP-SAT scheduler | done |
| 6 | No-lookahead property test | done |
| 7 | Real providers — Open-Meteo + CelesTrak done; Space-Track, ALeRCE next | partial |
| 8 | FastAPI + SSE — server done; SQLite persistence next | partial |
| 9 | React frontend | done |
| 10 | Three-arm evaluation | done |

## Run the server

```bash
uv sync --group dev --extra api
uv run python -m tscheduler.api            # http://127.0.0.1:8000
uv run python -m tscheduler.api --reload   # restart on source changes
```

Interactive API docs are at <http://127.0.0.1:8000/docs>. A session is one POST:

```bash
# Fold a night. Returns immediately with 202 and an id; the fold runs in the
# background and takes a few seconds.
curl -sX POST localhost:8000/api/sessions -H 'content-type: application/json' \
     -d '{"date":"2026-09-13","hours":9,"snrGoal":35}'

# Watch it build, then read the night: twilight bands, moon track, targets,
# and the decision points the replay slider will snap to.
curl -s localhost:8000/api/sessions/$ID

# The plan in force at an instant. The response carries validFrom/validUntil,
# so a slider only refetches when the cursor leaves that interval.
curl -s "localhost:8000/api/sessions/$ID/plan?as_of=2026-09-13T05:00:00Z"

# The efficiency/preference heatmap behind that plan.
curl -s "localhost:8000/api/sessions/$ID/grid?as_of=2026-09-13T05:00:00Z"

# For the sky view only: planets and zenith sky darkness per slot, and the
# night's satellite passes (current CelesTrak elements, SGP4, every 10 s).
curl -s localhost:8000/api/sessions/$ID/sky
curl -s localhost:8000/api/sessions/$ID/satellites

# Notifications: identifiers and progress, never plan data.
curl -N localhost:8000/api/events
```

Weather is real by default (`"weather": "auto"`), and decided by the night, not
by the request:

- a night that is **over** replays the ECMWF IFS 0.25° runs Open-Meteo archived
  (Single Runs API, no key; the archive starts 2 April 2026), each one visible
  only from the moment it was published;
- **tonight**, or a night up to 15 days ahead, uses the live forecast, stamped
  with the moment it was fetched;
- a night further out, or before the archive, is planned as clear and says so.

Nothing is invented: a field the source does not give is `null`, and a night
the forecast only partly covers says where the forecast ends. `"synthetic"`
runs fully offline against deterministic model runs with realistic publication
times, and `"open_meteo"` forces the archive.

Sessions live in memory. Restart the process and they are gone; the fold is a
pure function of the request, so any session can be rebuilt from it.

## Run the UI

Two processes. The Vite dev server proxies `/api`, so the browser sees one
origin and the SSE stream is same-origin.

```bash
# terminal 1
uv run python -m tscheduler.api

# terminal 2
cd frontend && npm install && npm run dev     # http://localhost:5173
```

## Deploy to Vercel

One project serves both halves: the built SPA from the CDN, and the API as a
Python function at `api/index.py`. `vercel.json` sends `/api/*` to the
function and everything else to the static build.

```bash
vercel login          # interactive: opens a browser
vercel link           # once, to create or attach the project
vercel                # preview deployment
vercel --prod         # production
```

### What the deployed app does differently

A serverless instance forgets the night between requests, so the deployed API
never keeps one. `POST /api/night` folds the whole night and answers with all
of it at once -- every plan, grid, geometry row and satellite pass, about
250 KB gzipped -- and the browser scrubs against its own copy. The reasoning,
and what it costs, is in `src/tscheduler/api/oneshot.py`.

| | Run locally | Deployed |
|---|---|---|
| Fresh weather and satellite data | yes | yes, unchanged |
| Waiting for a fold | progress over SSE | one 3-7 s request |
| A night re-planning itself overnight | every 15 min until dawn | on "Re-plan now" |

The last row is the real loss. `api/live.py` exists to stop a night folded at
dusk being presented as though it were what 04:00 looked like, and it does
that by checking for a new forecast run every few minutes until dawn. Nothing
can hold a night open here, so the check became a button. The guarantee it
protects is intact either way: every plan is built at its own `as_of`, and
the past is locked at each one.

The stateful endpoints (`POST /api/sessions` and the per-session GETs) are
still here and still work when you run the server yourself. The deployed
frontend does not call them, and on Vercel they answer 404 for every session
id -- which is the truth.

### Environment

`api/index.py` points the cache at `/tmp`, the only writable path on the
platform, so nothing is required to deploy. Set `TSCHED_SPACETRACK_USER` and
`TSCHED_SPACETRACK_PASS` in the project's environment for the historical
orbital elements replay mode uses.

### Size

The function bundles ortools, numpy, astropy and pandas: roughly 200 MB
against a 250 MB limit, so `requirements.txt` is pinned -- a minor release is
free to move that by tens of megabytes. If a build ever fails on size, the
lever is `src/tscheduler/scheduling/cpsat.py`. It is the only importer of
ortools (pandas comes in behind it, ~115 MB for the pair), and `naive.py`
honours the same constraints without it -- at the cost of the optimisation
this project exists to measure.


**Plan a night** asks for three things, and not for the weather:

- **Where and when.** A site and a night. "Night of" defaults to the night the
  site is in or about to start, by its local solar time, and a typed start time
  always lands inside that night, whichever side of UTC midnight it falls.
- **Telescope and camera.** 35 telescope configurations (reduced ones as their
  own rows) and 13 cameras, each from manufacturer figures. Every number the
  exposure model reads is editable: aperture, focal length or focal ratio,
  reducer or Barlow, central obstruction, optical throughput, pixel size, sensor
  size, effective QE, read noise, dark current and download time, plus slew time
  and minimum block. What the rig then does is computed by the server, not the
  page: effective focal length and ratio, Dawes and Rayleigh limits, the Airy
  core, the star size at 2.5″ seeing, pixel scale and sampling, field of view
  and collecting area (`POST /api/equipment/derive`).
- **Targets.** A search over the whole catalogue, ranked for that night, site
  and rig (`POST /api/targets/tonight`, about 40 ms warm). Each row shows how
  long it is usable above the altitude floor and clear of the Moon, how high it
  gets and when, how it fits the camera's field, and a best-case time to the SNR
  goal. Twelve are recommended, with no more than seven of one kind, and the
  selection follows the recommendations until you change it. A selected target
  that will not be up is flagged and says why.

Then press **Create Schedule**. The **weather** box, top left, says under
*Tonight*, for the plan the cursor is in, what the forecast known at that
moment made of the rest of the night: a sky-cover category, a go / marginal /
poor verdict, the best clear stretch, a cloud strip across the night, and
warnings for dew, wind and gusts. It names the model run it read and when that
run was published.
The sky behind the form is already live; when the night is folded the ground
rises into place and the plan drops onto a sky you were already looking at.

**The sky, from the ground.** A first-person view in the manner of Stellarium
Web: you stand at the site and look up, through a stereographic projection that
opens past 180 degrees until the horizon bends into a circle. Everything on it
is real and is where it really is at the cursor's instant:

- 41,411 Hipparcos stars to magnitude 8 in their B–V colours, drawn to the
  backend's naked-eye limit for that slot (Schaefer, from the same sky model
  the scheduler uses), dimmed by the site's extinction toward the horizon and
  deepened as you zoom — so a bright Moon or twilight thins the stars the way
  it does outside;
- the NASA/SVS Milky Way, the 89 IAU constellation figures and names;
- the planets (astropy positions, Astronomical Almanac magnitudes), the Moon
  with its true phase and bright limb toward the Sun, and its exclusion ring;
- satellites from CelesTrak's "visual" group — the ISS, Tiangong, Hubble and
  about 150 bright rocket bodies — propagated with SGP4 every ten seconds and
  shown only while sunlit, so at 1× they cross the sky at their real speed and
  vanish into Earth's shadow where they really do;
- the targets as rings, and the camera's field outlined around the target
  being observed;
- the **observing route**: every stop in the plan numbered with its start time
  ("3 · NGC 7023 · 02:58"), joined in observing order. Legs already travelled
  stay visible but muted, and done targets get a tick. The next leg is bright
  with dashes flowing toward the next stop, and a leg being slewed fills in as
  the telescope moves. When a new plan replaces the old one, the old route
  fades out while the new one appears;
- the whole deep-sky catalogue (about 1,850 objects) as faint rings, named once
  you zoom in, so anything can be clicked, searched for and added to the plan;
- a sky whose brightness is the backend's zenith sky brightness per slot, and a
  generated skyline of hills, trees and two roofs (scenery, not your site).

Drag to look around (the sky stays under the pointer), scroll or pinch to zoom
toward the pointer, click anything for its card, double-click to centre and
follow it, and search from the top bar. Nothing real is behind a toggle; the
bottom bar offers only **Focus on plan**, which fades the Milky Way,
constellations, faint stars, satellites and the catalogue so the targets and
the route stand out, and **Grid**, the alt-azimuth grid and altitude floor that "point ENE, 45°
up" is written in.

**Tonight is live; only a finished night is a replay.** The cursor can never
show an instant that has not happened. For a night that is over, the clock
beside the bar runs at 1×, 10×, a minute or ten minutes per second, and the
whole night can be scrubbed. For tonight it has no speeds: it opens on the
present and follows it in real time, marked **LIVE**. Drag back to review what
has already happened tonight; drag to the right end, or press **Back to live**,
to return. Everything after the present is under a hatched "not yet" veil. The
plan's blocks show through it, because a plan is a forecast, but the cursor
cannot go there. At dawn the night becomes a replay and the speeds appear.

Meanwhile the server **watches** tonight. Every 15 minutes
(`TSCHED_LIVE_REFRESH_MINUTES`) it asks Open-Meteo whether a newer model run
exists. That is one `meta.json` request, and nothing is fetched unless the
answer is yes. When there is a newer run, the server fetches it and re-plans
from that moment, with every earlier slot locked. The new plan is stamped with
the time the data was actually retrieved, not when the run was published. The
Activity panel shows when it last checked, when it checks next and what it
found, and **Check now** asks immediately (`POST /api/sessions/{id}/refresh`).

Drag the bar at the bottom. The cursor is both the clock and the as-of, and
scrubbing inside one decision interval issues **zero network requests**. The
night is folded up front (up to the present, for tonight), and the slider
bisects into the result. The playhead carries the cursor's time, and the part
of the night already behind it is tinted. Each marker along the bar's lower edge
is an update that arrived, and each along its upper edge is an alert, both in
their category's colour. Click one to jump there.

**The whole night, as a list.** The instruction card, top right, says what to
point at now. Under it, **Show full schedule** opens the night one row per
block in time order -- when it runs, what it is, how many exposures the plan
asks of it and how long it takes, with the block the cursor is in marked.
Clicking a row seeks there. On a night still happening the rows ahead of the
wall clock are shown but are not controls, because the cursor cannot go there.

**Alerts** pop at the top of the screen when the cursor *reaches* them, not
when the data arrives, so playing the night back shows each update the way an
observer at the telescope would have met it. Each is labelled by kind, not only
by how serious it is: a forecast update, cloud over a block, a satellite
crossing the field of view, the Moon, the horizon, too few frames. One that
re-planned the night says so in a sentence -- "M15 is in at 04:08, M74 is out.
About 1 h of the night is planned differently." -- so the change is legible
without opening anything. Every alert fades after five seconds, whatever its
severity; hovering holds it, a backgrounded tab holds the whole queue, and the
Activity panel keeps all of them. The Now card lists the alerts for its block,
one line each, and what to do about one opens on click. Transients (ZTF/LSST) have
their own style ready, but will not appear until the ALeRCE provider is built.

**Add to schedule.** Click anything in the sky and its card offers two
actions: **Zoom in**, and **+ Add to schedule**. A night that is over is
re-planned from the cursor, and every later forecast arrival is re-solved on
top of it; a night still happening is re-planned from now, because re-planning
from a past instant would rewrite a plan already handed over. Either way the
past stays locked, and the route redraws.

Adding is a request, not an order, and a request that cannot be met changes
nothing. If the optimiser can give the object no time -- it never clears the
altitude floor again tonight, or the targets already planned gain more from the
hours that are left -- the night is left exactly as it was: no target in the
spec, no decision point, no geometry row. A dialog says which of those it was
(`POST /api/sessions/{id}/targets` answers 409 with the reason; `api/amend.py`
solves every amendment against a working copy, so declining to commit it is the
whole of the rollback). Stars, planets, the Moon and satellites keep the button
and the dialog explains what they are: the exposure model works in surface
brightness, and a scheduled block is a fixed right ascension and declination.

**The weather box**, top left, is all of it in one place: what the sky is
doing at the cursor, and what the plan makes of the night. Under *Now*, for the
slot the cursor is in: how much of the sky is clear, cloud cover and its layer
split, seeing (labelled *assumed*, because no source forecasts it),
temperature, dew point with the humidity that flags dew, wind and
precipitation. Under *Tonight*, the outlook above. The two disagree often and
legitimately -- a clear slot inside a cloudy night -- which is why each is
under its own heading: the *Now* figures follow the cursor, the *Tonight*
figures follow the plan's decision point and hold still while you scrub within
it. Behind two folds: what is lighting the sky (the Moon's share of it,
usually, which is the part worth having, because it will move) and which model
run this came from. When a newer forecast run takes over, the box switches to
that run and flashes to say so. A value the source did not provide is a dash,
never a guess.

Click any target for its visibility window — when it is up, when it actually
clears the altitude floor and the lunar exclusion, and when it transits. The
gap between "up" and "usable" is the answer to *why is this not scheduled*.

`pastSlotsRewritten` must always be zero: a plan may extend the night, never
rewrite a slot already handed over. It is computed at every decision point and
carried on every plan as `changes.pastSlotsRewritten`, and asserted in
`tests/api/test_api.py` and `tests/api/test_amend.py`. The UI no longer shows
it, so to check it yourself rather than take the tests' word for it, read it
off `GET /api/sessions/{id}/plan` as you step through the decision points.

### The sky data

`frontend/public/sky/` is vendored and committed, so a fresh clone works
offline. To rebuild or re-verify it:

```bash
cd frontend
npm run sky:build      # fetch the catalogue and the background (needs cwebp)
npm run sky:check      # PROVE the background is in the frame we think it is
npm run verify         # build + bundle split + sky alignment
```

`sky:check` is not a formality. The NASA background turned out to be MIRRORED
relative to the obvious guess — right ascension zero at the image centre,
increasing to the left — and the obvious test for that ("is the brightest patch
the galactic centre?") **passes on the wrong image**, because the mirror has a
fixed point at RA 270° and the galactic centre sits almost exactly there. The
check that actually constrains the frame takes the brightness-weighted moment
tensor of every pixel and asks whether its smallest eigenvector is the galactic
pole. It is, to 1.8°.

Likewise `scripts/verify-sky.mjs <sessionId>` checks the numbers that reach the
browser: that the published rotation reproduces the published altitudes and
azimuths (residual ~23″, the annual-aberration floor), that named bright stars
land within 0.2″ of SIMBAD, that the constellation lines connect the right
stars, and that Polaris sits at the site's latitude.

The TypeScript types are generated from the backend's own OpenAPI document:

```bash
cd frontend && npm run gen:types
```

`tests/api/test_openapi_contract.py` fails if the committed `openapi.json` and
the live app ever disagree.

### The catalogue

`src/tscheduler/catalog/dso.csv` holds 1,857 deep-sky objects: every NGC, IC
and Messier object from [OpenNGC](https://github.com/mattiaverga/OpenNGC) that
a camera can plan for, and the Sharpless H II regions (Sharpless 1959, via
VizieR VII/20) no NGC or IC nebula already covers, with common names and each
object's mean surface brightness. That comes from HyperLEDA for galaxies, from
magnitude and size for the rest, and from a documented typical value where the
only catalogue magnitude is an embedded star's or cluster's (the Iris, the
Cocoon, the Pleiades); such values are flagged as estimated.

OpenNGC is © Mattia Verga and contributors, licensed
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). This file is a
modified version, released under the same licence; `catalog/NOTICE` lists what
changed, and the app links to it (`GET /api/catalog/notice`). Rebuild or
re-verify from the pinned upstream commit with:

```bash
uv run python scripts/build_catalog.py           # rebuild
uv run python scripts/build_catalog.py --check   # prove the committed file is what the sources give
```

## Try it from the command line

```bash
uv sync --group dev

# Plan a night offline against a synthetic forecast
uv run python scripts/plan_night.py --bortle 5 --snr 60

# Replay a REAL past night against real Open-Meteo model runs,
# watching the plan adapt as each new forecast is published
uv run python scripts/replay_night.py --date 2026-09-13

# Naive vs static vs adaptive on a real night
uv run python scripts/compare_arms.py --date 2026-09-13
```

```
  NIGHT PLAN  2026-09-13 01:00 - 09:00 UTC
  Site +40.1164,-88.2434 Bortle 5 | 203mm f/10.0 | SNR goal 60
  4 blocks, 4 targets, 92 observing / 4 switching / 0 idle slots, status=OPTIMAL

  01:00-01:05  move to NGC7000 N.America and focus
  01:00-01:55 | NGC7000 N.America
    RA 20h59m00.0s  Dec +44d22'12.0"   altitude 64->72 deg
    30 x 90s  (50 min integrating)
  ...
  NOT SCHEDULED
    M57 Ring    fits in principle (85 of 137 reference-min available)
                but was outranked for the slots it needed
```

### Replay on real data

```
  161 records from 13 runs, published 09-10 13:36 .. 09-13 13:36 UTC

  as_of 01:00  (session start)
    knows 13 records, newest published 09-12 19:36 | forecast mean cloud 15%
    plan: m27 -> m31 -> m27 -> n7000 -> m31 -> m33

  as_of 01:36  (new 01:36 run arrived)
    knows 13 records, newest published 09-13 01:36 | forecast mean cloud 3%
    plan: m27 -> m57 -> m27 -> m57 -> m31 -> n7000 -> m31 -> m33
    locked: slots 0-6 are history and cannot move
    CHANGED: added m57; 39 slot(s) differ

  as_of 07:36  (new 07:36 run arrived)
    locked: slots 0-78 are history and cannot move
    unchanged
```

Replay is a **fold**, not a sequence of point queries: slots already past are
locked to whatever the accepted plan had them doing, because those photons were
either collected or missed. The script asserts zero past slots moved rather than
claiming it. The forecast's dissemination lag is *measured* from Open-Meteo's
`meta.json`, and never taken below 9 h for ECMWF IFS 0.25°: the 00z and 12z
runs arrive later than the 06z and 18z (8.4 h observed), and rounding early
would let a replay see a run before it existed.

## Evaluation, reported honestly

Three arms — naive list-order, optimized-once-at-dusk, and adaptive — run over
one shared timeline and are scored against the **same realized conditions**,
never the forecast each planned with. The naive arm obeys every operational rule
the optimizer does; only optimisation is withheld.

Running it on six real September nights initially showed the optimizer *losing*
to list-order on five of them. That was a bug signal, not a result, and it
exposed two genuine problems:

- **The model targeted `E_t` exactly, with zero slack.** Under realized
  conditions the optimizer repeatedly landed targets at 0.98× requirement and
  got no credit, while naive's greedy blocks overshot by accident and survived.
  A 15% completion margin fixes it — and matches what observers actually do.
- **The headline metric rewarded spreading effort thin.** `science_value` gives
  partial credit, so on one night all three arms completed 4 targets while naive
  "won" purely on partial credit for targets it never finished. An SNR goal is a
  *threshold*, so targets-completed is now primary.

Choosing a metric after seeing results is how a comparison gets rigged, so the
reasoning is printed in the runner's own output rather than left implicit.

**Current honest status:** the optimizer ties or beats naive on completions on
four of six nights, and adaptive is close to static on these particular nights
because their forecasts evolved only mildly. Six nights is not evidence either
way, and the runner says so.

## How the schedule is computed

```
 SETUP       site, equipment, targets, SNR goal, time window
   |
 GEOMETRY    where is everything, all night?        no network; ~250 ms, once
   |
 CONDITIONS  how good is the sky, slot by slot?     all external data enters here
   |
 PHYSICS     how fast does SNR accumulate?  -> eta  ~5 ms
   |
 SOLVE       assign targets to slots                CP-SAT, ~80 ms
   |
 ADAPT       new data -> redo the future only -> diff -> explain -> offer
```

The night is cut into slots (5 minutes by default). For every (target, slot)
the system computes **efficiency** `eta`: how fast that camera would actually
accumulate signal-to-noise if pointed there, given altitude, moonlight, light
pollution, twilight, cloud and the camera's download dead time. `eta = 0.4`
means one second there buys 0.4 seconds of progress versus a reference slot.

A target is a **surface**, not a star: a galaxy or nebula described by its mean
V surface brightness in mag/arcsec². "SNR 20" means SNR 20 in each star-sized
patch of it (the photometric aperture of a 2.5″ star on that rig), which is the
scale an image actually resolves. 20 is a clean single-night image and the
default; 40 or more is a deep one. Frames are counted per block, so the frames a
block lists are exactly the frames that fit in it.

Because **SNR² is exactly additive** across sub-exposures, "has this target had
enough time?" is a plain sum:

```
sum over chosen slots of ( slot seconds x eta[target, slot] )  >=  E_target
```

That identity is verified to machine precision in `tests/unit/test_snr_coupling.py`
against the full CCD equation, and it is what makes the scheduler a linear
optimisation rather than a simulation.

## The no-lookahead guarantee

Replay is only meaningful if future data cannot influence a past decision. Every
datum carries three timestamps and conflating any two is the bug:

| | |
|---|---|
| `published_at` | when it became knowable to anyone — **the only gate** |
| `valid_time` | the instant it describes — unconstrained |
| `fetched_at` | when we pulled it — bookkeeping |

A TLE's `EPOCH` is *not* its publication time; a forecast's whole purpose is to
have `valid_time > published_at`. Four enforcement layers:

1. **Structural** — nothing below the API may read a wall clock (AST-walk test).
2. **Runtime** — `Provider.fetch()` is `@final` and *asserts* rather than filters.
3. **Provenance** — `Plan.__post_init__` rejects evidence published after its own
   `as_of`. The only layer that catches a memoization leak.
4. **Oracle** — actuals are typed and may only be held by evaluation code.

And the property test that checks the *outcome* rather than any mechanism:
plan a night twice, once against a world containing post-`T` data and once
truncated at `T`, and demand byte-identical fingerprints. It includes a negative
control, so it cannot pass vacuously.

## Two deliberate departures from the original spec

**`q[t,s]` is split in two.** The spec has one score that both weights the
objective and scales effective exposure. That double-counts: a mediocre slot
earns less reward *and* needs more slots, an effective `q²` penalty nobody ever
wrote down. It also makes tuning impossible, since a preference slider would
silently change every target's exposure requirement. So: `efficiency`
(calibrated physics, drives the completion constraint) and `preference` (soft
policy, drives only the objective).

**The objective needs a completion bonus.** With only
`Σ priority·urgency·q·x`, the optimizer's best move is to spend the entire night
on the single highest-weight target. Nothing in it prefers *finishing* anything.

## Development

```bash
uv run pytest                 # offline suite (~100 s; includes property tests)
uv run pytest -m "not slow and not network"   # fast suite (~1 min)
uv run pytest -m network      # hits real APIs; deselected by default
uv run ruff check . && uv run ruff format .
uv run mypy
```

Python is pinned to 3.12. `astroplan` is deliberately **not** a dependency: its
last release was 2024-08-13, it is sdist-only, still requires `six`, and pins
`astropy>=4` with no upper bound, so it is untested against astropy 8.
