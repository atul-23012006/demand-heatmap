#!/bin/sh
set -e

if [ ! -f data/processed/predictions.parquet ]; then
    echo "Artifacts missing -- running data pipeline (first run only, ~3-5 min)..."
    uv run --no-sync python scripts/download_data.py
    uv run --no-sync python scripts/prepare_zones.py
    uv run --no-sync python scripts/aggregate.py
    uv run --no-sync python scripts/train_model.py
else
    echo "Found existing data/processed/predictions.parquet -- skipping data pipeline."
fi

exec uv run --no-sync uvicorn backend.main:app --host 0.0.0.0 --port 8000
