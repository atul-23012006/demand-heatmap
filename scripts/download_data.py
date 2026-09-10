"""Download raw NYC TLC trip data and taxi zone reference files."""
import io
import zipfile
from pathlib import Path

import requests
import shapefile  # pyshp
from pyproj import Transformer

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

TRIP_MONTHS = ["2024-01", "2024-02", "2024-03"]
BASE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{month}.parquet"
ZONE_LOOKUP_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"
ZONE_SHAPES_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zones.zip"

# Free, keyless historical weather API. Manhattan centroid used as a single
# city-wide station — demand modeling doesn't need per-zone weather granularity.
WEATHER_URL = (
    "https://archive-api.open-meteo.com/v1/archive"
    "?latitude=40.7128&longitude=-74.0060"
    "&start_date=2024-01-01&end_date=2024-03-31"
    "&hourly=temperature_2m,precipitation,snowfall,wind_speed_10m"
    "&timezone=America%2FNew_York"
)


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"skip (exists): {dest.name}")
        return
    print(f"downloading {url} -> {dest.name}")
    resp = requests.get(url, timeout=120, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)


_TRANSFORMER = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)


def _reproject_coords(coords):
    if isinstance(coords[0], (int, float)):
        lon, lat = _TRANSFORMER.transform(coords[0], coords[1])
        return [lon, lat]
    return [_reproject_coords(c) for c in coords]


def shapefile_zip_to_geojson(zip_bytes: bytes, out_path: Path) -> None:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = zf.namelist()
    shp_name = next(n for n in names if n.endswith(".shp"))
    base = shp_name[: -len(".shp")]
    shp = io.BytesIO(zf.read(base + ".shp"))
    shx = io.BytesIO(zf.read(base + ".shx"))
    dbf = io.BytesIO(zf.read(base + ".dbf"))
    reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
    features = []
    for sr in reader.iterShapeRecords():
        geom = sr.shape.__geo_interface__
        geom = {
            "type": geom["type"],
            "coordinates": _reproject_coords(geom["coordinates"]),
        }
        props = sr.record.as_dict()
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    geojson = {"type": "FeatureCollection", "features": features}
    import json

    out_path.write_text(json.dumps(geojson))
    print(f"wrote {out_path.name} with {len(features)} zones")


def download_weather(dest: Path) -> None:
    if dest.exists():
        print(f"skip (exists): {dest.name}")
        return
    print(f"downloading weather -> {dest.name}")
    resp = requests.get(WEATHER_URL, timeout=60)
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    import csv

    with open(dest, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["pickup_hour", "temp_c", "precip_mm", "snow_cm", "wind_kmh"])
        for row in zip(
            hourly["time"],
            hourly["temperature_2m"],
            hourly["precipitation"],
            hourly["snowfall"],
            hourly["wind_speed_10m"],
        ):
            writer.writerow(row)


def main() -> None:
    for month in TRIP_MONTHS:
        url = BASE_URL.format(month=month)
        dest = RAW_DIR / f"yellow_tripdata_{month}.parquet"
        download(url, dest)

    download(ZONE_LOOKUP_URL, RAW_DIR / "taxi_zone_lookup.csv")
    download_weather(RAW_DIR / "weather.csv")

    geojson_path = RAW_DIR / "taxi_zones.geojson"
    if geojson_path.exists():
        print(f"skip (exists): {geojson_path.name}")
    else:
        resp = requests.get(ZONE_SHAPES_URL, timeout=120)
        resp.raise_for_status()
        shapefile_zip_to_geojson(resp.content, geojson_path)


if __name__ == "__main__":
    main()
