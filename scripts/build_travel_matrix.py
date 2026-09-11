"""Build a real driving-time matrix between all taxi zone centroids, using
OSRM's free public demo routing server (https://router.project-osrm.org).
Replaces the haversine-distance travel-time proxy with actual road network
travel times. Precomputed once and cached -- the API never calls OSRM live.

The public demo server caps table requests at ~100-119 coordinates, so the
263x263 matrix is built by tiling: split zones into groups of ~45, request
each group pair's combined coordinates, and slice out the relevant block.
"""
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = ROOT / "data" / "processed"

OSRM_URL = "https://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
GROUP_SIZE = 45
REQUEST_DELAY_SEC = 0.5  # be polite to the free public demo server


def fetch_durations(coords: list[tuple[float, float]]) -> list[list[float | None]]:
    """coords: list of (lon, lat). Returns duration matrix in seconds (may contain None)."""
    coord_str = ";".join(f"{lon:.5f},{lat:.5f}" for lon, lat in coords)
    resp = requests.get(OSRM_URL.format(coords=coord_str), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data}")
    return data["durations"]


def main() -> None:
    zones = pd.read_parquet(PROCESSED_DIR / "zones.parquet").sort_values("zone_id").reset_index(drop=True)
    zone_ids = zones["zone_id"].tolist()
    coords_by_zone = {row.zone_id: (row.lon, row.lat) for row in zones.itertuples()}

    groups = [zone_ids[i : i + GROUP_SIZE] for i in range(0, len(zone_ids), GROUP_SIZE)]
    print(f"{len(zone_ids)} zones, {len(groups)} groups of ~{GROUP_SIZE}")

    records = []  # (zone_from, zone_to, travel_min)

    def store_block(rows_ids: list[int], cols_ids: list[int], durations: list[list]) -> None:
        for i, zfrom in enumerate(rows_ids):
            for j, zto in enumerate(cols_ids):
                d = durations[i][j]
                if d is not None:
                    records.append((zfrom, zto, d / 60))

    n_requests = 0
    for gi in range(len(groups)):
        # diagonal block: this group against itself
        group = groups[gi]
        coords = [coords_by_zone[z] for z in group]
        durations = fetch_durations(coords)
        store_block(group, group, durations)
        n_requests += 1
        time.sleep(REQUEST_DELAY_SEC)

        for gj in range(gi + 1, len(groups)):
            group_j = groups[gj]
            combined_ids = group + group_j
            combined_coords = [coords_by_zone[z] for z in combined_ids]
            durations = fetch_durations(combined_coords)
            n = len(group)
            # off-diagonal quadrants only (diagonal quadrants already covered elsewhere)
            top_right = [row[n:] for row in durations[:n]]
            bottom_left = [row[:n] for row in durations[n:]]
            store_block(group, group_j, top_right)
            store_block(group_j, group, bottom_left)
            n_requests += 1
            time.sleep(REQUEST_DELAY_SEC)
            print(f"  group {gi}x{gj} done ({n_requests} requests so far)")

    df = pd.DataFrame(records, columns=["zone_from", "zone_to", "travel_min"])
    print(f"matrix rows: {len(df)} (of {len(zone_ids) ** 2} possible pairs), {n_requests} OSRM requests")

    out_path = PROCESSED_DIR / "travel_matrix.parquet"
    df.to_parquet(out_path, index=False)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
