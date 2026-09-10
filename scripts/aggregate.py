"""Aggregate raw NYC TLC trip data into hourly pickup demand per zone."""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# Valid pickup window for the months we downloaded (filters stray out-of-range timestamps
# that appear in TLC data due to bad meter clocks).
MONTH_RANGES = [
    ("2024-01-01", "2024-02-01"),
    ("2024-02-01", "2024-03-01"),
    ("2024-03-01", "2024-04-01"),
]


def main() -> None:
    con = duckdb.connect()
    parquet_glob = str(RAW_DIR / "yellow_tripdata_*.parquet")

    date_filter = " OR ".join(
        f"(tpep_pickup_datetime >= '{start}' AND tpep_pickup_datetime < '{end}')"
        for start, end in MONTH_RANGES
    )

    counts_query = f"""
        SELECT
            PULocationID AS zone_id,
            date_trunc('hour', tpep_pickup_datetime) AS pickup_hour,
            count(*) AS trip_count
        FROM '{parquet_glob}'
        WHERE PULocationID IS NOT NULL
          AND ({date_filter})
        GROUP BY 1, 2
    """

    # Dense zone x hour panel, zero-filled, so the model also learns "no demand" hours
    # and lag/rolling features don't have silent gaps.
    query = f"""
        WITH counts AS ({counts_query}),
        zones AS (
            SELECT LocationID AS zone_id
            FROM read_csv_auto('{RAW_DIR / "taxi_zone_lookup.csv"}')
            WHERE LocationID <= 263
        ),
        hours AS (
            SELECT unnest(generate_series(
                TIMESTAMP '2024-01-01 00:00:00',
                TIMESTAMP '2024-03-31 23:00:00',
                INTERVAL 1 HOUR
            )) AS pickup_hour
        )
        SELECT
            zones.zone_id,
            hours.pickup_hour,
            coalesce(counts.trip_count, 0) AS trip_count
        FROM zones
        CROSS JOIN hours
        LEFT JOIN counts
          ON counts.zone_id = zones.zone_id AND counts.pickup_hour = hours.pickup_hour
        ORDER BY zones.zone_id, hours.pickup_hour
    """
    df = con.execute(query).fetchdf()
    print(f"aggregated rows: {len(df)}")
    print(df.head())

    out_path = PROCESSED_DIR / "hourly_demand.parquet"
    df.to_parquet(out_path, index=False)
    print(f"wrote {out_path}")

    # Average fare per zone (training period only, matching the model's train split),
    # used by the earnings simulator. fare_amount is metered fare excluding tips/tolls;
    # clipped to drop obvious data-entry errors (negative/zero or absurdly large fares).
    fares_query = f"""
        SELECT
            PULocationID AS zone_id,
            avg(fare_amount) AS avg_fare
        FROM '{parquet_glob}'
        WHERE PULocationID IS NOT NULL
          AND tpep_pickup_datetime >= '2024-01-01' AND tpep_pickup_datetime < '2024-03-18'
          AND fare_amount BETWEEN 2.5 AND 200
        GROUP BY 1
    """
    fares_df = con.execute(fares_query).fetchdf()
    overall_avg_fare = float(fares_df["avg_fare"].mean())
    # zones with no training-period trips (rare) fall back to the citywide average
    fares_df = fares_df.set_index("zone_id").reindex(range(1, 264)).reset_index()
    fares_df["avg_fare"] = fares_df["avg_fare"].fillna(overall_avg_fare)
    fares_out = PROCESSED_DIR / "zone_fares.parquet"
    fares_df.to_parquet(fares_out, index=False)
    print(f"wrote {fares_out}")


if __name__ == "__main__":
    main()
