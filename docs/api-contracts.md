# Provider API contracts

Verified against live endpoints and primary documentation, with an adversarial
re-check. These are the contracts milestone 7 implements against.

Already implemented: **Open-Meteo** (forecast + single-runs replay) and
**CelesTrak** (live GP elements). Remaining: ALeRCE and Space-Track below.

---

## ALeRCE (ZTF transient alerts)

## ALeRCE ZTF broker — implementation contract

**Base URL:** `https://api.alerce.online/ztf/v1` (Swagger 2.0 at `/swagger.json`, `basePath` `/ztf/v1`, `info.version` `1.0.0`)

**Auth:** none. I re-verified today: the OpenAPI document has `securityDefinitions: null` and `security: null`, and anonymous GETs return 200. CORS is `*`. Do not build a credential path.

**Freshness probe:** `GET /objects/limit_values` → I got `{"min_ndet":1,"max_ndet":4310,"min_firstmjd":58242.148,"max_firstmjd":61299.530}` (max = 2026-09-16 12:43 UTC, i.e. end of last night). This is the only way to tell how current the ingest is — there is no `Last-Modified` and no `ETag`.

### Which API to use
| Surface | Verdict |
|---|---|
| `api.alerce.online/ztf/v1` | **Use this.** Current, live, unauthenticated. |
| `api.alerce.online/v2/lightcurve/` | Supplementary only — detections, non-detections, lightcurve, forced photometry (`?survey_id=ztf`). Different field naming (`mag`/`e_mag` vs `magpsf`/`sigmapsf`). |
| `api.alerce.online/v2/` objects/query | **Does not exist** (404). There is no v2 object query. |
| `api-lsst.alerce.online` (multi-survey) | **LSST only.** The official client raises `NotImplementedError("ZTF is not yet supported in the multisurvey API.")`. |
| `tap.alerce.online` (ADQL) | **Trap.** Its `ztf_*` tables are labelled "DO NOT USE - ZTF DATA MIGRATION IN PROGRESS" and are ~5.5 months stale (max ZTF `firstmjd` 61160.5 = 2026-03-29). Its `oid` is an internal int64, not `ZTF26abvcyoo`. |

### Endpoints actually needed
- `GET /objects/` — **trailing slash required** (`/objects` 308-redirects and some clients drop the query string). Filters: `classifier`, `classifier_version`, `class` (not `class_name`), `probability` (a **minimum**, `>=`), `ranking`, `ndet`/`firstmjd`/`lastmjd` (**ranges expressed as repeated params**: 1st occurrence = lower bound, 2nd = upper), `ra`+`dec`+`radius` (arcsec, all three together — the only positional filter), `page`, `page_size`, `count`, `order_by`, `order_mode`. Returns `{total, page, next, has_next, prev, has_prev, items[]}`; `total` is null unless `count=true`.
- `GET /objects/{oid}` — metadata only, **no classification**.
- `GET /objects/{oid}/probabilities` — `{classifier_name, classifier_version, class_name, probability, ranking}`.
- `GET /objects/{oid}/lightcurve` — detections + non-detections (the upper limits that bound the explosion epoch).
- `GET /objects/{oid}/magstats` — cheapest per-band `maglast`.
- `GET /classifiers/` — **call at startup**; the classifier/version/class vocabulary has changed and will change again.
- `GET /objects/limit_values` — freshness probe.

### Publication timestamp: there is none
**The REST API exposes no ingestion or publication time anywhere.** Verified four ways (OpenAPI definitions, live JSON from five endpoints, the backing SQLAlchemy schema in `alercebroker/db-plugins` — no `created_at`/`inserted_at` on `object`, `detection`, `non_detection`, `probability`, `magstat`, `feature`, `xmatch` — and HTTP headers). `firstmjd`/`lastmjd`/`detection.mjd` are **observation** times: `mjd = jd - 2400000.5` where ZTF's `jd` is "Observation Julian date at **start of exposure**".

The TAP service has `created_date`/`updated_date`, but they are day-granularity strings reflecting a bulk ELT load, on tables marked do-not-use and months stale. Useless.

**You must synthesize `published_at = observation_mjd + latency`.** Sourced budget:
- ZTF alert production (observation → packet staged in IPAC Kafka): p5/p50/p95 = **6.7 / 8.5 / 12.6 min** — **Masci et al. 2019, PASP 131, 018003, §7.7**. *(Not Patterson et al. 2019 — a common misattribution; do not repeat it in code comments.)*
- IPAC Kafka → consumer: ~10 s (Patterson et al. 2019).
- ALeRCE stamp classification: ~1 s (Förster et al. 2021).
- ALeRCE `lc_classifier`: **a daily batch** (Sánchez-Sáez et al. 2021) → treat as **~24 h**.
- ALeRCE DB-write → REST-visible lag: **unmeasured**. Do not assume zero.

Constants: `p50 ≈ obs + 9 min`, `p95 ≈ obs + 13 min`, **replay guard = obs + 20–30 min** (err long; a short constant silently leaks the future and inflates measured scheduler performance). For anything derived from `lc_classifier`, use `obs + 24 h`.

The only interface carrying a real publication time is the ALeRCE Kafka stream (`kafka.alerce.science:9093`, SASL_PLAINTEXT / SCRAM-SHA-256, credentials by email, topics `stamp_classifier_YYYYMMDD` / `lc_classifier_YYYYMMDD`). **48-hour retention** — usable for live polling, useless for replaying a past night, and unrecoverable retroactively.

### Rate limits
**None documented anywhere** — not in the swagger description, readthedocs, science.alerce.online, or a robots.txt. Empirically 20 concurrent GETs all returned 200 with no 429 and no `Retry-After`. Absence of a documented limit is not permission; if this runs in production, email them. Recommended cadence (engineering judgment, not ALeRCE policy): poll every 2–5 min, concurrency 1–2, exponential backoff, stop entirely during Palomar daytime. Polling faster than ~2 min gains nothing real given the 8.5 min p50 production latency. **Set client timeouts ≥ 120 s** — `/classifiers/` needed 120 s on a cold call; a 30 s timeout produces spurious failures.

---

## Space-Track (historical orbital elements)

## Space-Track `gp_history` — implementation contract

**Base:** `https://www.space-track.org`; auth `POST /ajaxauth/login`; query `GET /basicspacedata/query/class/<CLASS>/<pred>/<val>/.../format/json`

### Auth
Form-urlencoded POST with exactly two fields, `identity` and `password`, to `/ajaxauth/login`. No CSRF token on that path (the token is only for the browser form at `/auth/login`). Session cookie is `chocolatechip`, `Max-Age=7200` — **re-login every < 2 h**, including mid-stream on a long paginated pull. A free account suffices. Unauthenticated queries return **401** with `{"error":"You must be logged in to complete this action"}` — probe for that rather than parsing the login response, whose success body is unverified. You can also POST `identity=...&password=...&query=<full URL>` to log in and retrieve in one request; this is the escape hatch for query URLs too long to send as a GET (e.g. a multi-thousand-entry `NORAD_CAT_ID` list).

### Rate limits — I re-fetched the public documentation page today and confirm verbatim
> "Limit API queries to less than **30 requests per 1 minute(s)** and **300 requests per 1 hour(s)**"

Per-class bandwidth guidance ("do not exceed the following data retrieval rates for your automated scripts or your account may be suspended"):
- **GP (aka TLEs): 1 / hour** — "Please randomly choose a minute that is not at the top or bottom of the hour."
- **GP_HISTORY: 1 / lifetime** — "Do NOT use this class to retrieve current ephemerides; use the GP class... Once you download an object's history, you need to store it on your own servers; do not download it again."
- SATCAT: 1 / day after 1700 UTC.

Suspension is the documented consequence, permanent for repeat offenders. **Design for one bulk pull per replay corpus, cached to disk forever.**

### The core call
```
GET /basicspacedata/query/class/gp_history
    /CREATION_DATE/2026-09-01%2000:00:00--2026-09-15%2000:00:00
    /MEAN_MOTION/%3E11.25            # LEO: period < 128 min
    /DECAY_DATE/null-val
    /predicates/NORAD_CAT_ID,OBJECT_NAME,CREATION_DATE,EPOCH,TLE_LINE1,TLE_LINE2
    /orderby/NORAD_CAT_ID%20asc,CREATION_DATE%20asc
    /limit/20000,0
    /emptyresult/show/format/json
```
Returns a flat JSON array of OMM records, **all values as strings**, datetimes as `YYYY-MM-DDTHH:MM:SS` UTC (`EPOCH` carries microseconds). ISO-8601 in this fixed form sorts lexicographically = chronologically, so the fold can compare strings directly.

Encoding: `>` = `%3E`, `<` = `%3C`, spaces (in datetimes and in `orderby/X desc`) = `%20`, ranges use `--` inclusive both ends, `null-val` matches NULL, `~~TEXT` is a wildcard anywhere, `^TEXT` a prefix wildcard. Relative variables `now-7`, `now+14`, `now-6.5` work; Space-Track's own live FAQ example is `.../class/gp_history/norad_cat_id/25544/CREATION_DATE/>now-10/format/xml`. Unconstrained queries fail with "Query range out of bounds" — always bound by `CREATION_DATE` **and** an object filter, and paginate under a stable `orderby`.

### Publication timestamp: `CREATION_DATE`. Never `EPOCH`.
Space-Track's live FAQ, on what replaced the deprecated `tle_publish` class: *"The use case for the tle_publish class will be the same gp_history class and use the **CREATION_DATE** field ... to differentiate **when the ephemerides was published to Space-Track.org**."* Per CCSDS 502.0-B-3 Table 4-1, `CREATION_DATE` is "File creation date/time in UTC"; `EPOCH` is "Epoch of Mean Keplerian elements" — two different clocks.

**Nuance you must handle:** Space-Track's own 2024 ops brief distinguishes "'insert epoch' is when Space-Track.org ingested the data" from "'creation date' is the data originator's creation date". **`gp_history` has no `INSERT_EPOCH`** — I re-fetched `https://www.space-track.org/basicspacedata/modeldef/class/gp_history` (publicly readable without login) and confirm **40 fields, none named `INSERT_EPOCH`**. So `CREATION_DATE` is the only publication-side clock the class exposes. **Cut at `T − margin`, e.g. `T − 1 h`,** to absorb the originator→site latency you cannot measure.

**Why filtering on `EPOCH` leaks, in both directions:**
1. *Admits data that did not yet exist* — systematically, not as an edge case. `EPOCH` is the reference time of the state; observations are fitted and the message created hours later. "Largest `EPOCH ≤ T`" normally selects an element set whose `CREATION_DATE` is **after** T. Indicative live measurement: CelesTrak's current 18-SDS GP for recently-cataloged objects showed a minimum `EPOCH` age of 11.7 h, median 14.3 h.
2. *Mis-ranks data that was in hand at T.* Space-Track: *"in rare cases space-track may receive a TLE with an **earlier EPOCH than the most recently uploaded TLE**."* Publication order ≠ epoch order.

**Rule:** filter `CREATION_DATE <= (T − margin)`, then `argmax(CREATION_DATE)` per `NORAD_CAT_ID`, tiebreak on `GP_ID` *(GP_ID monotonicity with ingestion is inference from the schema, not documented — verify before relying on it)*. `EPOCH` is only an SGP4 input afterwards, never a selection predicate.

### Fields (confirmed live, 40, in order)
`CCSDS_OMM_VERS, COMMENT, CREATION_DATE, ORIGINATOR, OBJECT_NAME, OBJECT_ID, CENTER_NAME, REF_FRAME, TIME_SYSTEM, MEAN_ELEMENT_THEORY, EPOCH, MEAN_MOTION, ECCENTRICITY, INCLINATION, RA_OF_ASC_NODE, ARG_OF_PERICENTER, MEAN_ANOMALY, EPHEMERIS_TYPE, CLASSIFICATION_TYPE, NORAD_CAT_ID, ELEMENT_SET_NO, REV_AT_EPOCH, BSTAR, MEAN_MOTION_DOT, MEAN_MOTION_DDOT, SEMIMAJOR_AXIS, PERIOD, APOAPSIS, PERIAPSIS, OBJECT_TYPE, RCS_SIZE, COUNTRY_CODE, LAUNCH_DATE, SITE, DECAY_DATE, FILE, GP_ID, TLE_LINE0, TLE_LINE1, TLE_LINE2`

Minimal streak-predictor set: `NORAD_CAT_ID, OBJECT_NAME, CREATION_DATE, EPOCH, TLE_LINE1, TLE_LINE2`. To stay safe past 5-digit catalog numbers (the USSF SATCAT is past 100,000 and such objects have no valid TLE text form), pull the OMM element fields instead and feed `sgp4.omm.initialize()`.

### Known unknowns
- **No `gp_history` query was ever executed** — neither the researcher nor the checker holds an account. Range syntax on that class, max rows per response, pagination beyond ~20k, whether `favorites/` is accepted, the real `CREATION_DATE − EPOCH` distribution, and the throttle's HTTP status are all documented-but-untested. Run the example against one `NORAD_CAT_ID` for one week before trusting the shape.
- The detailed API reference on `/documentation` is **login-gated**; the operator/predicate vocabulary comes from a 2016 Wayback capture of that page cross-checked against live public FAQ examples. Re-read the gated page once you have an account and diff it.
- Whether the yearly bulk zips (the Sync.com link Space-Track itself recommends for large ranges) carry `CREATION_DATE` is **unknown**. If they are bare TLE text they have no publication timestamp and are useless for leak-free replay. Check this early — it determines whether the bulk path exists at all.
- **CelesTrak is not a fallback.** Its GP output returns `<CREATION_DATE/>` **empty**, so it is structurally incapable of supporting leak-free replay; its historical archive is a CAPTCHA-gated email form capped at 10 requests/day, 100 satellites and 20,000 records per request, and the page itself redirects bulk users to Space-Track.

---

## Implementation notes

- ALeRCE cone search: NEVER pass ra=0.0, dec=0.0 or radius=0. I reproduced this live today. `?ra=10.0&dec=0.0&radius=5` returned ZTF17aaaaaak at (59.128, +49.428) — nowhere near the cone — while `?ra=10.0&dec=0.001&radius=5` correctly returned []. Cause is `if ra and dec and radius:` in astro_object_service.py::_create_conesearch_statement; 0.0 is falsy in Python, so the cone predicate is silently dropped and you get arbitrary rows from the whole table with no error. Substitute 1e-9 for an exact zero, or filter client-side.

- ALeRCE: ALWAYS pass an explicit `classifier` on /objects/. If you omit classifier/class/probability/ranking/classifier_version, the API joins against a hardcoded default of lc_classifier + hierarchical_random_forest_1.1.0 + ranking 1. That version does not exist in the live DB (/classifiers/ reports 1.0.0), so `class`, `classifier` and `probability` all come back null and you will conclude the object is unclassified when it is not.

- ALeRCE: firstmjd/lastmjd/ndet are RANGE parameters expressed as repeated query params, not min/max-suffixed names. First occurrence is the lower bound (>=), second is the upper (<=); a single occurrence is a lower bound only, unbounded above. Build params as a list of tuples, not a dict.

- ALeRCE: firstmjd is NOT 'first ever ZTF detection' — it is the earliest alert ALeRCE ingested for that oid. In a live 3-day sample, 35% of objects had mjdstarthist years earlier (e.g. ZTF18abrdzqr: firstmjd 61298.42 vs mjdstarthist 58377.41, eight years of prior ZTF history; ZTF23abjijyi: ndet=1 but ndethist=9). This is a correctness blocker for any 'young supernova' cut, not a footnote. Require small ndethist AND small (firstmjd - mjdstarthist), and ideally bound the explosion epoch with the non-detection upper limits from /lightcurve.

- ALeRCE REPLAY LEAKAGE: classification probabilities are CURRENT mutable state with no version history and no as-of query. Reading an object's lc_classifier probability today for a 2026-03-12 replay gives you a classification computed from the full light curve including everything after that date — straightforward future leakage. Mitigations, none perfect: (a) restrict replay decisions to stamp_classifier with classifier_version pinned, since its input is the frozen first-detection stamp triplet — but accept that the live model version on the replay night may not be the one you are reading; (b) reconstruct features yourself from /detections filtered to mjd <= replay_epoch; (c) archive the Kafka stream going forward, which cannot be done retroactively (48 h retention).

- ALeRCE naming traps: the list-item classification field is `class` (a Python/JS reserved word) while /probabilities uses `class_name`; the query parameter is also `class`. `ranking` silently defaults to 1 whenever you pass classifier/class/classifier_version. `ndethist` comes back as a STRING ('259') despite an integer DB column — cast before comparing. Corrected-photometry keys (magpsf_corr, sigmapsf_corr, sigmapsf_corr_ext) are ABSENT from the detection dict, not null, when corrected is false — use .get(). A single `oid` value is treated as a SQL LIKE pattern with * -> %, so a stray asterisk becomes a wildcard table scan.

- ALeRCE: stamp_classifier_2025_beta reports classifier_version 'beta' from /classifiers/ but the actual probability rows carry '2.1.1_beta'. Filtering on the value returned by /classifiers/ silently returns nothing.

- ALeRCE photometry semantics: magpsf is the DIFFERENCE-IMAGE PSF magnitude — right for detectability of a transient on a host galaxy. magpsf_corr adds back the reference flux and is right for total source brightness; they differed by 1.0 mag on a live object (20.203 vs 19.193). Choose deliberately and honour the `corrected` and `dubious` flags. Filter isdiffpos == 1 (positive subtraction) for transient hunting — negative-subtraction alerts are in the stream and are not brightenings.

- ALeRCE: there is NO server-side declination filter — the only positional filter is a cone search requiring ra+dec+radius together. Any dec cut is client-side on meandec. In practice this is nearly a no-op for ZTF's northern footprint (all 50 objects in a live SN query passed dec > -30), but the code must not assume the server did it.

- ALeRCE: only pass count=true when you actually need the total. It runs a second full query, hard-capped at max_results=50000, and `total` is null otherwise. Do not run unbounded pagination loops.

- Space-Track: make ONE bulk CREATION_DATE-range query covering the entire replay window plus lookback, cache it to Parquet, and answer every per-instant T locally. Never one query per night and never one per object — Space-Track's own guidance is explicit ('Do not send hundreds of individual queries... combining queries for multiple objects using a comma-delimited list where appropriate') and gp_history's documented retrieval rate is 1 per LIFETIME. Repeated large gp_history pulls are exactly what gets accounts suspended.

- Space-Track: the CREATION_DATE lookback must exceed the longest publication gap in your object set or objects silently vanish from the snapshot. Starlink republishes roughly 6x/day (~4 h spacing); quiet debris can go days. Seven days of lookback is the safe default. Paginate with limit/<count>,<offset> under a stable orderby and re-login mid-stream (the session cookie expires at 7200 s).

- Space-Track has NO Starlink/OneWeb/'active' groups — those are CelesTrak concepts. Its only named groups are the `favorites/` lists (my_favorites, All, Amateur, Navigation, Special_Interest, Visible, Weather), and whether `favorites/` is even accepted by gp_history is unverified. Filter with OBJECT_NAME/~~STARLINK, or resolve a CelesTrak group to a catalog-number list once and filter locally.

- Space-Track: ELEMENT_SET_NO is always 999 by design ('To eliminate confusion caused by reusing element numbers after 999'). It is not a version counter — never order by it. Use CREATION_DATE, with GP_ID as tiebreaker.

- Do NOT back-propagate current TLEs as a substitute for gp_history. Two independent reasons. Methodological and fatal: a current TLE was fitted to observations taken after T, so back-propagating it into the replay window IS the leak you are eliminating, whatever its accuracy. Quantitative: arXiv:2605.19850 (24,641 TLE-vs-next-TLE pairs, 501 Starlinks, April 2026) gives pooled L2 median error ~1 km at 6 h growing to ~38 km at 7 d, with along-track medians of ~16 km (v1.x) and ~98 km (v2-mini) at 7 d. That paper explicitly excluded maneuvers, and Starlink station-keeps continuously; CelesTrak's SupGP-vs-GP comparison shows 18-SDS GP already off by a median 9.3 km / p90 41.7 km / p99 475 km for ~11,100 Starlinks at under a day of element age. Nothing in a current TLE tells you which objects are in that tail.

- If a padded back-propagation is used anyway as a fallback, scope it honestly: the sky-PATH question ('does this satellite cross a 0.5 deg field?') survives with ~0.03-0.15 deg of cross-track error, so pad the field radius by ~0.15 deg; the slot-level TIMING question survives too (16-98 km along-track is 2-13 s of phase at 7.585 km/s against a 300 s slot, so pad the slot by ~1 min each end). But you CANNOT predict where in the slot or where in the frame the streak lands — a satellite crosses 0.5 deg in ~0.65 s at 0.79 deg/s and the phase is wrong by seconds. Boolean risk flag only; never streak time or position. Also note the 7-day error figures are FORWARD staleness; no source measures BACKWARD LEO propagation error, so treat backward as 'no better, probably worse once you leave the OD fit span or cross a burn'.

- sgp4 vectorised API: many dates one satellite is `e, r, v = satellite.sgp4_array(jd, fr)` (e shape (N,), r/v shape (N,3)); many satellites and many dates is `SatrecArray([...]).sgp4(jd, fr)` with jd and fr as NumPy arrays EVEN FOR A SINGLE DATE (e shape (M,N), r/v shape (M,N,3)). Units km and km/s in TEME. CRITICAL: 'The array routine is only faster if your machine has successfully installed or compiled the SGP4 C++ code' — assert `from sgp4.api import accelerated; assert accelerated` at startup, or a few thousand objects x 300 steps is a Python loop in disguise. Map error codes with SGP4_ERRORS (code 6 = decayed / underground, position still returned).

- Sunlit test: sgp4 gives position only, no illumination. Skyfield's is_sunlit() uses a SPHERICAL Earth (ERAD 6378136.6 m, so up to ~21 km wrong near the terminator from oblateness), UMBRA ONLY (Sun as a point, no penumbra — the ~0.26 deg penumbral half-angle smears the terminator by a few km along a 550 km orbit), no refraction or extinction, and needs GCRS positions plus a ~17 MB de421.bsp download. Fine for a risk flag, wrong near the shadow boundary. The repo already has a shadow test committed (ecb4219) — check which model it implements and whether it accounts for the penumbra before adding a second one.

- Provenance discipline in code comments and the pitch alike: attribute the ZTF alert-production percentiles (6.7/8.5/12.6 min) to Masci et al. 2019 PASP 131 018003 sec 7.7, NOT to Patterson et al. 2019 — that misattribution is widespread in secondary sources and Patterson contains only the ~10 s distribution leg. Likewise cite the LSST 10^7/night to DMTN-102, never to the SRD.

- Anything whose only verification is an arXiv preprint must say so. The published texts of Wang et al. 2024 (ApJ 962, 91), Villar et al. 2020 (ApJ 905, 94) and Lubin et al. 2026 (AJ 171, 85) could not be loaded — IOPscience serves a Radware bot-manager captcha to automated fetches. Crossref metadata resolves correctly for all three, so the citations themselves are sound, but do not claim the refereed text was checked. Lubin's section numbering 'III.4' is the preprint's; AJ would render it 3.4.

- Tonry et al. 2012 PS1 photometric-system coefficients remain UNVERIFIED (the PDF fetch failed). If the strict ZTF(PS1) -> SDSS -> V transformation chain is needed rather than applying Jester directly, read those coefficients out of the paper before using them. The Jester et al. 2005 Table 1 coefficients ARE verified.

