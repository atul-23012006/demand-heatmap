"""FastAPI backend for the ride-hailing demand heatmap predictor."""
import json
import math
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "backend" / "model_artifacts"
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="Ride-Hailing Demand Heatmap Predictor")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---- Load precomputed artifacts once at startup ----------------------------
_predictions = pd.read_parquet(DATA_DIR / "processed" / "predictions.parquet")
_predictions["pickup_hour"] = pd.to_datetime(_predictions["pickup_hour"])

_zones = pd.read_parquet(DATA_DIR / "processed" / "zones.parquet")
_zones_by_id = _zones.set_index("zone_id")

_metrics = json.loads((MODEL_DIR / "metrics.json").read_text())
_per_zone_metrics = pd.read_parquet(MODEL_DIR / "per_zone_metrics.parquet")

_raw_geojson = json.loads((DATA_DIR / "raw" / "taxi_zones.geojson").read_text())
_zone_geojson_features = []
for feat in _raw_geojson["features"]:
    zone_id = feat["properties"]["LocationID"]
    if zone_id not in _zones_by_id.index:
        continue
    row = _zones_by_id.loc[zone_id]
    _zone_geojson_features.append(
        {
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "zone_id": int(zone_id),
                "zone_name": row["zone_name"],
                "borough": row["borough"],
            },
        }
    )
_zone_geojson = {"type": "FeatureCollection", "features": _zone_geojson_features}

_available_hours = sorted(_predictions["pickup_hour"].unique())


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


AVG_SPEED_KMH = 22.0  # rough NYC in-traffic average, used only as a travel-time proxy


# ---- API ---------------------------------------------------------------


@app.get("/api/meta")
def meta():
    return {
        "start": _available_hours[0].isoformat(),
        "end": _available_hours[-1].isoformat(),
        "n_hours": len(_available_hours),
    }


@app.get("/api/zones")
def zones():
    return _zone_geojson


@app.get("/api/zone_centroids")
def zone_centroids():
    return _zones[["zone_id", "lat", "lon"]].to_dict(orient="records")


@app.get("/api/heatmap")
def heatmap(datetime: str = Query(..., description="ISO hour, e.g. 2024-03-20T18:00:00")):
    try:
        ts = pd.Timestamp(datetime)
    except ValueError:
        raise HTTPException(400, "invalid datetime")

    rows = _predictions[_predictions["pickup_hour"] == ts]
    if rows.empty:
        raise HTTPException(404, "no predictions for that hour; check /api/meta for range")

    out = rows[["zone_id", "trip_count", "model_pred", "baseline_pred"]].rename(
        columns={"trip_count": "actual", "model_pred": "predicted", "baseline_pred": "baseline"}
    )
    return {"datetime": ts.isoformat(), "zones": out.to_dict(orient="records")}


@app.get("/api/metrics")
def metrics():
    per_zone = _per_zone_metrics.merge(_zones, on="zone_id", how="left")
    return {"overall": _metrics, "per_zone": per_zone.to_dict(orient="records")}


@app.get("/api/recommend")
def recommend(
    zone_id: int = Query(..., description="driver's current zone"),
    datetime: str = Query(..., description="ISO hour"),
    top_n: int = 5,
):
    try:
        ts = pd.Timestamp(datetime)
    except ValueError:
        raise HTTPException(400, "invalid datetime")
    if zone_id not in _zones_by_id.index:
        raise HTTPException(404, "unknown zone_id")

    rows = _predictions[_predictions["pickup_hour"] == ts]
    if rows.empty:
        raise HTTPException(404, "no predictions for that hour; check /api/meta for range")

    origin = _zones_by_id.loc[zone_id]
    candidates = rows.merge(_zones, on="zone_id", how="left")
    candidates["travel_km"] = candidates.apply(
        lambda r: _haversine_km(origin["lat"], origin["lon"], r["lat"], r["lon"]), axis=1
    )
    candidates["travel_min"] = candidates["travel_km"] / AVG_SPEED_KMH * 60
    # Discount predicted demand by how long it takes to get there.
    candidates["score"] = candidates["model_pred"] / (1 + candidates["travel_min"] / 10)
    candidates = candidates[candidates["zone_id"] != zone_id]
    top = candidates.sort_values("score", ascending=False).head(top_n)

    return {
        "datetime": ts.isoformat(),
        "origin_zone_id": zone_id,
        "recommendations": top[
            ["zone_id", "zone_name", "borough", "model_pred", "travel_min", "score"]
        ].rename(columns={"model_pred": "predicted_demand"}).to_dict(orient="records"),
    }


# ---- Static frontend -----------------------------------------------------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
