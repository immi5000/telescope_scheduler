# Telescope Night Scheduler

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

# Notifications: identifiers and progress, never plan data.
curl -N localhost:8000/api/events
```

`"weather": "synthetic"` (the default) runs fully offline against several
deterministic model runs with real publication times. `"weather": "open_meteo"`
fetches genuine archived runs for that night from the Single Runs API — no key
required.

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

Pick a night, press **plan the night**, then drag the timeline. The cursor is
both the clock and the as-of: everything left of it is hatched, because those
slots are history and no re-plan may touch them. The panel on the right names
the forecast run that arrived, what moved because of it, and how many past slots
were rewritten — which is always zero, shown rather than merely asserted in a
test.

Night mode is on by default. Dark adaptation takes 20–30 minutes to build and
seconds of blue light to destroy.

The TypeScript types are generated from the backend's own OpenAPI document:

```bash
cd frontend && npm run gen:types
```

`tests/api/test_openapi_contract.py` fails if the committed `openapi.json` and
the live app ever disagree.

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
`meta.json` (7.12 h for ECMWF IFS025), not assumed.

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

The night is cut into 5-minute slots. For every (target, slot) the system
computes **efficiency** `eta`: how fast that camera would actually accumulate
signal-to-noise if pointed there, given altitude, moonlight, light pollution,
twilight, cloud and seeing. `eta = 0.4` means one second there buys 0.4 seconds
of progress versus a reference slot.

Because **SNR² is exactly additive** across sub-exposures, "has this target had
enough time?" is a plain sum:

```
sum over chosen slots of ( 300 s x eta[target, slot] )  >=  E_target
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
uv run pytest -m "not slow"   # fast suite (~5 s)
uv run pytest -m network      # hits real APIs; deselected by default
uv run ruff check . && uv run ruff format .
uv run mypy
```

Python is pinned to 3.12. `astroplan` is deliberately **not** a dependency: its
last release was 2024-08-13, it is sdist-only, still requires `six`, and pins
`astropy>=4` with no upper bound, so it is untested against astropy 8.
