def test_meta_reports_valid_range(client):
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    data = resp.json()
    assert data["start"] < data["end"]
    assert data["n_hours"] > 0


def test_zones_geojson_has_263_zones(client):
    resp = client.get("/api/zones")
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 263
    props = data["features"][0]["properties"]
    assert {"zone_id", "zone_name", "borough"} <= props.keys()


def test_heatmap_within_range(client):
    meta = client.get("/api/meta").json()
    resp = client.get("/api/heatmap", params={"datetime": meta["start"]})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["zones"]) > 0
    zone = data["zones"][0]
    assert {"zone_id", "actual", "predicted", "baseline"} <= zone.keys()


def test_heatmap_outside_range_is_404(client):
    resp = client.get("/api/heatmap", params={"datetime": "2019-01-01T00:00:00"})
    assert resp.status_code == 404


def test_heatmap_invalid_datetime_is_400(client):
    resp = client.get("/api/heatmap", params={"datetime": "not-a-date"})
    assert resp.status_code == 400


def test_recommend_excludes_origin_and_is_sorted(client):
    meta = client.get("/api/meta").json()
    resp = client.get("/api/recommend", params={"zone_id": 161, "datetime": meta["start"], "top_n": 5})
    assert resp.status_code == 200
    data = resp.json()
    recs = data["recommendations"]
    assert len(recs) == 5
    assert all(r["zone_id"] != 161 for r in recs)
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_recommend_unknown_zone_is_404(client):
    meta = client.get("/api/meta").json()
    resp = client.get("/api/recommend", params={"zone_id": 99999, "datetime": meta["start"]})
    assert resp.status_code == 404


def test_simulate_totals_are_consistent(client):
    meta = client.get("/api/meta").json()
    date = meta["start"][:10]
    resp = client.get("/api/simulate", params={"start_zone": 161, "date": date})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["stay_put"]["hours"]) == 24
    assert len(data["follow_model"]["hours"]) == 24
    assert data["stay_put"]["total_earnings"] == round(
        sum(h["earnings"] for h in data["stay_put"]["hours"]), 2
    )
    # every hour's earnings must be non-negative
    assert all(h["earnings"] >= 0 for h in data["follow_model"]["hours"])


def test_recommend_batch_assigns_every_driver(client):
    meta = client.get("/api/meta").json()
    payload = {
        "datetime": meta["start"],
        "drivers": [
            {"driver_id": "a", "zone_id": 161},
            {"driver_id": "b", "zone_id": 162},
            {"driver_id": "c", "zone_id": 4},
        ],
    }
    resp = client.post("/api/recommend_batch", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert {a["driver_id"] for a in data["assignments"]} == {"a", "b", "c"}


def test_recommend_batch_spreads_load_when_capacity_exhausted(client):
    """A single low-demand zone can't productively absorb 50 drivers -- the
    load balancer must spread them across more than one destination zone."""
    meta = client.get("/api/meta").json()
    # pick an off-peak hour to keep even the top zone's demand (and thus capacity) low
    off_peak = meta["start"][:11] + "04:00:00"
    drivers = [{"driver_id": f"d{i}", "zone_id": 4} for i in range(50)]
    resp = client.post("/api/recommend_batch", json={"datetime": off_peak, "drivers": drivers})
    assert resp.status_code == 200
    data = resp.json()
    assigned_zones = {a["zone_id"] for a in data["assignments"]}
    assert len(assigned_zones) > 1


def test_recommend_batch_unknown_zone_is_404(client):
    resp = client.post(
        "/api/recommend_batch",
        json={"datetime": "2024-03-20T18:00:00", "drivers": [{"driver_id": "a", "zone_id": 99999}]},
    )
    assert resp.status_code == 404


def test_metrics_reports_model_beats_or_matches_baseline(client):
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall"]["model_mae"] <= data["overall"]["baseline_mae"]
    assert len(data["per_zone"]) == 263
