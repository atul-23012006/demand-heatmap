# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Intent Layer

This project is small and flat (every directory is well under the token
threshold for its own node), so there are no child `AGENTS.md` files — this
document is the single source of truth. **Read the Architecture section below
before touching `scripts/` or `backend/main.py`**; several steps look
harmless to change in isolation but encode a specific reason.

### Global Invariants

- The `scripts/*.py` pipeline must run in order (download → prepare_zones →
  aggregate → train_model); each step's output is the next step's input, and
  there is no other coupling between them. `build_travel_matrix.py` only
  depends on `prepare_zones.py`'s output and can run any time after it.
- `data/raw/`, `data/processed/`, `backend/model_artifacts/` are generated,
  git-ignored, and must stay reproducible from `scripts/` alone — never hand-edit
  files in them.
- The served prediction window is exactly the precomputed test split in
  `data/processed/predictions.parquet` (see `/api/meta`). Don't add API
  parameters that imply live/future forecasting without also changing how
  predictions are computed — right now there's no online inference path.

## What this is

A ride-hailing demand heatmap predictor for NYC. It trains a demand-forecasting
model on real NYC TLC yellow-taxi trip data and serves predictions through a
FastAPI backend with a Leaflet-based map UI: a "best zones to reposition to"
recommender for a single driver, a multi-driver fleet load-balancer, and an
earnings simulator comparing repositioning strategies.

## Commands

```bash
uv sync                                   # install dependencies

# Data pipeline — run once, in this exact order (each step's output feeds the next):
uv run python scripts/download_data.py    # downloads ~155MB: 3 months of TLC parquet + zone shapefile/lookup + weather -> data/raw/
uv run python scripts/prepare_zones.py    # shapefile -> zone centroids/reference table -> data/processed/zones.parquet
uv run python scripts/aggregate.py        # raw trips -> dense hourly (zone x hour) demand panel + avg fare per zone -> data/processed/{hourly_demand,zone_fares}.parquet
uv run python scripts/train_model.py      # feature engineering + train + baseline comparison -> backend/model_artifacts/, data/processed/predictions.parquet
uv run python scripts/build_travel_matrix.py   # real OSRM driving-time matrix, all 263x263 zone pairs -> data/processed/travel_matrix.parquet

uv run uvicorn backend.main:app --reload --port 8123   # run the app at http://127.0.0.1:8123/

uv run playwright install chromium        # one-time; needed for tests/test_ui.py
uv run pytest                             # run the test suite (API/UI tests skip if the pipeline hasn't been run)

docker compose up --build                 # containerized run; see entrypoint.sh for pipeline auto-bootstrap
```

There is no lint tooling configured in this repo yet.

## Architecture

**Pipeline is one-directional and file-based** — each `scripts/*.py` reads
Parquet/CSV from the previous step and writes Parquet/JSON for the next. There
is no shared in-process state between scripts; rerunning any step from scratch
is always safe (idempotent downloads skip existing files).

- `scripts/download_data.py` — pulls 3 months of NYC TLC yellow-taxi Parquet,
  the zone lookup CSV, and the official taxi-zone shapefile. The shapefile is
  in NY State Plane feet (EPSG:2263); this script reprojects it to WGS84
  (EPSG:4326) via `pyproj` before writing `taxi_zones.geojson` — Leaflet needs
  lat/lon, and this is the only reprojection step in the pipeline.
- `scripts/prepare_zones.py` — computes a representative-point centroid per
  zone (via `shapely`) for the recommender's origin/destination points.
- `scripts/build_travel_matrix.py` — real driving times between all 263x263
  zone pairs via OSRM's free public demo table API
  (router.project-osrm.org). The demo server caps table requests at
  ~100-119 coordinates, so this tiles the matrix: zones split into groups of
  45, one request per group-pair (21 requests total for 263 zones), diagonal
  and off-diagonal blocks assembled separately to avoid redundant calls. If
  you change `GROUP_SIZE`, keep group-pair coordinate counts (up to 2x group
  size) comfortably under 100. Output is long-form
  (`zone_from, zone_to, travel_min`); `backend/main.py` pivots it to a wide
  matrix at startup for O(1) lookups.
- `scripts/aggregate.py` — produces a **dense** zone × hour panel (cross join
  of all 263 zones × every hour in range, zero-filled), not just hours with
  trips. This matters: without zero-filling, lag/rolling features would have
  silent gaps and the model would never see "no demand" hours. It also
  computes `data/processed/zone_fares.parquet` (avg fare per zone, training
  period only) for the earnings simulator — zones with no training-period
  trips fall back to the citywide average rather than NaN.
- `scripts/train_model.py` — trains `HistGradientBoostingRegressor` (Poisson
  loss) on calendar features + lag features (1h/24h/168h) + rolling means +
  hourly weather (temp/precip/snow/wind, city-wide from `data/raw/weather.csv`).
  `zone_id` is deliberately kept as a **numeric** (not categorical) feature —
  sklearn's native categorical handling caps cardinality at 255, and there are
  263 zones. Train/test split is temporal (train: Jan 1–Mar 17 2024, test:
  Mar 18–31 2024), never random, to avoid leaking future data into training.
  Baseline for comparison is historical mean trip count by (zone, day-of-week,
  hour), fit on the training split only. Note: weather features were added
  expecting a meaningful lift and didn't move MAE materially either way —
  left in as honest signal, not because they're load-bearing for the result.
- `backend/main.py` — FastAPI app. Loads all model artifacts and precomputed
  predictions **once at startup** into module-level globals (`_predictions`,
  `_zones`, `_metrics`, `_zone_geojson`, etc.) — there is no per-request
  recomputation or lag-feature reconstruction. This is why the served date
  range is fixed to the precomputed test window (`/api/meta` reports the
  actual bounds): predictions are a genuine backtest against the held-out
  window, not a live forecast, and the UI intentionally can't be pointed at
  dates outside `data/processed/predictions.parquet`.
- `/api/recommend` scores candidate zones as
  `predicted_demand / (1 + travel_min/10)`, where `travel_min` comes from the
  precomputed OSRM matrix (`_travel_wide` in `backend/main.py`, built from
  `data/processed/travel_matrix.parquet`). `AVG_SPEED_KMH`/`_haversine_travel_min`
  are a fallback only, for a zone pair genuinely missing from the matrix — not
  the primary path. `_score_zones_for_driver` is the shared helper for this;
  both `/api/recommend` and the per-driver legs of `/api/simulate` and
  `/api/recommend_batch` call it, so there is one scoring definition, not three.
- `TRIPS_PER_DRIVER_SATURATION` (default 8.0, `backend/main.py`) is the one
  heuristic constant behind both stretch features: predicted zone demand this
  saturated is treated as "one productive driver's worth" of hourly pickup
  capacity. It drives pickup probability in `/api/simulate` and per-zone
  capacity in `/api/recommend_batch`. It is not calibrated against real fleet
  data — both endpoints say so in their own response payload / UI copy, and
  that framing should be preserved if this constant is tuned.
- `/api/recommend_batch` load-balances multiple simultaneous drivers with a
  **greedy** assignment (highest driver×zone score first, decrementing
  per-zone capacity as it goes), not an optimal matching (e.g. Hungarian
  algorithm) — deliberate, since candidate pool is capped
  (`candidate_pool_size`, default 40 zones) and driver counts are small. Any
  driver whose top candidates are all at capacity gets a fallback
  "best available regardless of capacity" assignment flagged
  `over_capacity: true` rather than being left unassigned.
- `/api/simulate` replays one real day of precomputed predictions (never
  live/future) and is intentionally a from-scratch hour-by-hour simulation,
  not a reuse of `/api/recommend` — its "follow the model" strategy has to
  track a moving current-zone across 24 hours and account for travel time
  eating into the destination hour's capture probability, which the
  single-shot recommend endpoint doesn't need to do.
- `frontend/` is a single vanilla-JS page (no build step, no framework). Map
  tiles use standard OSM raster tiles with a CSS `filter: invert(1)
  hue-rotate(180deg) ...` trick (`.map-tiles-dark` in `styles.css`) to fake a
  dark basemap — this was a deliberate substitution for CartoDB's dark tiles,
  which now require an API key. The filter is scoped to the tile layer's own
  container, so it does not affect the GeoJSON zone overlay drawn in Leaflet's
  separate overlay pane.
- Demand values are colored using a **sqrt-scaled** color function
  (`colorForDemand` in `app.js`) because raw demand is heavily right-skewed
  (a handful of Manhattan/airport zones dominate) — linear scaling would make
  most of the map look uniformly near-zero.
- The Map tab has two click modes (`uiMode` in `app.js`): "single" (existing
  click-a-zone → recommend flow) and "fleet" (click up to
  `MAX_FLEET_DRIVERS` zones to place drivers, then "Balance Fleet" calls
  `/api/recommend_batch`). Both share the same Leaflet map instance and zone
  layer — there is deliberately no second map for fleet mode.
- Tests (`tests/`): `test_feature_engineering.py` runs standalone against
  pure functions in `scripts/train_model.py`. `test_api.py` uses a
  session-scoped `client` fixture (`tests/conftest.py`) that skips the whole
  module if `data/processed/predictions.parquet` doesn't exist yet, rather
  than failing — `backend/main.py` loads artifacts at import time, so
  importing it without the pipeline having run would otherwise crash
  collection. `scripts/__init__.py` and `backend/__init__.py` exist only so
  tests can import from them; `pythonpath = ["."]` in `pyproject.toml` makes
  the repo root importable.
- `test_ui.py` uses Playwright against a **real uvicorn subprocess**
  (`live_server` fixture in `conftest.py`), not the ASGI TestClient — a
  browser needs actual HTTP-served HTML/JS/CSS, not an in-process ASGI app.
  `live_server` picks a free port itself so tests don't collide with a
  `--reload` dev server you might have running. Zone selection and fleet
  driver placement are driven via `page.evaluate("selectZone(161)")` /
  `toggleFleetDriver(...)` (both are plain top-level functions in `app.js`,
  a classic script not a module, so they're already on `window`) rather than
  clicking specific map pixel coordinates, which would be brittle against
  zoom/projection changes. Needs `uv run playwright install chromium` once
  before first run — not installed automatically by `uv sync`.
- Docker (`Dockerfile`, `docker-compose.yml`, `entrypoint.sh`): the image
  installs dependencies at build time via `uv sync --frozen --no-dev`, and
  `entrypoint.sh` always runs `uv run --no-sync` — the `--no-sync` matters,
  without it `uv run` tries to reconcile the `--no-dev` build-time venv
  against the full lockfile (which includes the `dev` group) and hits the
  network on every container start. `data/` and `backend/model_artifacts/`
  are volume-mounted so the data pipeline only runs once, not on every
  `docker compose up`.

## Data notes

- `data/raw/`, `data/processed/`, and `backend/model_artifacts/` are
  git-ignored — they're regenerated by the pipeline above, not checked in.
- Zone IDs 264 ("Unknown") and 265 ("Outside of NYC") from the TLC zone
  lookup are excluded throughout — they have no geometry in the shapefile.
