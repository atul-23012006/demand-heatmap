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
  there is no other coupling between them.
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
FastAPI backend with a Leaflet-based map UI, including a "best zones to
reposition to" recommender for drivers.

## Commands

```bash
uv sync                                   # install dependencies

# Data pipeline — run once, in this exact order (each step's output feeds the next):
uv run python scripts/download_data.py    # downloads ~155MB: 3 months of TLC parquet + zone shapefile/lookup -> data/raw/
uv run python scripts/prepare_zones.py    # shapefile -> zone centroids/reference table -> data/processed/zones.parquet
uv run python scripts/aggregate.py        # raw trips -> dense hourly (zone x hour) demand panel -> data/processed/hourly_demand.parquet
uv run python scripts/train_model.py      # feature engineering + train + baseline comparison -> backend/model_artifacts/, data/processed/predictions.parquet

uv run uvicorn backend.main:app --reload --port 8123   # run the app at http://127.0.0.1:8123/
```

There is no lint or test tooling configured in this repo yet.

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
  zone (via `shapely`) for the travel-time proxy used by the recommender.
- `scripts/aggregate.py` — produces a **dense** zone × hour panel (cross join
  of all 263 zones × every hour in range, zero-filled), not just hours with
  trips. This matters: without zero-filling, lag/rolling features would have
  silent gaps and the model would never see "no demand" hours.
- `scripts/train_model.py` — trains `HistGradientBoostingRegressor` (Poisson
  loss) on calendar features + lag features (1h/24h/168h) + rolling means.
  `zone_id` is deliberately kept as a **numeric** (not categorical) feature —
  sklearn's native categorical handling caps cardinality at 255, and there are
  263 zones. Train/test split is temporal (train: Jan 1–Mar 17 2024, test:
  Mar 18–31 2024), never random, to avoid leaking future data into training.
  Baseline for comparison is historical mean trip count by (zone, day-of-week,
  hour), fit on the training split only.
- `backend/main.py` — FastAPI app. Loads all model artifacts and precomputed
  predictions **once at startup** into module-level globals (`_predictions`,
  `_zones`, `_metrics`, `_zone_geojson`, etc.) — there is no per-request
  recomputation or lag-feature reconstruction. This is why the served date
  range is fixed to the precomputed test window (`/api/meta` reports the
  actual bounds): predictions are a genuine backtest against the held-out
  window, not a live forecast, and the UI intentionally can't be pointed at
  dates outside `data/processed/predictions.parquet`.
- `/api/recommend` scores candidate zones as
  `predicted_demand / (1 + travel_min/10)`, where `travel_min` is a
  haversine-distance-over-assumed-speed proxy (`AVG_SPEED_KMH` in
  `backend/main.py`) — not a real routing engine.
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

## Data notes

- `data/raw/`, `data/processed/`, and `backend/model_artifacts/` are
  git-ignored — they're regenerated by the pipeline above, not checked in.
- Zone IDs 264 ("Unknown") and 265 ("Outside of NYC") from the TLC zone
  lookup are excluded throughout — they have no geometry in the shapefile.
