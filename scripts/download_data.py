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


def main() -> None:
    for month in TRIP_MONTHS:
        url = BASE_URL.format(month=month)
        dest = RAW_DIR / f"yellow_tripdata_{month}.parquet"
        download(url, dest)

    download(ZONE_LOOKUP_URL, RAW_DIR / "taxi_zone_lookup.csv")

    geojson_path = RAW_DIR / "taxi_zones.geojson"
    if geojson_path.exists():
        print(f"skip (exists): {geojson_path.name}")
    else:
        resp = requests.get(ZONE_SHAPES_URL, timeout=120)
        resp.raise_for_status()
        shapefile_zip_to_geojson(resp.content, geojson_path)


if __name__ == "__main__":
    main()
