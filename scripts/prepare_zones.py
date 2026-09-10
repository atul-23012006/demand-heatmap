"""Compute zone centroids (for travel-time proxy) and a clean zone reference table."""
import json
from pathlib import Path

import pandas as pd
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    geojson = json.loads((RAW_DIR / "taxi_zones.geojson").read_text())
    lookup = pd.read_csv(RAW_DIR / "taxi_zone_lookup.csv")

    rows = []
    for feat in geojson["features"]:
        zone_id = feat["properties"]["LocationID"]
        geom = shape(feat["geometry"])
        centroid = geom.representative_point()
        rows.append({"zone_id": zone_id, "lat": centroid.y, "lon": centroid.x})

    centroids = pd.DataFrame(rows)
    zones = centroids.merge(
        lookup.rename(columns={"LocationID": "zone_id", "Borough": "borough", "Zone": "zone_name"}),
        on="zone_id",
        how="left",
    )[["zone_id", "zone_name", "borough", "lat", "lon"]]

    out_path = PROCESSED_DIR / "zones.parquet"
    zones.to_parquet(out_path, index=False)
    print(f"wrote {out_path} ({len(zones)} zones)")
    print(zones.head())


if __name__ == "__main__":
    main()
