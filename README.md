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

```bash
cd frontend && npm run build && cd ..   # the deployment cannot build this
git add -A && git commit -m "..."       # frontend/dist is committed on purpose
vercel deploy --prod
```

Live: <https://telescope-scheduler.vercel.app>

One Python function serves everything -- the API and the built SPA behind it
(`src/tscheduler/api/asgi.py` mounts the static files under the API routes).
CP-SAT runs there: a production fold answers in ~3.7 s with
`status: OPTIMAL`.

### The two constraints that shaped this

**The night is not remembered.** A serverless instance forgets between
requests, so `POST /api/night` folds a whole night and returns all of it at
once -- every plan, grid, geometry row and satellite pass, ~250 KB gzipped --
and the browser scrubs against its own copy. `src/tscheduler/api/oneshot.py`
has the reasoning. The visible cost is that nothing re-plans overnight:
`api/live.py`'s watch became a "Re-plan now" button, which re-folds against
the current forecast. Every plan is still built at its own `as_of`, and the
past is still locked at each one.

**`outputDirectory` must not be set.** This is not a style preference. With
`buildCommand`/`outputDirectory` configured, the Python function measures
**526.61 MB** against a 500 MB ceiling; without them it measures 291.55 MB,
is optimised to under 225 MB, and ships. Vercel requires `outputDirectory`
whenever `buildCommand` is set, so the frontend cannot be built by the
deployment at all -- hence the committed `frontend/dist` and the build step
above. Forget it and the deployed UI is silently whatever was last built.

### Why not a smaller function, or a container

Trimming does not close the gap. ortools ships 79 MB of shared libraries
(`libortools`, `libscip`, `libhighs`) before numpy's OpenBLAS (24 MB),
astropy and the pandas that `cp_model` imports but this code never uses;
159 MB of the bundle is compiled binary that nothing can strip. `excludeFiles`
does not prune installed packages -- measured, twice, at about 2 MB of effect.

A container image would make the whole question moot (15 GB limit, and
`Dockerfile.vercel` is auto-detected), but Container Images is a gated beta
and was silently ignored on this account. If it is ever enabled, that is the
better deployment: the SPA goes back behind the CDN and the stateful design
becomes possible again.

### Environment

`api/index.py` points the cache at `/tmp`, the only writable path, so nothing
is required to deploy. Set `TSCHED_SPACETRACK_USER` and
`TSCHED_SPACETRACK_PASS` in the project's environment for the historical
orbital elements replay mode uses.

Deployment Protection (SSO) is on, so the URL asks for a Vercel login. Turn it
off in Project Settings -> Deployment Protection to make the site public.


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
