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


if __name__ == "__main__":
    main()
