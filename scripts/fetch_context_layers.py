#!/usr/bin/env python3
"""Fetch context layers for the transmission flight-path prototype.

This intentionally stays small and dependency-free. It writes processed
GeoJSON files that the visualizer can load directly:

    data/processed/<area>/lines.geojson
    data/processed/<area>/structures.geojson
    data/processed/<area>/airport.geojson
    data/processed/<area>/forest.geojson
    data/processed/<area>/water.geojson
    data/processed/<area>/roads.geojson

The extracts come from OpenStreetMap via Overpass. They are useful planning
context, not authoritative operational data.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UNVERIFIED_SSL_CONTEXT = ssl._create_unverified_context()

AREAS = {
    "nipawin": {
        "label": "Nipawin / CYBU",
        "bbox": (52.9, -104.85, 53.75, -103.15),  # south, west, north, east
    },
    "montreal": {
        "label": "Montreal / YUL",
        "bbox": (45.35, -74.10, 45.78, -73.20),
    },
    "laronge": {
        "label": "Lac La Ronge / CYVC",
        "bbox": (54.85, -106.10, 55.45, -104.65),
    },
}

LAYER_QUERIES = {
    "lines": [
        'way["power"~"^(line|minor_line|cable)$"]({bbox});',
    ],
    "structures": [
        'node["power"~"^(tower|pole|substation)$"]({bbox});',
        'way["power"="substation"]({bbox});',
    ],
    "airport": [
        'node["aeroway"="aerodrome"]({bbox});',
        'way["aeroway"="aerodrome"]({bbox});',
    ],
    "forest": [
        'way["landuse"="forest"]({bbox});',
        'way["natural"="wood"]({bbox});',
        'way["natural"="tree_row"]({bbox});',
        'way["wood"]({bbox});',
    ],
    "water": [
        'way["natural"="water"]({bbox});',
        'way["water"]({bbox});',
        'way["landuse"="reservoir"]({bbox});',
        'way["waterway"~"^(river|stream|canal|ditch|drain)$"]({bbox});',
    ],
    "roads": [
        'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|track|road)$"]({bbox});',
    ],
}

AREA_LAYER_QUERIES = {
    ("montreal", "roads"): [
        'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified)$"]({bbox});',
    ],
}


def feature_collection(features: list[dict], metadata: dict) -> dict:
    return {"type": "FeatureCollection", "metadata": metadata, "features": features}


def overpass_query(area: str, layer: str) -> str:
    south, west, north, east = AREAS[area]["bbox"]
    bbox = f"{south},{west},{north},{east}"
    query_parts = AREA_LAYER_QUERIES.get((area, layer), LAYER_QUERIES[layer])
    clauses = "\n  ".join(clause.format(bbox=bbox) for clause in query_parts)
    return f"""[out:json][timeout:180];
(
  {clauses}
);
out body geom;
"""


def fetch_overpass(query: str, retries: int = 2) -> dict:
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        for endpoint in OVERPASS_ENDPOINTS:
            request = urllib.request.Request(
                endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "utilities-samples-flight-path-context/0.1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=240, context=UNVERIFIED_SSL_CONTEXT) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.HTTPException) as exc:
                last_error = exc
                time.sleep(2 + attempt)
    raise RuntimeError(f"Overpass fetch failed after retries: {last_error}")


def way_geometry(element: dict, layer: str) -> dict | None:
    coords = [[point["lon"], point["lat"]] for point in element.get("geometry", [])]
    if len(coords) < 2:
        return None
    is_closed = coords[0] == coords[-1] and len(coords) >= 4
    if layer in {"forest", "water"} and is_closed:
        return {"type": "Polygon", "coordinates": [coords]}
    return {"type": "LineString", "coordinates": coords}


def element_kind(tags: dict, layer: str) -> str:
    for key in ("power", "aeroway", "natural", "landuse", "water", "waterway", "highway", "wood"):
        if tags.get(key):
            return tags[key]
    return layer


def centroid(coords: list[list[float]]) -> list[float]:
    return [
        sum(coord[0] for coord in coords) / len(coords),
        sum(coord[1] for coord in coords) / len(coords),
    ]


def point_feature(element: dict, tags: dict, area: str, layer: str) -> dict | None:
    if element.get("type") == "node":
        coords = [element["lon"], element["lat"]]
    elif element.get("geometry"):
        coords = centroid([[point["lon"], point["lat"]] for point in element["geometry"]])
    else:
        return None
    return {
        "type": "Feature",
        "properties": {
            "id": element.get("id"),
            "kind": element_kind(tags, layer),
            "name": tags.get("name", ""),
            "icao": tags.get("icao", ""),
            "iata": tags.get("iata", ""),
            "source": "OpenStreetMap / Overpass",
            "layer": layer,
        },
        "geometry": {"type": "Point", "coordinates": coords},
    }


def overpass_to_geojson(data: dict, area: str, layer: str) -> dict:
    features: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for element in data.get("elements", []):
        key = (element["type"], element["id"])
        if key in seen:
            continue
        seen.add(key)
        tags = element.get("tags") or {}
        if layer in {"airport", "structures"}:
            feature = point_feature(element, tags, area, layer)
            if feature:
                features.append(feature)
            continue
        if element.get("type") != "way":
            continue
        geometry = way_geometry(element, layer)
        if not geometry:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": element.get("id"),
                    "kind": element_kind(tags, layer),
                    "name": tags.get("name", ""),
                    "source": "OpenStreetMap / Overpass",
                    "layer": layer,
                    "voltage": tags.get("voltage", ""),
                    "surface": tags.get("surface", ""),
                    "access": tags.get("access", ""),
                },
                "geometry": geometry,
            }
        )
    metadata = {
        "area": area,
        "area_label": AREAS[area]["label"],
        "layer": layer,
        "source": "OpenStreetMap / Overpass",
        "feature_count": len(features),
        "bbox_south_west_north_east": AREAS[area]["bbox"],
        "generated_at_unix": int(time.time()),
    }
    return feature_collection(features, metadata)


def write_geojson(area: str, layer: str, geojson: dict) -> Path:
    out_dir = PROCESSED_DIR / area
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{layer}.geojson"
    out_path.write_text(json.dumps(geojson, separators=(",", ":")), encoding="utf-8")
    return out_path


def fetch_layer(area: str, layer: str) -> Path:
    query = overpass_query(area, layer)
    data = fetch_overpass(query)
    geojson = overpass_to_geojson(data, area, layer)
    return write_geojson(area, layer, geojson)


def iter_layers(layers: Iterable[str]) -> list[str]:
    selected = list(layers)
    return selected or list(LAYER_QUERIES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--area", choices=AREAS, action="append", help="Area to fetch. Repeat for multiple areas.")
    parser.add_argument("--layer", choices=LAYER_QUERIES, action="append", help="Layer to fetch. Repeat for multiple layers.")
    args = parser.parse_args()

    areas = args.area or list(AREAS)
    layers = iter_layers(args.layer or [])
    for area in areas:
        for layer in layers:
            path = fetch_layer(area, layer)
            count = json.loads(path.read_text(encoding="utf-8")).get("metadata", {}).get("feature_count", 0)
            print(f"Wrote {path} ({count} {layer} features)")


if __name__ == "__main__":
    main()
