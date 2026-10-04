# Brief: paraglider tracker (OGN → SQL) with a minimal modern map UI

Self-contained brief. Read all of it before writing code.

## 0. How to work

You are a senior full-stack engineer building this project end to end.

- Deliver **complete, runnable files**: no stubs, no "…", no TODO placeholders. Show the file tree first, then the files.
- Work in the milestones of §10. After each milestone list the files, say how to run and test it, then **stop and wait for my "continue"** (keeps answers within output limits).
- If you can execute code, run the tests and the fake-feed end-to-end test yourself and report real results, including failures. If you cannot execute code, say so and be extra careful.
- Do not invent OGN protocol details. §4 and §5 are verified facts. If something contradicts the official docs (§12), tell me.
- Ambiguity: take the default given here and mention it in one line. Ask at most 3 questions, only if truly blocked.

## 1. Goal

A small self-hosted program that

1. connects to the **Open Glider Network (OGN)** live feed (APRS-IS),
2. keeps **paragliders only** (OGN aircraft type 7),
3. cuts the stream into flights and stores **every track point with all telemetry in an SQL database** for later analysis,
4. serves a **minimal, modern web UI**: live map with **labelled paragliders**, a **2D / 3D** switch and a **calendar to go back in time**.

Region: **Switzerland**, but a region is a config file, so other countries are easy to add. It runs on my **Windows PC** (Python, PowerShell) now and on a small Linux VPS (Oracle Always Free) later, so keep CPU, RAM and disk use low.

## 2. Background: what was built before

A larger predecessor, "prack" (paragliding & track), was built and tested over many iterations and ran on my Windows PC against the real Swiss feed. It had: a live map of paragliders, hang gliders and gliders (FLARM, FANET, OGN trackers, ADS-L, Naviter, Flymaster …); details and route; altitude/time chart with filled ground; switchable 3D map with 3D track; flight database (SQLite, PostgreSQL optional) with IGC/KML/GPX/CSV export; calendar; hide/unhide; PG/HG/GL filter; daily weather archive; cockpit-style UI; Docker/Caddy deployment; about 50 pytest tests, headless-browser UI checks and a fake OGN server for end-to-end tests. Stack: Python 3.11, FastAPI, SQLAlchemy 2, SQLite (WAL), MapLibre GL + deck.gl, vanilla ES modules, no build step.

Problems found on real data shaped the rules in §5. **Those rules are the most valuable part of this brief.** The new project is a **slim rewrite focused on tracking quality**. Keep what proved necessary, drop everything under "Out of scope".

If you can open it, the reference implementation is https://github.com/betafant/prack (branch `claude/youthful-dirac-sji4dt`): `prack/ogn/parser.py`, `prack/ogn/constants.py`, `prack/tracking/tracker.py`, `prack/models.py`, `prack/static/js/map.js`. If I attach files, treat them as the reference. Otherwise this brief is complete.

## 3. Scope

**In scope:** OGN ingest, parsing, paraglider filtering, privacy flags; flight detection; de-duplication across protocols; SQL storage with one fix per second; terrain elevation; live map with labels, 2D/3D, selected glider's track, calendar/history; CLI, diagnostics, tests; README with Windows quick start; Docker for the VPS.

**Out of scope, do not build:** hang gliders, gliders and other aircraft types; weather archive; IGC/KML/GPX export UI; altitude chart or vario instrument; hide/unhide; filter/sort UI; login screens or accounts; replay slider; pilot profiles.

## 4. OGN facts (verified)

**Connection**
- TCP `aprs.glidernet.org:14580`. Login line, CRLF-terminated: `user <CALL> pass -1 vers <app> <version> filter a/<N>/<W>/<S>/<E>`. `pass -1` = read-only. `<CALL>`: at most 9 characters, unique per connection (e.g. `PRACK` + 4 random digits).
- Area filter = region bbox + 30 km margin, so flights that cross the border keep being tracked. Switzerland: bbox W 5.85, S 45.75, E 10.55, N 47.85, which gives `a/48.120/5.456/45.480/10.944`.
- Send `#keepalive` every 240 s. Socket read timeout 90 s (the server sends a `#` comment about every 20 s). On any error reconnect with exponential backoff 5 s → 300 s. Lines starting with `#` are comments.
- Reader thread → bounded queue (about 50 000 lines, count drops) → processing thread. Never do DB I/O in the reader.

**Beacon format:** `CALLSIGN>TOCALL,qAS,RECEIVER:payload`
- Position payload: `/HHMMSSh` (UTC time only: take the date from the reception time and handle midnight roll-over; a `z` variant with DDHHMM exists), `DDMM.mmN`, symbol-table char (`/` or `\`), `DDDMM.mmE`, symbol char (varies: `'` `g` `^` `n` …), optional `CCC/SSS` (course in degrees, speed in **knots**), `/A=aaaaaa` (altitude in **feet**), then space-separated tokens: `!Wxy!` (third decimal digit of the lat/lon minutes, add digit/1000 minute), `id<hex>`, `±nnnfpm` (climb, ft/min), `±n.nrot` (turn rate, 1 rot = half turn per minute = 3 °/s), `n.ndB` (SNR), `ne` (bit errors), `±n.nkHz` (frequency offset), `gpsAxB`, `sN.NN`, `hXX`, `rXXXXXX`, `relayed`.
- Conversions: ft → m ×0.3048, kn → km/h ×1.852, fpm → m/s ×0.00508.
- Status lines (payload starts with `>`): FANET sends `Name="Mia"`, the pilot name of that address.
- Skip: `#` lines; receiver and ground-station beacons (tocall `OGNSDR`, `OGNDVS`, `OGNEMO`, `OGNSXR`, or a path containing `TCPIP` / `qAC`); weather symbol `_`; positions without altitude. Relayed FLARM messages (`relayed` token or receiver) are normal, valid positions.

**The `id` field** (`idXXXXXXXX`, 8 hex digits) = flags byte + 24-bit address. Bit 7 stealth, bit 6 no-tracking, bits 5–2 aircraft type, bits 1–0 address type (0 random, 1 ICAO, 2 FLARM, 3 OGN). Example: `id1E112880` → type 7, address type 2, address `112880`.
Aircraft types: 0 unknown, 1 glider, 2 tow plane, 3 helicopter, 4 skydiver, 5 drop plane, 6 hang glider, **7 paraglider**, 8 powered, 9 jet, 10 UFO, 11 balloon, 12 airship, 13 drone, 14 ground support, 15 static object.

**Other id formats; all must be handled**
- **Naviter**: 10 hex digits = 40 bits: bit 39 stealth, bit 38 no-track, bits 34–37 type, bits 28–33 address type, low 24 bits address.
- **Flymaster** (`OGFLYM`) and **Capturs** (`OGCAPT`) send **no id at all**. Flymaster users are paragliders: type 7, address taken from the callsign.
- **Wingman**: flags byte followed by its own text id. **AirMate**: plain 6-hex address, type 0.
- **LiveTrack24, SPOT, Spider, SkyLines, inReach …** carry no aircraft type, so type 0: ignored by default.

**Source = APRS destination ("tocall", strip a `-N` suffix):** `OGFLR`/`OGNFLR`/`OGFLR6`/`OGFLR7` FLARM, `OGNFNT` FANET, `OGNTRK` OGN tracker, `OGADSL` OGN tracker with ADS-L, `OGADSB`/`OGNADSB` ADS-B, `OGNAVI` Naviter, `OGFLYM` Flymaster, `OGNSKY` SafeSky, `OGNPUR` PureTrack … (full list: `tocalls.txt`). The callsign prefix (`FLR`, `FNT`, `ICA`, `OGN`, `NAV` …) plus the 6-hex address identifies the transmitter. **The same address can arrive under several prefixes.**

**DDB** (optional, recommended): `https://ddb.glidernet.org/download/?j=1&t=1` returns JSON `devices[]` with `device_id` (= address), `aircraft_model`, `registration`, `cn` (competition number), `tracked` (Y/N), `identified` (Y/N), `aircraft_type`. Load at start, refresh daily, cache in the DB.

## 5. Tracking rules

Per device, keyed by its 24-bit address (see rule 4).

**Filter pipeline.** Count every drop reason and expose the counters.
1. Type must be 7 (config `PRACK_TRACKED_TYPES`, default `7`).
2. Privacy: drop if the no-tracking flag is set, if the stealth flag is set (config `PRACK_RESPECT_STEALTH`, default on), or if DDB says `tracked=N`. Show registration and competition number only when DDB says `identified=Y`.
3. Time sanity: drop if the timestamp is more than 2 min in the future or more than 30 min old, and drop non-increasing timestamps per device.
4. Glitches: a jump of more than 2 km that implies more than 500 km/h is rejected (at most 3 in a row, then accept it as real).
5. Plausibility: rule 2 below.
6. Region: a flight may only *start* inside a region bbox.

**State machine**
- Not flying: keep the last 60 s of fixes in a pre-takeoff buffer.
- Takeoff: 2 consecutive fixes with ground speed ≥ 15 km/h open a flight. Also store the buffered fixes so the launch run is included.
- Flying: store every fix, at most one per `PRACK_MIN_FIX_INTERVAL` (default 1 s).
- Landed: speed < 5 km/h and within 150 m of an anchor point for 4 min (and AGL < 80 m when terrain is known, otherwise |vario| ≤ 0.5 m/s). Close with `close_reason=landed`; `landing_time` = start of the stationary period.
- Gap: no data for 20 min closes the flight as `gap`. If the device is airborne again within 90 min, **resume the same flight** (coverage holes in the Alps). On app restart, flights left `active` are closed as `gap` and may resume the same way.
- On close, a background **finalizer** fills terrain elevation per fix (`ground`), computes statistics (distance, straight distance, max altitude, max AGL, altitude gain, best climb/sink over about 20 s, max speed), sets `airborne` (max AGL ≥ 50 m and duration ≥ 60 s; without terrain data: duration ≥ 60 s and altitude range > 100 m or max speed > 20 km/h), and stores an RDP-simplified `preview` path (at most about 500 points) for the day overview. Walking, driving and GPS noise must not count as flights.
- Live state (memory only): everything heard in the last 10 min, including grounded paragliders, and the last ~360 positions of each as trail.

**Hard-won rules.** Each fixed a real bug on real data. Implement all of them.

1. **No ADS-B "paragliders".** The ADS-B emitter category "ultralight / hang glider / paraglider" arrives as type 7 for tocalls `OGADSB` / `OGNADSB`. Real paragliders carry no ADS-B transponder; microlights do. Force type 0 (unknown) for ADS-B. (A "paraglider" flying 306 km/h showed up on the map.)
2. **Speed plausibility.** A paraglider flying faster than 130 km/h in at least 2 of the last 10 fixes is a mis-configured device: drop it, delete its open flight, ignore it from then on. A single spike is just discarded. Purge such flights from the DB at startup too.
3. **Decode every id format** (§4). Whole sources silently vanished at first (Naviter, Flymaster …). Build a `diagnose` command that lists received sources and aircraft types and the reason for every dropped class. Compare with https://live.glidernet.org.
4. **One pilot on several protocols = one aircraft, one track.** FLARM + FANET (or FANET + ADS-L) of the same pilot arrive as `FLR112880`, `FNT112880`, `ICA112880`.
   - Unify by address when the other callsign's last position is < 3 km and < 10 min away.
   - Priority **FLARM > FANET > OGN tracker > ADS-L**. FLARM wins because it sends about 1 position per second with climb and turn rate, the most detailed track.
   - While a better source was heard in the last 30 s, ignore positions of lower-ranked sources. Otherwise the track zig-zags between two devices. They only fill gaps.
   - Keep the FANET **pilot name** even when FLARM wins.
   - If the preferred source shows up mid-flight, switch the open flight and device identity to it.
   - If two flights already exist for one device, merge them: copy the dropped flight's fixes only where the kept flight has no data. Also do this at startup.
   - Identity matching must also work for states restored from the DB after a restart.
   ```python
   SOURCE_PRIORITY = {"FLARM": 0, "FANET": 1, "OGN tracker": 2, "OGN tracker (ADS-L)": 3}  # lower = preferred
   prio = lambda source: SOURCE_PRIORITY.get(source, 9)
   state.source_seen[b.source] = b.timestamp
   best = min(prio(s) for s, seen in state.source_seen.items()
              if abs((b.timestamp - seen).total_seconds()) <= 30)
   if prio(b.source) > best:
       return  # a better source is being heard: ignore this position
   ```
5. **Maximum detail.** Store every fix (1/s for FLARM) with `INSERT … ON CONFLICT DO NOTHING` on (flight_id, ts), and push every new position to the browser each second (see API).
6. **Time.** Store UTC. `flights.date` = the *local* date (Europe/Zurich) of the flight start. Windows has no time zone database, so depend on the `tzdata` package.
7. **Windows MIME quirk.** Python's `mimetypes` can serve `.js` as `text/plain` on Windows (registry), which breaks ES modules. Register `.js` and `.mjs` as `text/javascript`, plus `.css`, `.svg`, `.woff2`, explicitly at startup.
8. **Front-end library versions.** MapLibre GL JS **5.x** (e.g. 5.24.0) with deck.gl **9.4** (`MapboxOverlay`, `interleaved: true`). MapLibre 6 removed `map.transform`, which deck.gl 9.4 reads. Pin exact versions and vendor the files locally (no CDN, no Node needed to run).
9. **deck.gl resize bug.** In interleaved mode the overlay shifts after the map container is resized, because luma.gl's default framebuffer keeps a stale size. On map `resize` and `render`, set the framebuffer size to the canvas size. Call `map.resize()` before camera animations whenever a panel changes size.
10. **3D camera.** When fitting a track in 3D, cap zoom at about 12 and pass the terrain height as camera `elevation` (or use `setCenterClampedToGround`), otherwise the camera ends up inside a mountain. 3D = `map.setTerrain({source:'dem', exaggeration:1})` with pitch about 60°; 2D = `setTerrain(null)` with pitch 0. Draw 3D tracks at **GPS altitude MSL**, which matches the DEM.
11. **Stable selection.** When an aircraft's identity switches (FNT → FLR), keep the selection and the trail (match by address).
12. **OGN etiquette.** Read-only login, one connection, identify the app in `vers`, back off on errors.

## 6. Database

SQLite by default (WAL, `synchronous=NORMAL`, `busy_timeout`, `foreign_keys=ON`), PostgreSQL optional via a URL. SQLAlchemy 2.x. All timestamps UTC.

The `fixes` table must be compact: integer-scaled columns, composite primary key, `WITHOUT ROWID`. That gives about 62 bytes per fix, against about 190 with REAL/DATETIME columns. A 2 h FLARM flight is about 7 200 fixes (0.5 MB); a busy Swiss day with hundreds of flights is about 100–300 MB.

```
devices(id PK, callsign UNIQUE, address, address_type, aircraft_type, source, registration,
        competition_id, model, pilot_name, first_seen, last_seen)
          -- callsign = identity of the preferred source, e.g. FLR112880
flights(id PK, device_id FK, region, date /*local*/, status /*active|closed*/, close_reason /*landed|gap*/,
        airborne, source, start_time, end_time, takeoff_time, landing_time, fix_count,
        takeoff_lat, takeoff_lon, takeoff_alt, landing_lat, landing_lon, landing_alt,
        min_lat, max_lat, min_lon, max_lon, max_alt, min_alt, max_agl, alt_gain,
        max_climb, max_sink, max_speed, distance_km, straight_km, max_from_start_km, ground_filled,
        preview /*JSON [[lon,lat,alt],...] simplified: day overview only, NOT the track*/,
        created_at, updated_at)                                   -- index (date, region)
fixes(flight_id FK, ts /*epoch s*/, lat /*deg*1e6*/, lon /*deg*1e6*/, alt /*m*10, GPS MSL*/,
      ground /*m*10, terrain below*/, speed /*km/h*10*/, track /*deg*/, climb /*m/s*100*/,
      turn /*deg/s*10*/, src /*0 FLARM, 1 FANET, 2 OGN tracker, 3 ADS-L: protocol of this fix*/,
      receiver, signal /*dB*10*/, errors, freq_offset /*kHz*10*/, gps,
      PRIMARY KEY (flight_id, ts)) WITHOUT ROWID
ddb(address PK, model, registration, competition_id, tracked, identified, ddb_aircraft_type)
```

**Analysis-friendly views are required**, so nobody working with the data ever sees scaled integers:
- `fixes_v`: real units (`utc`, `local_time`, `lat`, `lon`, `alt_m`, `ground_m`, `agl_m`, `speed_kmh`, `heading`, `vario_ms`, `turn_dps`, `source`) joined with pilot and flight columns.
- `flights_v`: durations in minutes, local date and times.

In the README, include ready-to-run SQL and pandas examples: flights per day, activity per hour, launch sites by takeoff lat/lon, thermals (sustained vario > 0.5 m/s while turning), receiver coverage by signal.

## 7. Backend and API

- Python ≥ 3.11, FastAPI + uvicorn, SQLAlchemy 2, httpx, Pillow (DEM decoding), tzdata. One process: APRS reader thread → bounded queue → tracker (single consumer, lock-protected state) → batched DB writer (flush about every second, flight summaries about every 10 s) → finalizer thread. A DDB refresher thread. The SSE endpoint polls `tracker.live(since)` every second.
- Config via environment variables `PRACK_*` and an optional `.env`: host (default 127.0.0.1), port, data dir, database URL, regions, OGN host/port/callsign, min fix interval, tracked types, respect stealth, optional basic-auth user/password. A region is a TOML file (id, name, timezone, bbox, center, zoom, base maps): adding a country = adding a file.
- Terrain: Terrarium DEM tiles `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png` (elevation in m = R·256 + G + B/256 − 32768). Download on demand, cache on disk, sample bilinearly at z12 (about 30 m in the Alps) for `ground` / AGL. The same tiles go to the browser via `/api/dem/{z}/{x}/{y}.png`.
- CLI `prack`: `run`, `init-db`, `demo` (simulated paragliders, no network), `replay <file>` (feed a recorded APRS log through the tracker), `diagnose [seconds]`.
- JSON API:
  - `GET /api/config`: regions (bbox, center, zoom, timezone, base maps), terrain tile URL and encoding, live window, version.
  - `GET /api/live/stream` (SSE, one message per second) and `GET /api/live` (snapshot). Message: `{seq, now, full, aircraft:[…], removed:[ids]}`. Delta since the last `seq`, plus a full snapshot with trails every 60 s. Aircraft: `id, address, name, pilot, reg, cn, src, flight_id, flying, takeoff, t, lat, lon, alt, gnd, spd, vs, hdg` and `pts`, **all positions received since the last message** as `[t, lon, lat, alt, spd, vs, hdg, gnd]`, so the browser appends full-resolution points.
  - `GET /api/days?start=&end=`: `[{date, total}]` of airborne flights per local day (calendar shading).
  - `GET /api/days/{date}/flights`: flights of a day with id, label, summary stats and `preview`.
  - `GET /api/flights/{id}` and `GET /api/flights/{id}/track`: columnar arrays `{t[], lat[], lon[], alt[], gnd[], spd[], vs[], hdg[]}`.
  - `GET /api/dem/{z}/{x}/{y}.png`, `GET /api/health`, `GET /api/status` (link state, counters per drop reason, received sources and types, DB size).
- FastAPI serves the static front-end. Optional HTTP basic auth. Default bind 127.0.0.1.

## 8. UI: minimal and modern

One page, **full-screen map**. Everything else is a few small floating "glass" controls. No sidebars, tables or charts.

- **Top bar** (floating): wordmark; segmented control **Live | History**; **2D | 3D**; **calendar button** showing the selected date; theme toggle; connection dot (green = OGN link up). One chip **Ground** shows grounded paragliders, dimmed (default off).
- **Live:** all airborne paragliders heard in the last 10 min. Each is a heading-rotated marker with a **label**. Name priority: FANET pilot name → `competition id + registration` → registration or competition id → callsign. Below the name: altitude and vario, e.g. `Mia · 1 587 m · −1.9`. Declutter: names from zoom about 9, details when zoomed in or selected. Faded trail of the last ~6 min. It must stay smooth with 300 or more aircraft, so use deck.gl `IconLayer` + `TextLayer`, not DOM markers.
- **Select** (click marker or label): highlight it and draw the **whole flight track** (from `/track`, then live points appended), coloured by altitude. A floating card shows name, source, altitude/AGL, speed, vario, heading, take-off time, duration, distance and a **Follow** toggle; on phones it is a bottom sheet. The selection survives identity switches.
- **2D / 3D:** 2D is a flat map. 3D shows DEM terrain (exaggeration 1), pitch about 60°, sky/fog, the track as a 3D line at true altitude plus a faint vertical curtain to the ground, with a smooth camera transition (see rule 10).
- **History / calendar:** popover month grid, each day shaded by flights per day; ◀ ▶ day buttons and "Today". Picking a day loads all paraglider flights of that day and draws their `preview` tracks (thin, semi-transparent, coloured by altitude) in the current 2D/3D view. Clicking a track selects it (full track + card). A compact chip list of the day's flights is optional.
- Base maps: swisstopo WMTS (grey / colour / aerial) from the region file; one switch is enough.
- The URL hash holds the state (`#/live`, `#/day/2026-07-15`, `sel=`, `view=3d`), so views are bookmarkable.
- Times in region local time (Europe/Zurich); units m, km/h, m/s. UI language English, all strings in one file.
- **Style:** modern and minimal, in the spirit of Apple Maps or Linear. Dark by default plus a light theme (`prefers-color-scheme` and a toggle). Glass panels (backdrop blur, 14 px radius, hairline borders, soft shadows), one accent colour, Inter or system font with tabular numbers, subtle motion that respects `prefers-reduced-motion`, touch targets ≥ 44 px, responsive from phone to desktop, visible keyboard focus. No skeuomorphic cockpit instruments.

## 9. Quality

- **pytest**, at least 40 tests:
  - parser golden lines (§11);
  - tracker: take-off and landing, min fix interval, gap and resume, restart restore, privacy and type filters, ADS-B → unknown, speed plausibility (single spike vs sustained);
  - FLARM + FANET + ADS-L unification: identity, pilot name, FANET only fills FLARM gaps, one flight, merge of two existing flights, restart;
  - compact-schema round trip;
  - API: `days`, `track`, SSE `pts`.
- A synthetic APRS **line builder** for tests, and a **fake OGN TCP server** (login banner, then lines at 1 Hz) for end-to-end runs without internet.
- Front-end smoke test with Playwright (headless Chromium; WebGL needs `--use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist`; set locale `en-GB`): markers appear, 3D toggle works, a calendar day shows tracks, no console errors. Save screenshots.
- `ruff` clean, type hints on public functions.

## 10. Milestones (stop after each)

1. **Ingest:** skeleton, config, regions, APRS client, parser, DDB, `diagnose`, `replay`, parser tests.
2. **Tracker + DB:** models and views, state machine, de-duplication, finalizer, DEM service, fake feed, tests.
3. **API:** REST + SSE + DEM proxy, demo mode, API tests.
4. **Live UI:** map, labels, selection, 2D/3D, live trails.
5. **History UI + polish:** calendar and day view, themes, responsive layout, README (Windows quick start in PowerShell: `py -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, `pip install .`, `prack run`; config table; SQL examples), Dockerfile and compose, systemd unit.

**Acceptance:**
- `pip install .` then `prack run` on Windows 10/11 serves `http://127.0.0.1:8000` with the map.
- With internet, paragliders appear during the day and match live.glidernet.org (same airborne paragliders in Switzerland).
- `prack demo` works offline.
- Every landed flight is exactly one `flights` row with about 1 Hz `fixes`, never two rows for one pilot.
- `SELECT * FROM fixes_v LIMIT 5` shows real units.
- Tests and ruff pass.

## 11. Golden test lines (parser)

| Line | Expected |
|---|---|
| `FLR112880>OGFLR,qAS,Pizol:/183037h4702.30N/00926.07E'090/019/A=005300 !W00! id1E112880 -380fpm +0.0rot 12.5dB 0e +1.2kHz gps2x3` | FLARM, address `112880`, address type 2, type 7, lat 47.038333, lon 9.4345, alt 1615.4 m, 35.2 km/h, course 90, climb −1.93 m/s |
| `FNT1103CE>OGNFNT,qAS,FNB1103CE:/183727h5057.94N/00801.00Eg355/002/A=001042 !W10! id1E1103CE +03fpm` | FANET, type 7, lat 50.965683, lon 8.016667, alt 317.6 m, 3.7 km/h, course 355 |
| `FNT1118C1>OGNFNT,qAS,BelaVista:>191924h Name="FlrmAIC" 26.0dB -12.1kHz` | status beacon, pilot name `FlrmAIC` |
| `OGN3FF19F>OGNTRK,qAS,Rx:/090000h4638.71N/00814.12E'090/019/A=005000 !W00! id1D3FF19F +000fpm` | OGN tracker, type 7, address `3FF19F` |
| `ICA3FF19F>OGADSB,qAS,Rx:/090000h4638.71N\00814.12E^090/165/A=005000 !W00! id1D3FF19F +000fpm` | ADS-B: type forced to **0** (not a paraglider) |
| `FLM9A1B2C>OGFLYM,qAS,Rx:/090000h4638.71N/00814.12E'090/019/A=005000 !W00!` | Flymaster, no id field: type 7, address `9A1B2C` |
| `NAV042121>OGNAVI,qAS,NAVITER:/140648h4550.36N/01314.85E'090/152/A=001086 !W47! id0440042121 +000fpm +0.5rot` | Naviter 10-hex id: address `042121`, type 1 (glider, so ignored) |
| `FNB1103CE>OGNFNT,TCPIP*,qAC,GLIDERN3:/183738h5057.95NI00801.00E&/A=001042` | receiver/server beacon: ignored |

**Dual-protocol scenario for the tracker tests:** one pilot sends `FLR112880` every second and `FNT112880` every 4 s (positions about 11 m apart) plus the status line `FNT112880>OGNFNT,qAS,Rx:>090020h Name="Mia"`.
Expected:
- one device `FLR112880` with `pilot_name="Mia"`, one flight, and one fix per second;
- FANET fixes are stored only before FLARM is first heard, or while FLARM has been silent for more than 30 s;
- the live API shows exactly one aircraft.

## 12. References

- OGN APRS protocol, sample messages, tocalls: https://github.com/glidernet/ogn-aprs-protocol (`README`, `tocalls.txt`, `valid_messages/`, `Naviter_APRS_format.md`). Reference parser: https://github.com/glidernet/python-ogn-client
- Compare against: https://live.glidernet.org. Device database: https://ddb.glidernet.org
- APRS-IS filter syntax: http://www.aprs-is.net/javAPRSFilter.aspx
- Terrain tiles: https://registry.opendata.aws/terrain-tiles/
- swisstopo WMTS: `https://wmts.geo.admin.ch/1.0.0/<layer>/default/current/3857/{z}/{x}/{y}.jpeg`, layers `ch.swisstopo.pixelkarte-grau`, `ch.swisstopo.pixelkarte-farbe`, `ch.swisstopo.swissimage` (attribution "© swisstopo")
- MapLibre GL JS 5.x and deck.gl 9.4 documentation (`MapboxOverlay`)

## Appendix: verified reference code (port as is)

```python
HEADER_RE = re.compile(r"^(?P<callsign>[^>\s]{1,12})>(?P<tocall>[^,:]+)(?:,(?P<path>[^:]*))?:(?P<payload>.*)$")

POSITION_RE = re.compile(
    r"^(?:[/@](?P<time>\d{6})(?P<tfmt>[hz])|[!=])"
    r"(?P<lat>\d{4}\.\d{2})(?P<ns>[NS])(?P<symtab>.)"
    r"(?P<lon>\d{5}\.\d{2})(?P<ew>[EW])(?P<sym>.)"
    r"(?:(?P<course>\d{3})/(?P<speed>\d{3}))?"
    r"(?:/A=(?P<alt>-?\d{5,6}))?"
    r"(?P<rest>.*)$"
)
# each space-separated token of "rest" is matched against: !W(\d)(\d)! | id([0-9A-Za-z-]+) | ([+-]?\d+)fpm |
# ([+-]?\d+(\.\d+)?)rot | ([+-]?\d+(\.\d+)?)dB | (\d+)e | ([+-]?\d+(\.\d+)?)kHz | gps(\d+x\d+) | ...

def decode_time(value, fmt, reference):  # fmt "h": HHMMSS UTC, pick the day closest to the reception time
    h, m, s = int(value[0:2]), int(value[2:4]), int(value[4:6])
    candidate = reference.replace(hour=h, minute=m, second=s, microsecond=0)
    return min((candidate + timedelta(days=d) for d in (-1, 0, 1)),
               key=lambda c: abs((c - reference).total_seconds()))

def coord(value, deg_digits, hemisphere, extra):  # "4702.30", 2, "N", extra digit from !Wxy!
    minutes = float(value[deg_digits:]) + (extra / 1000.0 if extra is not None else 0.0)
    result = int(value[:deg_digits]) + minutes / 60.0
    return -result if hemisphere in ("S", "W") else result

HEX = set("0123456789ABCDEF")

def callsign_address(callsign):  # FLR112880 -> 112880
    tail = callsign[3:].upper()
    return tail if len(tail) == 6 and set(tail) <= HEX else callsign.upper()[:8]

def decode_id(value, callsign):
    """-> (address, address_type, aircraft_type, stealth, no_tracking)"""
    v = value.upper()
    if len(v) == 8 and set(v) <= HEX:        # flags byte STttttaa + 24 bit address
        flags = int(v[:2], 16)
        return v[2:], flags & 0x03, (flags >> 2) & 0x0F, bool(flags & 0x80), bool(flags & 0x40)
    if len(v) == 10 and set(v) <= HEX:       # Naviter, 40 bit
        n = int(v, 16)
        return v[4:], (n >> 28) & 0x3F, (n >> 34) & 0x0F, bool((n >> 39) & 1), bool((n >> 38) & 1)
    address = callsign_address(callsign)
    if len(v) > 8 and set(v[:2]) <= HEX and v[2] not in HEX:   # Wingman: flags byte + own id
        flags = int(v[:2], 16)
        return address, flags & 0x03, (flags >> 2) & 0x0F, bool(flags & 0x80), bool(flags & 0x40)
    if len(v) == 6 and set(v) <= HEX:        # AirMate: plain address, no type
        return v, 0, 0, False, False
    return address, 0, 0, False, False       # LiveTrack24, SPOT, Spider, SkyLines, ...: no type information
```
