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
| 7 | Real providers (Open-Meteo, Space-Track, ALeRCE) | next |
| 8 | Persistence + FastAPI + SSE | |
| 9 | React frontend | |
| 10 | Three-arm evaluation | |

## Try it

```bash
uv sync --group dev
uv run python scripts/plan_night.py --bortle 5 --snr 60
```

Runs fully offline against a synthetic forecast. Swapping in a live provider is
the only change needed — that is what the provider seam is for.

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
uv run pytest                 # full suite (~110 s; includes property tests)
uv run pytest -m "not slow"   # fast suite (~5 s)
uv run ruff check . && uv run ruff format .
uv run mypy
```

Python is pinned to 3.12. `astroplan` is deliberately **not** a dependency: its
last release was 2024-08-13, it is sdist-only, still requires `six`, and pins
`astropy>=4` with no upper bound, so it is untested against astropy 8.
