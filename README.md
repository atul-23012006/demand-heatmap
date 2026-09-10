# NYC Ride-Hailing Demand Heatmap Predictor

Predicts near-term pickup demand for NYC taxi zones and recommends where a
driver should reposition to, using real NYC TLC yellow-taxi trip data
(Jan–Mar 2024).

- **Data**: 3 months of real NYC TLC trip records (~8.9M trips), aggregated
  to hourly pickup counts per taxi zone (263 zones).
- **Model**: `HistGradientBoostingRegressor` (Poisson loss) trained on
  time/calendar features + lag/rolling demand features, benchmarked against
  a historical-mean-by-(zone, day-of-week, hour) baseline on a held-out
  2-week test window (Mar 18–31 2024). Model beats baseline by ~11% MAE.
- **Serving**: FastAPI backend serves precomputed predictions for the test
  window; a Leaflet map renders a live choropleth heatmap with a
  date/hour picker, per-zone drill-down, and a "best zones to reposition
  to" recommender (predicted demand discounted by centroid-distance travel
  time).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python scripts/download_data.py   # downloads ~155MB of NYC TLC data + zone shapes
uv run python scripts/prepare_zones.py   # zone centroids + reference table
uv run python scripts/aggregate.py       # builds the hourly zone x hour demand panel
uv run python scripts/train_model.py     # trains the model, writes metrics + predictions
```

## Run

```bash
uv run uvicorn backend.main:app --reload --port 8123
```

Then open http://127.0.0.1:8123/

## Project layout

```
scripts/           data prep & training pipeline (run once, in order above)
backend/main.py     FastAPI app: serves the API + the static frontend
backend/model_artifacts/  trained model + metrics (generated)
frontend/           vanilla JS + Leaflet single-page UI
data/raw/           downloaded TLC parquet, zone shapefile→geojson, zone lookup (generated)
data/processed/     aggregated hourly demand panel + precomputed predictions (generated)
```

## Notes

- The demo window (Mar 18–31 2024) is a genuine held-out backtest, not a
  live forecast — the UI's date range is intentionally bounded to it so
  every prediction shown can be checked against what actually happened.
- Travel time between zones is a straight-line-distance proxy (haversine /
  assumed avg. speed), not a real routing engine — noted as a stretch goal.

## Stretch goals (not implemented)

- Driver earnings simulator (replay a shift, "follow the model" vs. "stay put")
- Multi-driver load balancing so the recommender doesn't send every driver
  to the same hotspot
