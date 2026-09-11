# NYC Ride-Hailing Demand Heatmap Predictor

Predicts near-term pickup demand for NYC taxi zones and recommends where a
driver should reposition to, using real NYC TLC yellow-taxi trip data
(Jan–Mar 2024).

- **Data**: 3 months of real NYC TLC trip records (~8.9M trips), aggregated
  to hourly pickup counts per taxi zone (263 zones), plus hourly NYC weather
  (temperature/precipitation/snow/wind) from Open-Meteo.
- **Model**: `HistGradientBoostingRegressor` (Poisson loss) trained on
  time/calendar/weather features + lag/rolling demand features, benchmarked
  against a historical-mean-by-(zone, day-of-week, hour) baseline on a
  held-out 2-week test window (Mar 18–31 2024). Model beats baseline by
  ~9.4% MAE / ~9.4% WAPE. (Weather features turned out to add little beyond
  the lag/calendar signal — see Notes below.)
- **Serving**: FastAPI backend serves precomputed predictions for the test
  window; a Leaflet map renders a live choropleth heatmap with a date/hour
  picker, per-zone drill-down, and a "best zones to reposition to"
  recommender (predicted demand discounted by centroid-distance travel time).
- **Fleet load balancing**: a "Fleet (multi-driver)" mode on the map lets you
  place several drivers and get a load-balanced assignment — a greedy
  capacity-based matcher so drivers aren't all sent to the same single
  hotspot once its capacity is exhausted.
- **Earnings simulator**: replays one day of predicted demand and compares
  "stay in one zone all day" vs. "reposition every hour toward the
  best-scoring zone," with an hour-by-hour earnings chart.

## Tech stack

- **Backend**: Python, FastAPI, uvicorn
- **ML**: scikit-learn (`HistGradientBoostingRegressor`), pandas, DuckDB (data aggregation)
- **Frontend**: vanilla JS, Leaflet.js (no build step, no framework)
- **Data sources**: NYC TLC trip records, Open-Meteo weather API
- **Tests**: pytest + FastAPI TestClient
- **Deployment**: Docker, docker-compose

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python scripts/download_data.py   # downloads ~155MB: NYC TLC trips + zone shapes + weather
uv run python scripts/prepare_zones.py   # zone centroids + reference table
uv run python scripts/aggregate.py       # builds the hourly zone x hour demand panel + fare table
uv run python scripts/train_model.py     # trains the model, writes metrics + predictions
```

## Run

```bash
uv run uvicorn backend.main:app --reload --port 8123
```

Then open http://127.0.0.1:8123/

## Run with Docker

```bash
docker compose up --build
```

First run executes the full data pipeline inside the container (~3-5 min,
needs network access); `data/` and `backend/model_artifacts/` are mounted as
volumes so subsequent restarts skip straight to serving. Then open
http://127.0.0.1:8000/

## Tests

```bash
uv run pytest
```

Feature-engineering tests (`tests/test_feature_engineering.py`) run standalone.
API tests (`tests/test_api.py`) spin up the FastAPI app against the real
generated artifacts and are skipped automatically if the data pipeline hasn't
been run yet.

## Project layout

```
scripts/                  data prep & training pipeline (run once, in order above)
backend/main.py            FastAPI app: serves the API + the static frontend
backend/model_artifacts/   trained model + metrics (generated)
frontend/                  vanilla JS + Leaflet single-page UI
data/raw/                  downloaded TLC parquet, zone shapefile→geojson, zone lookup, weather (generated)
data/processed/            aggregated hourly demand panel, zone fares, precomputed predictions (generated)
tests/                     pytest suite (feature engineering + API)
Dockerfile, docker-compose.yml, entrypoint.sh   containerized deployment
```

## Notes

- The demo window (Mar 18–31 2024) is a genuine held-out backtest, not a
  live forecast — the UI's date range is intentionally bounded to it so
  every prediction shown can be checked against what actually happened.
- Travel time between zones is a straight-line-distance proxy (haversine /
  assumed avg. speed), not a real routing engine.
- Weather features (hourly temp/precip/snow/wind, city-wide) were added
  expecting a meaningful lift; in practice they moved model MAE by well
  under a point either direction. Kept in because they're honest signal and
  the code path is useful, but the finding itself is worth stating plainly:
  at this level of aggregation, lag/calendar features dominate and weather
  is mostly noise.
- The earnings simulator and fleet load balancer both use one heuristic
  constant (`TRIPS_PER_DRIVER_SATURATION` in `backend/main.py`, default 8):
  predicted demand this saturated is treated as "one productive driver's
  worth" of pickup capacity for that hour. It's not calibrated against real
  fleet sizes — both features are explicitly illustrative, not a fare or
  dispatch model, and say so in their own API responses / UI copy.
