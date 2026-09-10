"""FastAPI backend for the ride-hailing demand heatmap predictor."""
import json
import math
from pathlib import Path
from typing import List

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

_zone_fares = pd.read_parquet(DATA_DIR / "processed" / "zone_fares.parquet").set_index("zone_id")

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

# Heuristic: predicted zone demand this saturated absorbs one productive driver.
# Used both by the earnings simulator (pickup probability) and the fleet
# load-balancer (per-zone capacity). Not calibrated against real fleet sizes —
# it's an illustrative assumption, documented here and in the UI.
TRIPS_PER_DRIVER_SATURATION = 8.0


def _travel_minutes(origin_zone_id: int, dest_zone_id: int) -> float:
    if origin_zone_id == dest_zone_id:
        return 0.0
    o, d = _zones_by_id.loc[origin_zone_id], _zones_by_id.loc[dest_zone_id]
    km = _haversine_km(o["lat"], o["lon"], d["lat"], d["lon"])
    return km / AVG_SPEED_KMH * 60


def _score_zones_for_driver(origin_zone_id: int, rows: pd.DataFrame) -> pd.DataFrame:
    """rows: predictions for a single hour, all zones. Returns rows with travel_min/score,
    scored from the given driver's origin zone, excluding the origin zone itself."""
    candidates = rows.merge(_zones, on="zone_id", how="left")
    origin = _zones_by_id.loc[origin_zone_id]
    candidates["travel_km"] = candidates.apply(
        lambda r: _haversine_km(origin["lat"], origin["lon"], r["lat"], r["lon"]), axis=1
    )
    candidates["travel_min"] = candidates["travel_km"] / AVG_SPEED_KMH * 60
    candidates["score"] = candidates["model_pred"] / (1 + candidates["travel_min"] / 10)
    return candidates[candidates["zone_id"] != origin_zone_id]


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

    candidates = _score_zones_for_driver(zone_id, rows)
    top = candidates.sort_values("score", ascending=False).head(top_n)

    return {
        "datetime": ts.isoformat(),
        "origin_zone_id": zone_id,
        "recommendations": top[
            ["zone_id", "zone_name", "borough", "model_pred", "travel_min", "score"]
        ].rename(columns={"model_pred": "predicted_demand"}).to_dict(orient="records"),
    }


@app.get("/api/simulate")
def simulate(
    start_zone: int = Query(..., description="driver's starting zone"),
    date: str = Query(..., description="YYYY-MM-DD, within the served test window"),
):
    """Replay one day (24 hourly predictions) and compare two driver strategies:
    'stay put' in start_zone all day vs. 'follow the model' repositioning each hour
    toward the best-scoring reachable zone. Earnings are illustrative, not real fare
    modeling: pickup probability per hour = min(1, predicted_demand / TRIPS_PER_DRIVER_SATURATION),
    scaled down by the fraction of the hour lost to travel when repositioning."""
    if start_zone not in _zones_by_id.index:
        raise HTTPException(404, "unknown zone_id")
    try:
        day_start = pd.Timestamp(date)
    except ValueError:
        raise HTTPException(400, "invalid date")

    day_hours = [day_start + pd.Timedelta(hours=h) for h in range(24)]
    day_preds = _predictions[_predictions["pickup_hour"].isin(day_hours)]
    if day_preds.empty:
        raise HTTPException(404, "no predictions for that date; check /api/meta for range")

    def capture_prob(demand: float, time_fraction: float = 1.0) -> float:
        return float(min(1.0, max(0.0, demand) * time_fraction / TRIPS_PER_DRIVER_SATURATION))

    # Strategy 1: stay put all day.
    stay_zone_preds = day_preds[day_preds["zone_id"] == start_zone].set_index("pickup_hour")
    fare_stay = float(_zone_fares.loc[start_zone, "avg_fare"])
    stay_put_hours = []
    for hour in day_hours:
        demand = float(stay_zone_preds.loc[hour, "model_pred"]) if hour in stay_zone_preds.index else 0.0
        prob = capture_prob(demand)
        stay_put_hours.append(
            {"hour": hour.isoformat(), "zone_id": start_zone, "predicted_demand": demand,
             "earnings": round(prob * fare_stay, 2)}
        )

    # Strategy 2: follow the model, reconsidering position each hour.
    follow_hours = []
    current_zone = start_zone
    for hour in day_hours:
        rows = day_preds[day_preds["pickup_hour"] == hour]
        current_demand_row = rows[rows["zone_id"] == current_zone]
        current_demand = float(current_demand_row["model_pred"].iloc[0]) if not current_demand_row.empty else 0.0
        current_score = current_demand  # travel_min = 0 for staying

        candidates = _score_zones_for_driver(current_zone, rows)
        best = candidates.sort_values("score", ascending=False).head(1)

        moved = False
        if not best.empty and float(best["score"].iloc[0]) > current_score:
            dest_zone = int(best["zone_id"].iloc[0])
            travel_min = float(best["travel_min"].iloc[0])
            time_fraction = max(0.0, 1 - travel_min / 60)
            demand = float(best["model_pred"].iloc[0])
            moved = True
        else:
            dest_zone = current_zone
            travel_min = 0.0
            time_fraction = 1.0
            demand = current_demand

        fare = float(_zone_fares.loc[dest_zone, "avg_fare"])
        prob = capture_prob(demand, time_fraction)
        follow_hours.append(
            {"hour": hour.isoformat(), "zone_id": dest_zone, "moved": moved,
             "travel_min": round(travel_min, 1), "predicted_demand": demand,
             "earnings": round(prob * fare, 2)}
        )
        current_zone = dest_zone

    total_stay = round(sum(h["earnings"] for h in stay_put_hours), 2)
    total_follow = round(sum(h["earnings"] for h in follow_hours), 2)

    return {
        "date": date,
        "start_zone": start_zone,
        "start_zone_name": _zones_by_id.loc[start_zone, "zone_name"],
        "assumptions": {
            "trips_per_driver_saturation": TRIPS_PER_DRIVER_SATURATION,
            "avg_speed_kmh": AVG_SPEED_KMH,
            "note": "Illustrative simulation: pickup probability per hour is a simple "
                    "saturation heuristic, not a calibrated fare/dispatch model.",
        },
        "stay_put": {"total_earnings": total_stay, "hours": stay_put_hours},
        "follow_model": {"total_earnings": total_follow, "hours": follow_hours},
        "earnings_lift_pct": round((total_follow - total_stay) / total_stay * 100, 1) if total_stay > 0 else None,
    }


class DriverPosition(BaseModel):
    driver_id: str
    zone_id: int


class RecommendBatchRequest(BaseModel):
    datetime: str
    drivers: List[DriverPosition]
    candidate_pool_size: int = 40


@app.post("/api/recommend_batch")
def recommend_batch(req: RecommendBatchRequest):
    """Load-balanced repositioning for multiple simultaneous drivers: a naive
    'everyone goes to the single best zone' recommender would send a whole fleet
    to one hotspot. This greedily assigns each driver to the best zone it can
    still productively use, so demand gets spread across the top candidates."""
    try:
        ts = pd.Timestamp(req.datetime)
    except ValueError:
        raise HTTPException(400, "invalid datetime")
    for d in req.drivers:
        if d.zone_id not in _zones_by_id.index:
            raise HTTPException(404, f"unknown zone_id: {d.zone_id}")
    if not req.drivers:
        raise HTTPException(400, "drivers list is empty")

    rows = _predictions[_predictions["pickup_hour"] == ts]
    if rows.empty:
        raise HTTPException(404, "no predictions for that hour; check /api/meta for range")

    candidate_zones = rows.merge(_zones, on="zone_id", how="left").sort_values(
        "model_pred", ascending=False
    ).head(req.candidate_pool_size).copy()
    capacity = {
        int(z): max(1, round(d / TRIPS_PER_DRIVER_SATURATION))
        for z, d in zip(candidate_zones["zone_id"], candidate_zones["model_pred"])
    }

    # Build every (driver, zone) pair scored from that driver's own position.
    pairs = []
    for d in req.drivers:
        for _, z in candidate_zones.iterrows():
            travel_min = _travel_minutes(d.zone_id, int(z["zone_id"]))
            score = float(z["model_pred"]) / (1 + travel_min / 10)
            pairs.append((score, d.driver_id, int(z["zone_id"]), travel_min, float(z["model_pred"])))
    pairs.sort(key=lambda p: p[0], reverse=True)

    assigned = {}
    remaining_capacity = dict(capacity)
    for score, driver_id, zone_id, travel_min, demand in pairs:
        if driver_id in assigned:
            continue
        if remaining_capacity.get(zone_id, 0) <= 0:
            continue
        assigned[driver_id] = {
            "driver_id": driver_id,
            "zone_id": zone_id,
            "zone_name": _zones_by_id.loc[zone_id, "zone_name"],
            "predicted_demand": demand,
            "travel_min": round(travel_min, 1),
            "score": round(score, 2),
        }
        remaining_capacity[zone_id] -= 1

    # Fallback: any driver capacity couldn't cover gets their single best zone anyway
    # (still useful info, just flagged as over-capacity).
    for d in req.drivers:
        if d.driver_id in assigned:
            continue
        best_score, best_zone, best_travel, best_demand = None, None, None, None
        for _, z in candidate_zones.iterrows():
            travel_min = _travel_minutes(d.zone_id, int(z["zone_id"]))
            score = float(z["model_pred"]) / (1 + travel_min / 10)
            if best_score is None or score > best_score:
                best_score, best_zone, best_travel, best_demand = score, int(z["zone_id"]), travel_min, float(z["model_pred"])
        assigned[d.driver_id] = {
            "driver_id": d.driver_id,
            "zone_id": best_zone,
            "zone_name": _zones_by_id.loc[best_zone, "zone_name"],
            "predicted_demand": best_demand,
            "travel_min": round(best_travel, 1),
            "score": round(best_score, 2),
            "over_capacity": True,
        }

    return {
        "datetime": ts.isoformat(),
        "assignments": [assigned[d.driver_id] for d in req.drivers],
    }


# ---- Static frontend -----------------------------------------------------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
