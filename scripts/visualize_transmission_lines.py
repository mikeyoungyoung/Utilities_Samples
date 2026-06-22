#!/usr/bin/env python3
"""One-off visualization for Montreal electric transmission line GeoPackage.

The script avoids heavyweight GIS dependencies. It downloads the GeoPackage,
reads GeoPackage/WKB geometry blobs via sqlite3, and renders a static PNG with
Pillow. It also writes a small HTML companion that embeds the image and metrics.
"""

from __future__ import annotations

import math
import json
import sqlite3
import struct
import textwrap
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = ROOT / "outputs"
GPKG_PATH = DATA_DIR / "lignes-transport-electrique-2020.gpkg"
TILE_CACHE_DIR = DATA_DIR / "osm_tiles"
PNG_PATH = OUTPUT_DIR / "montreal_transmission_lines.png"
HTML_PATH = OUTPUT_DIR / "montreal_transmission_lines.html"
FLIGHT_PNG_PATH = OUTPUT_DIR / "yul_to_transmission_midpoint_flight_path.png"
FLIGHT_HTML_PATH = OUTPUT_DIR / "yul_to_transmission_midpoint_flight_path.html"
INTERACTIVE_HTML_PATH = OUTPUT_DIR / "interactive_flight_path_picker.html"

DATA_URL = (
    "https://donnees.montreal.ca/fr/dataset/"
    "ac3515d6-2753-47a5-8575-35be7d127f43/resource/"
    "71a81a40-1d76-4318-b698-a4d5cd6fea30/download/"
    "lignes-transport-electrique-2020.gpkg"
)

LINE_TABLE = "carto_ser_ele_tel_aerien"
STRUCTURE_TABLE = "carto_ser_electricite"
GEOM_COL = "geometry"
TILE_ZOOM = 11
TILE_SIZE = 256
YUL_AIRPORT = {
    "name": "Montreal-Trudeau International Airport (YUL)",
    "lon": -73.7408,
    "lat": 45.4706,
}

TOKENS = {
    "surface": "#FCFCFD",
    "panel": "#FFFFFF",
    "ink": "#1F2430",
    "muted": "#6F768A",
    "grid": "#E6E8F0",
    "axis": "#D7DBE7",
    "blue_base": "#A3BEFA",
    "blue_mid": "#5477C4",
    "blue_dark": "#2E4780",
    "gold_base": "#FFE15B",
    "gold_dark": "#736422",
    "orange_base": "#F0986E",
    "orange_dark": "#804126",
    "pink_base": "#F390CA",
    "pink_dark": "#8A3A6F",
    "neutral_light": "#E2E5EA",
}


@dataclass(frozen=True)
class GeometrySet:
    table: str
    kind: str
    parts: list[list[tuple[float, float]]]
    feature_count: int


class WKBReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0
        self.endian = "<"

    def read_byte(self) -> int:
        value = self.data[self.offset]
        self.offset += 1
        return value

    def read_uint32(self) -> int:
        value = struct.unpack_from(f"{self.endian}I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def read_point(self) -> tuple[float, float]:
        x, y = struct.unpack_from(f"{self.endian}dd", self.data, self.offset)
        self.offset += 16
        return x, y

    def read_geometry(self) -> list[list[tuple[float, float]]]:
        byte_order = self.read_byte()
        self.endian = "<" if byte_order == 1 else ">"
        geom_type = self.read_uint32() % 1000

        if geom_type == 1:
            return [[self.read_point()]]
        if geom_type == 2:
            return [self._read_line_string()]
        if geom_type == 3:
            return self._read_polygon()
        if geom_type in (4, 5, 6):
            parts: list[list[tuple[float, float]]] = []
            for _ in range(self.read_uint32()):
                parts.extend(self.read_geometry())
            return parts

        raise ValueError(f"Unsupported WKB geometry type: {geom_type}")

    def _read_line_string(self) -> list[tuple[float, float]]:
        return [self.read_point() for _ in range(self.read_uint32())]

    def _read_polygon(self) -> list[list[tuple[float, float]]]:
        rings = []
        for _ in range(self.read_uint32()):
            rings.append([self.read_point() for _ in range(self.read_uint32())])
        return rings


def ensure_data() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    if GPKG_PATH.exists() and GPKG_PATH.stat().st_size > 1024:
        return

    request = urllib.request.Request(
        DATA_URL,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        GPKG_PATH.write_bytes(response.read())


def gpkg_wkb(blob: bytes) -> bytes:
    if blob[:2] != b"GP":
        raise ValueError("Expected GeoPackage binary geometry header.")
    flags = blob[3]
    envelope_type = (flags >> 1) & 0b111
    envelope_lengths = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    envelope_len = envelope_lengths.get(envelope_type)
    if envelope_len is None:
        raise ValueError(f"Unsupported GeoPackage envelope type: {envelope_type}")
    return blob[8 + envelope_len :]


def read_layer(conn: sqlite3.Connection, table: str, kind: str) -> GeometrySet:
    rows = conn.execute(f'SELECT "{GEOM_COL}" FROM "{table}"').fetchall()
    parts: list[list[tuple[float, float]]] = []
    for (blob,) in rows:
        parts.extend(WKBReader(gpkg_wkb(blob)).read_geometry())
    return GeometrySet(table=table, kind=kind, parts=parts, feature_count=len(rows))


def read_extent(conn: sqlite3.Connection) -> tuple[float, float, float, float]:
    rows = conn.execute(
        "SELECT min_x, min_y, max_x, max_y FROM gpkg_contents "
        "WHERE table_name IN (?, ?)",
        (LINE_TABLE, STRUCTURE_TABLE),
    ).fetchall()
    return (
        min(row[0] for row in rows),
        min(row[1] for row in rows),
        max(row[2] for row in rows),
        max(row[3] for row in rows),
    )


def length_m(parts: Iterable[Sequence[tuple[float, float]]]) -> float:
    total = 0.0
    for part in parts:
        total += sum(
            math.hypot(x2 - x1, y2 - y1)
            for (x1, y1), (x2, y2) in zip(part, part[1:])
        )
    return total


def midpoint_along_longest_part(parts: Iterable[Sequence[tuple[float, float]]]) -> tuple[float, float]:
    longest = max(parts, key=lambda part: length_m([part]) if len(part) >= 2 else 0)
    target = length_m([longest]) / 2
    walked = 0.0
    for start, end in zip(longest, longest[1:]):
        segment_len = math.hypot(end[0] - start[0], end[1] - start[1])
        if walked + segment_len >= target:
            ratio = (target - walked) / segment_len if segment_len else 0
            return (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
        walked += segment_len
    return longest[-1]


def haversine_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, origin)
    lon2, lat2 = map(math.radians, destination)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, origin)
    lon2, lat2 = map(math.radians, destination)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass_label(bearing: float) -> str:
    labels = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return labels[int((bearing + 11.25) // 22.5) % 16]


def polygon_centroids(parts: Iterable[Sequence[tuple[float, float]]]) -> list[tuple[float, float]]:
    centroids = []
    for ring in parts:
        if not ring:
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return centroids


def epsg2950_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Approximate inverse for NAD83(CSRS) / MTM zone 8, EPSG:2950."""
    a = 6378137.0
    inv_f = 298.257222101
    f = 1.0 / inv_f
    e2 = 2 * f - f * f
    ep2 = e2 / (1 - e2)
    k0 = 0.9999
    false_easting = 304800.0
    lon0 = math.radians(-73.5)

    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))

    j1 = 3 * e1 / 2 - 27 * e1**3 / 32
    j2 = 21 * e1**2 / 16 - 55 * e1**4 / 32
    j3 = 151 * e1**3 / 96
    j4 = 1097 * e1**4 / 512
    fp = mu + j1 * math.sin(2 * mu) + j2 * math.sin(4 * mu) + j3 * math.sin(6 * mu) + j4 * math.sin(8 * mu)

    sin_fp = math.sin(fp)
    cos_fp = math.cos(fp)
    tan_fp = math.tan(fp)
    c1 = ep2 * cos_fp**2
    t1 = tan_fp**2
    n1 = a / math.sqrt(1 - e2 * sin_fp**2)
    r1 = a * (1 - e2) / (1 - e2 * sin_fp**2) ** 1.5
    d = (x - false_easting) / (n1 * k0)

    lat = fp - (n1 * tan_fp / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon = lon0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / cos_fp
    return math.degrees(lon), math.degrees(lat)


def lonlat_to_tile_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    world = TILE_SIZE * 2**zoom
    x = (lon + 180.0) / 360.0 * world
    lat_rad = math.radians(lat)
    y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * world
    return x, y


def epsg2950_to_tile_pixel(point: tuple[float, float], zoom: int) -> tuple[float, float]:
    lon, lat = epsg2950_to_lonlat(point[0], point[1])
    return lonlat_to_tile_pixel(lon, lat, zoom)


def tile_pixel_extent(
    lines: GeometrySet,
    structures: GeometrySet,
    zoom: int,
    extra_pixels: Sequence[tuple[float, float]] = (),
) -> tuple[float, float, float, float]:
    pixels = []
    for part in lines.parts:
        pixels.extend(epsg2950_to_tile_pixel(point, zoom) for point in part)
    pixels.extend(epsg2950_to_tile_pixel(point, zoom) for point in polygon_centroids(structures.parts))
    pixels.extend(extra_pixels)

    min_x = min(point[0] for point in pixels)
    min_y = min(point[1] for point in pixels)
    max_x = max(point[0] for point in pixels)
    max_y = max(point[1] for point in pixels)
    pad_x = (max_x - min_x) * 0.04
    pad_y = (max_y - min_y) * 0.04
    return min_x - pad_x, min_y - pad_y, max_x + pad_x, max_y + pad_y


def fetch_osm_tile(zoom: int, x: int, y: int) -> Image.Image | None:
    max_tile = 2**zoom
    if x < 0 or y < 0 or x >= max_tile or y >= max_tile:
        return None

    tile_path = TILE_CACHE_DIR / str(zoom) / str(x) / f"{y}.png"
    if tile_path.exists() and tile_path.stat().st_size > 0:
        return Image.open(tile_path).convert("RGB")

    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "utilities-samples-one-off-viz/1.0 (local analysis)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
    except Exception:
        return None

    tile_path.parent.mkdir(parents=True, exist_ok=True)
    tile_path.write_bytes(data)
    return Image.open(BytesIO(data)).convert("RGB")


def build_street_background(
    size: tuple[int, int],
    world_extent: tuple[float, float, float, float],
    scale: float,
    zoom: int,
    offset: tuple[float, float] = (0, 0),
) -> Image.Image | None:
    min_tx, min_ty, max_tx, max_ty = world_extent
    tile_min_x = math.floor(min_tx / TILE_SIZE)
    tile_max_x = math.floor(max_tx / TILE_SIZE)
    tile_min_y = math.floor(min_ty / TILE_SIZE)
    tile_max_y = math.floor(max_ty / TILE_SIZE)

    layer = Image.new("RGB", size, TOKENS["panel"])
    fetched = 0
    for tx in range(tile_min_x, tile_max_x + 1):
        for ty in range(tile_min_y, tile_max_y + 1):
            tile = fetch_osm_tile(zoom, tx, ty)
            if tile is None:
                continue
            fetched += 1
            screen_x = int(round(offset[0] + (tx * TILE_SIZE - min_tx) * scale))
            screen_y = int(round(offset[1] + (ty * TILE_SIZE - min_ty) * scale))
            tile_px = max(1, int(math.ceil(TILE_SIZE * scale)) + 1)
            layer.paste(tile.resize((tile_px, tile_px), Image.Resampling.BILINEAR), (screen_x, screen_y))

    if fetched == 0:
        return None

    overlay = Image.new("RGBA", size, (255, 255, 255, 108))
    return Image.alpha_composite(layer.convert("RGBA"), overlay).convert("RGB")


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    width: int,
    line_spacing: int = 6,
) -> int:
    x, y = xy
    lines: list[str] = []
    for paragraph in text.splitlines():
        lines.extend(textwrap.wrap(paragraph, width=width) or [""])
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_spacing if hasattr(font, "size") else 18
    return y


def render_map(
    lines: GeometrySet,
    structures: GeometrySet,
    extent: tuple[float, float, float, float],
) -> dict[str, float | bool]:
    width, height = 1800, 1200
    left, top, right, bottom = 92, 210, 1320, 1090
    map_left, map_top, map_right, map_bottom = left + 35, top + 35, right - 35, bottom - 35
    map_size = (map_right - map_left, map_bottom - map_top)
    img = Image.new("RGB", (width, height), TOKENS["surface"])
    draw = ImageDraw.Draw(img)

    title_font = load_font(44, bold=True)
    subtitle_font = load_font(23)
    label_font = load_font(22, bold=True)
    small_font = load_font(18)
    mono_font = load_font(20)

    title = "Montreal electric transmission corridor footprint"
    subtitle = (
        "Aerial cable centerlines and support/base structures from Ville de Montreal "
        "open geospatial data; projected in EPSG:2950 and rendered as a planning-scale view."
    )
    draw.text((76, 42), title, font=title_font, fill=TOKENS["ink"])
    draw_wrapped(draw, (78, 104), subtitle, subtitle_font, TOKENS["muted"], width=104)

    draw.rounded_rectangle((left, top, right, bottom), radius=10, fill=TOKENS["panel"], outline=TOKENS["axis"], width=2)
    min_x, min_y, max_x, max_y = extent
    data_w = max_x - min_x
    data_h = max_y - min_y
    world_extent = tile_pixel_extent(lines, structures, TILE_ZOOM)
    min_tx, min_ty, max_tx, max_ty = world_extent
    world_w = max_tx - min_tx
    world_h = max_ty - min_ty
    scale = min(map_size[0] / world_w, map_size[1] / world_h)
    draw_w = world_w * scale
    draw_h = world_h * scale
    offset_x = map_left + (map_size[0] - draw_w) / 2
    offset_y = map_top + (map_size[1] - draw_h) / 2

    street_background = build_street_background(
        map_size,
        world_extent,
        scale,
        TILE_ZOOM,
        offset=(offset_x - map_left, offset_y - map_top),
    )
    if street_background is not None:
        img.paste(street_background, (map_left, map_top))
    else:
        for i in range(0, 6):
            gx = map_left + i * map_size[0] / 5
            gy = map_top + i * map_size[1] / 5
            draw.line((gx, map_top, gx, map_bottom), fill=TOKENS["grid"], width=1)
            draw.line((map_left, gy, map_right, gy), fill=TOKENS["grid"], width=1)

    def project(point: tuple[float, float]) -> tuple[float, float]:
        x, y = epsg2950_to_tile_pixel(point, TILE_ZOOM)
        px = offset_x + (x - min_tx) * scale
        py = offset_y + (y - min_ty) * scale
        return px, py

    for part in lines.parts:
        if len(part) < 2:
            continue
        pts = [project(p) for p in part]
        draw.line(pts, fill=TOKENS["blue_dark"], width=7, joint="curve")
        draw.line(pts, fill=TOKENS["blue_base"], width=3, joint="curve")

    centroids = polygon_centroids(structures.parts)
    for x, y in centroids:
        px, py = project((x, y))
        r = 3.5
        draw.ellipse((px - r, py - r, px + r, py + r), fill=TOKENS["orange_base"], outline=TOKENS["orange_dark"], width=1)

    # Legend.
    legend_x, legend_y = left + 30, bottom - 84
    draw.rounded_rectangle((legend_x - 16, legend_y - 18, legend_x + 500, legend_y + 54), radius=8, fill="#FFFFFF", outline=TOKENS["axis"], width=1)
    draw.line((legend_x, legend_y, legend_x + 66, legend_y), fill=TOKENS["blue_dark"], width=5)
    draw.line((legend_x, legend_y, legend_x + 66, legend_y), fill=TOKENS["blue_base"], width=2)
    draw.text((legend_x + 82, legend_y - 13), "Aerial cable centerlines", font=small_font, fill=TOKENS["ink"])
    draw.ellipse((legend_x + 316, legend_y - 7, legend_x + 330, legend_y + 7), fill=TOKENS["orange_base"], outline=TOKENS["orange_dark"], width=1)
    draw.text((legend_x + 344, legend_y - 13), "Support/base structures", font=small_font, fill=TOKENS["ink"])

    # Scale bar derived from Web Mercator tile resolution at the map center.
    scale_km = 10
    center_lon, center_lat = epsg2950_to_lonlat((min_x + max_x) / 2, (min_y + max_y) / 2)
    meters_per_tile_px = 156543.03392804097 * math.cos(math.radians(center_lat)) / (2**TILE_ZOOM)
    bar_px = scale_km * 1000 / meters_per_tile_px * scale
    bar_x, bar_y = right - 240, bottom - 72
    draw.line((bar_x, bar_y, bar_x + bar_px, bar_y), fill=TOKENS["ink"], width=4)
    draw.line((bar_x, bar_y - 8, bar_x, bar_y + 8), fill=TOKENS["ink"], width=3)
    draw.line((bar_x + bar_px, bar_y - 8, bar_x + bar_px, bar_y + 8), fill=TOKENS["ink"], width=3)
    draw.text((bar_x, bar_y + 14), f"{scale_km} km", font=small_font, fill=TOKENS["muted"])

    total_km = length_m(lines.parts) / 1000
    structure_count = structures.feature_count
    line_count = lines.feature_count
    density = structure_count / max(total_km, 0.1)
    bbox_area_km2 = data_w * data_h / 1_000_000

    panel_x = 1380
    draw.text((panel_x, 226), "Dataset readout", font=label_font, fill=TOKENS["ink"])
    metrics = [
        ("Cable features", f"{line_count:,}"),
        ("Approx. cable length", f"{total_km:,.1f} km"),
        ("Structure features", f"{structure_count:,}"),
        ("Structures per km", f"{density:,.1f}"),
        ("Bounding-box area", f"{bbox_area_km2:,.0f} km²"),
    ]
    y = 280
    for label, value in metrics:
        draw.text((panel_x, y), value, font=load_font(34, bold=True), fill=TOKENS["blue_dark"])
        draw.text((panel_x, y + 40), label, font=small_font, fill=TOKENS["muted"])
        y += 102

    draw.text((panel_x, 840), "What this view supports", font=label_font, fill=TOKENS["ink"])
    notes = (
        "Good for corridor screening, proximity checks, planning overlays, and "
        "inspection-zone sketches. It should not be treated as live grid telemetry "
        "or an engineering-grade asset register."
    )
    draw_wrapped(draw, (panel_x, 886), notes, small_font, TOKENS["muted"], width=38)

    source = (
        "Source: Government and Municipalities of Quebec; Ville de Montreal. "
        "Electric transmission lines. Date published: 2024-01-04. "
        "Resource: lignes-transport-electrique-2020.gpkg. "
        "Basemap: OpenStreetMap contributors."
    )
    draw_wrapped(draw, (76, 1130), source, small_font, TOKENS["muted"], width=150)

    img.save(PNG_PATH)
    return {
        "line_features": line_count,
        "structure_features": structure_count,
        "cable_km": total_km,
        "structures_per_km": density,
        "bbox_area_km2": bbox_area_km2,
        "street_background": street_background is not None,
    }


def render_flight_path_map(
    lines: GeometrySet,
    structures: GeometrySet,
    extent: tuple[float, float, float, float],
) -> dict[str, float | bool | str]:
    width, height = 1800, 1200
    left, top, right, bottom = 92, 210, 1320, 1090
    map_left, map_top, map_right, map_bottom = left + 35, top + 35, right - 35, bottom - 35
    map_size = (map_right - map_left, map_bottom - map_top)
    img = Image.new("RGB", (width, height), TOKENS["surface"])
    draw = ImageDraw.Draw(img)

    title_font = load_font(42, bold=True)
    subtitle_font = load_font(23)
    label_font = load_font(22, bold=True)
    small_font = load_font(18)
    metric_font = load_font(34, bold=True)

    destination_xy = midpoint_along_longest_part(lines.parts)
    destination_lonlat = epsg2950_to_lonlat(*destination_xy)
    origin_lonlat = (YUL_AIRPORT["lon"], YUL_AIRPORT["lat"])
    origin_px_world = lonlat_to_tile_pixel(origin_lonlat[0], origin_lonlat[1], TILE_ZOOM)
    dest_px_world = lonlat_to_tile_pixel(destination_lonlat[0], destination_lonlat[1], TILE_ZOOM)
    world_extent = tile_pixel_extent(lines, structures, TILE_ZOOM, extra_pixels=[origin_px_world, dest_px_world])

    min_x, min_y, max_x, max_y = extent
    data_w = max_x - min_x
    data_h = max_y - min_y
    min_tx, min_ty, max_tx, max_ty = world_extent
    world_w = max_tx - min_tx
    world_h = max_ty - min_ty
    scale = min(map_size[0] / world_w, map_size[1] / world_h)
    draw_w = world_w * scale
    draw_h = world_h * scale
    offset_x = map_left + (map_size[0] - draw_w) / 2
    offset_y = map_top + (map_size[1] - draw_h) / 2

    def project_epsg2950(point: tuple[float, float]) -> tuple[float, float]:
        x, y = epsg2950_to_tile_pixel(point, TILE_ZOOM)
        return offset_x + (x - min_tx) * scale, offset_y + (y - min_ty) * scale

    def project_lonlat(point: tuple[float, float]) -> tuple[float, float]:
        x, y = lonlat_to_tile_pixel(point[0], point[1], TILE_ZOOM)
        return offset_x + (x - min_tx) * scale, offset_y + (y - min_ty) * scale

    title = "Conceptual helicopter route from YUL to a transmission-line midpoint"
    subtitle = (
        "Shortest straight-line transit to the midpoint of the longest cable feature. "
        "For planning visualization only; not a flight plan, clearance, or navigation product."
    )
    draw.text((76, 42), title, font=title_font, fill=TOKENS["ink"])
    draw_wrapped(draw, (78, 104), subtitle, subtitle_font, TOKENS["muted"], width=112)

    draw.rounded_rectangle((left, top, right, bottom), radius=10, fill=TOKENS["panel"], outline=TOKENS["axis"], width=2)
    street_background = build_street_background(
        map_size,
        world_extent,
        scale,
        TILE_ZOOM,
        offset=(offset_x - map_left, offset_y - map_top),
    )
    if street_background is not None:
        img.paste(street_background, (map_left, map_top))
    else:
        for i in range(0, 6):
            gx = map_left + i * map_size[0] / 5
            gy = map_top + i * map_size[1] / 5
            draw.line((gx, map_top, gx, map_bottom), fill=TOKENS["grid"], width=1)
            draw.line((map_left, gy, map_right, gy), fill=TOKENS["grid"], width=1)

    for part in lines.parts:
        if len(part) < 2:
            continue
        pts = [project_epsg2950(point) for point in part]
        draw.line(pts, fill=TOKENS["blue_dark"], width=5, joint="curve")
        draw.line(pts, fill=TOKENS["blue_base"], width=2, joint="curve")

    for x, y in polygon_centroids(structures.parts):
        px, py = project_epsg2950((x, y))
        r = 2.7
        draw.ellipse((px - r, py - r, px + r, py + r), fill=TOKENS["orange_base"], outline=TOKENS["orange_dark"], width=1)

    origin_screen = project_lonlat(origin_lonlat)
    dest_screen = project_lonlat(destination_lonlat)
    route_color = TOKENS["gold_dark"]
    route_fill = TOKENS["gold_base"]
    draw.line((origin_screen[0], origin_screen[1], dest_screen[0], dest_screen[1]), fill=route_color, width=8)
    draw.line((origin_screen[0], origin_screen[1], dest_screen[0], dest_screen[1]), fill=route_fill, width=4)

    for point, label, fill, outline in [
        (origin_screen, "YUL", TOKENS["panel"], TOKENS["ink"]),
        (dest_screen, "MID", route_fill, route_color),
    ]:
        x, y = point
        draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=fill, outline=outline, width=4)
        draw.text((x + 16, y - 23), label, font=load_font(20, bold=True), fill=TOKENS["ink"])

    route_km = haversine_km(origin_lonlat, destination_lonlat)
    bearing = initial_bearing(origin_lonlat, destination_lonlat)

    legend_x, legend_y = left + 30, bottom - 84
    draw.rounded_rectangle((legend_x - 16, legend_y - 18, legend_x + 595, legend_y + 54), radius=8, fill="#FFFFFF", outline=TOKENS["axis"], width=1)
    draw.line((legend_x, legend_y, legend_x + 68, legend_y), fill=route_color, width=7)
    draw.line((legend_x, legend_y, legend_x + 68, legend_y), fill=route_fill, width=3)
    draw.text((legend_x + 84, legend_y - 13), "Conceptual shortest route", font=small_font, fill=TOKENS["ink"])
    draw.line((legend_x + 324, legend_y, legend_x + 392, legend_y), fill=TOKENS["blue_dark"], width=5)
    draw.line((legend_x + 324, legend_y, legend_x + 392, legend_y), fill=TOKENS["blue_base"], width=2)
    draw.text((legend_x + 408, legend_y - 13), "Transmission line", font=small_font, fill=TOKENS["ink"])

    center_lat = (origin_lonlat[1] + destination_lonlat[1]) / 2
    meters_per_tile_px = 156543.03392804097 * math.cos(math.radians(center_lat)) / (2**TILE_ZOOM)
    scale_km = 10
    bar_px = scale_km * 1000 / meters_per_tile_px * scale
    bar_x, bar_y = right - 240, bottom - 72
    draw.line((bar_x, bar_y, bar_x + bar_px, bar_y), fill=TOKENS["ink"], width=4)
    draw.line((bar_x, bar_y - 8, bar_x, bar_y + 8), fill=TOKENS["ink"], width=3)
    draw.line((bar_x + bar_px, bar_y - 8, bar_x + bar_px, bar_y + 8), fill=TOKENS["ink"], width=3)
    draw.text((bar_x, bar_y + 14), f"{scale_km} km", font=small_font, fill=TOKENS["muted"])

    panel_x = 1380
    draw.text((panel_x, 226), "Route readout", font=label_font, fill=TOKENS["ink"])
    metrics = [
        ("Straight-line distance", f"{route_km:,.1f} km"),
        ("Initial bearing", f"{bearing:03.0f}° {compass_label(bearing)}"),
        ("Origin", "YUL / Dorval"),
        ("Destination", "Longest-line midpoint"),
        ("Cable network length", f"{length_m(lines.parts) / 1000:,.1f} km"),
    ]
    y = 280
    for label, value in metrics:
        draw.text((panel_x, y), value, font=metric_font, fill=TOKENS["blue_dark"])
        draw.text((panel_x, y + 40), label, font=small_font, fill=TOKENS["muted"])
        y += 102

    draw.text((panel_x, 840), "Assumptions", font=label_font, fill=TOKENS["ink"])
    notes = (
        "This optimizes only for shortest horizontal distance. It does not account "
        "for weather, altitude, controlled airspace, ATC clearance, heliport procedures, "
        "obstacles, terrain, noise abatement, or company operating limits."
    )
    draw_wrapped(draw, (panel_x, 886), notes, small_font, TOKENS["muted"], width=38)

    source = (
        "Source: Ville de Montreal electric transmission line GeoPackage; basemap: OpenStreetMap contributors. "
        "Airport point uses approximate YUL coordinates. Planning visualization only; verify any real route "
        "with certified aviation sources and the appropriate authorities."
    )
    draw_wrapped(draw, (76, 1130), source, small_font, TOKENS["muted"], width=150)

    img.save(FLIGHT_PNG_PATH)
    return {
        "route_km": route_km,
        "bearing": bearing,
        "bearing_label": compass_label(bearing),
        "destination_lon": destination_lonlat[0],
        "destination_lat": destination_lonlat[1],
        "bbox_area_km2": data_w * data_h / 1_000_000,
        "street_background": street_background is not None,
    }


def write_html(metrics: dict[str, float | bool]) -> None:
    rel_png = PNG_PATH.name
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Montreal Electric Transmission Lines</title>
  <style>
    body {{
      margin: 0;
      background: {TOKENS["surface"]};
      color: {TOKENS["ink"]};
      font-family: Arial, Helvetica, sans-serif;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 48px; }}
    img {{ width: 100%; height: auto; border: 1px solid {TOKENS["axis"]}; border-radius: 8px; background: white; }}
    p {{ color: {TOKENS["muted"]}; line-height: 1.45; }}
    code {{ background: #f1f3f7; padding: 2px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
<main>
  <img src="{rel_png}" alt="Static visualization of Montreal electric transmission lines and structures">
  <p>
    Parsed from the GeoPackage with <code>sqlite3</code> and rendered with Python/Pillow:
    {metrics["line_features"]:,.0f} cable features, {metrics["structure_features"]:,.0f}
    structure features, and approximately {metrics["cable_km"]:,.1f} km of cable centerlines.
  </p>
</main>
</body>
</html>
"""
    HTML_PATH.write_text(html, encoding="utf-8")


def write_flight_html(metrics: dict[str, float | bool | str]) -> None:
    rel_png = FLIGHT_PNG_PATH.name
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YUL to Transmission Midpoint Flight Path</title>
  <style>
    body {{
      margin: 0;
      background: {TOKENS["surface"]};
      color: {TOKENS["ink"]};
      font-family: Arial, Helvetica, sans-serif;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 48px; }}
    img {{ width: 100%; height: auto; border: 1px solid {TOKENS["axis"]}; border-radius: 8px; background: white; }}
    p {{ color: {TOKENS["muted"]}; line-height: 1.45; }}
    strong {{ color: {TOKENS["ink"]}; }}
  </style>
</head>
<body>
<main>
  <img src="{rel_png}" alt="Conceptual helicopter route from YUL to a transmission-line midpoint">
  <p>
    Conceptual shortest route: <strong>{metrics["route_km"]:,.1f} km</strong>,
    initial bearing <strong>{metrics["bearing"]:03.0f}° {metrics["bearing_label"]}</strong>,
    to selected destination <strong>{metrics["destination_lat"]:.5f}, {metrics["destination_lon"]:.5f}</strong>.
    This is a GIS planning visualization only, not an operational aviation route.
  </p>
</main>
</body>
</html>
"""
    FLIGHT_HTML_PATH.write_text(html, encoding="utf-8")


def geojson_for_interactive(lines: GeometrySet, structures: GeometrySet) -> tuple[dict, dict]:
    line_features = []
    for idx, part in enumerate(lines.parts):
        if len(part) < 2:
            continue
        coords = [list(epsg2950_to_lonlat(x, y)) for x, y in part]
        line_features.append(
            {
                "type": "Feature",
                "properties": {"id": idx, "kind": "aerial_cable"},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )

    structure_features = []
    for idx, (x, y) in enumerate(polygon_centroids(structures.parts)):
        structure_features.append(
            {
                "type": "Feature",
                "properties": {"id": idx, "kind": "support_base"},
                "geometry": {"type": "Point", "coordinates": list(epsg2950_to_lonlat(x, y))},
            }
        )

    return (
        {"type": "FeatureCollection", "features": line_features},
        {"type": "FeatureCollection", "features": structure_features},
    )


def write_interactive_picker_html(lines: GeometrySet, structures: GeometrySet) -> None:
    line_geojson, structure_geojson = geojson_for_interactive(lines, structures)
    default_xy = midpoint_along_longest_part(lines.parts)
    default_lon, default_lat = epsg2950_to_lonlat(*default_xy)
    default_route_km = haversine_km((YUL_AIRPORT["lon"], YUL_AIRPORT["lat"]), (default_lon, default_lat))
    default_bearing = initial_bearing((YUL_AIRPORT["lon"], YUL_AIRPORT["lat"]), (default_lon, default_lat))
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Interactive YUL Flight Path Picker</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ5kT9Tw2VpMZrjLv2Kc7f6D5gC5p8o=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    /* Critical Leaflet CSS inlined so the page still renders if the CDN stylesheet is blocked. */
    .leaflet-container {{
      overflow: hidden;
      background: #ddd;
      outline-offset: 1px;
      font-family: Arial, Helvetica, sans-serif;
    }}
    .leaflet-pane,
    .leaflet-tile,
    .leaflet-marker-icon,
    .leaflet-marker-shadow,
    .leaflet-tile-container,
    .leaflet-pane > svg,
    .leaflet-pane > canvas,
    .leaflet-zoom-box,
    .leaflet-image-layer,
    .leaflet-layer {{
      position: absolute;
      left: 0;
      top: 0;
    }}
    .leaflet-container {{ -webkit-tap-highlight-color: transparent; }}
    .leaflet-container a {{ color: #0078A8; }}
    .leaflet-tile {{
      filter: inherit;
      visibility: hidden;
      user-select: none;
      -webkit-user-drag: none;
    }}
    .leaflet-tile-loaded {{ visibility: inherit; }}
    .leaflet-tile-container {{ pointer-events: none; }}
    .leaflet-pane {{ z-index: 400; }}
    .leaflet-tile-pane {{ z-index: 200; }}
    .leaflet-overlay-pane {{ z-index: 400; }}
    .leaflet-shadow-pane {{ z-index: 500; }}
    .leaflet-marker-pane {{ z-index: 600; }}
    .leaflet-tooltip-pane {{ z-index: 650; }}
    .leaflet-popup-pane {{ z-index: 700; }}
    .leaflet-map-pane canvas {{ z-index: 100; }}
    .leaflet-map-pane svg {{ z-index: 200; }}
    .leaflet-vml-shape {{
      width: 1px;
      height: 1px;
    }}
    .lvml {{ behavior: url(#default#VML); display: inline-block; position: absolute; }}
    .leaflet-control {{
      position: relative;
      z-index: 800;
      pointer-events: visiblePainted;
      pointer-events: auto;
    }}
    .leaflet-top,
    .leaflet-bottom {{
      position: absolute;
      z-index: 1000;
      pointer-events: none;
    }}
    .leaflet-top {{ top: 0; }}
    .leaflet-right {{ right: 0; }}
    .leaflet-bottom {{ bottom: 0; }}
    .leaflet-left {{ left: 0; }}
    .leaflet-control {{ float: left; clear: both; }}
    .leaflet-right .leaflet-control {{ float: right; }}
    .leaflet-top .leaflet-control {{ margin-top: 10px; }}
    .leaflet-bottom .leaflet-control {{ margin-bottom: 10px; }}
    .leaflet-left .leaflet-control {{ margin-left: 10px; }}
    .leaflet-right .leaflet-control {{ margin-right: 10px; }}
    .leaflet-bottom.leaflet-left {{ left: 50%; max-width: calc(100% - 24px); transform: translateX(-50%); }}
    .leaflet-bottom.leaflet-left .leaflet-control {{ margin-left: 0; margin-bottom: 16px; }}
    .leaflet-control-zoom a {{
      display: block;
      width: 30px;
      height: 30px;
      line-height: 30px;
      text-align: center;
      text-decoration: none;
      background: #fff;
      border-bottom: 1px solid #ccc;
      color: #1F2430;
      font: bold 18px Arial, Helvetica, sans-serif;
    }}
    .leaflet-control-zoom a:first-child {{ border-radius: 4px 4px 0 0; }}
    .leaflet-control-zoom a:last-child {{ border-radius: 0 0 4px 4px; border-bottom: 0; }}
    .leaflet-control-attribution {{
      padding: 2px 6px;
      background: rgba(255,255,255,.82);
      color: #333;
      font-size: 11px;
    }}
    .leaflet-control-attribution a {{ text-decoration: none; }}
    .leaflet-interactive {{ cursor: pointer; }}
    .leaflet-tooltip {{
      position: absolute;
      padding: 6px 8px;
      background-color: #fff;
      border: 1px solid #fff;
      border-radius: 3px;
      color: #222;
      white-space: nowrap;
      user-select: none;
      pointer-events: none;
      box-shadow: 0 1px 3px rgba(0,0,0,.25);
    }}
    .leaflet-tooltip-left {{ margin-left: -6px; }}
    .leaflet-tooltip-right {{ margin-left: 6px; }}
    .leaflet-tooltip-top {{ margin-top: -6px; }}
    .leaflet-tooltip-bottom {{ margin-top: 6px; }}
    :root {{
      --surface: {TOKENS["surface"]};
      --panel: {TOKENS["panel"]};
      --ink: {TOKENS["ink"]};
      --muted: {TOKENS["muted"]};
      --axis: {TOKENS["axis"]};
      --blue-base: {TOKENS["blue_base"]};
      --blue-dark: {TOKENS["blue_dark"]};
      --gold-base: {TOKENS["gold_base"]};
      --gold-dark: {TOKENS["gold_dark"]};
      --orange-base: {TOKENS["orange_base"]};
      --orange-dark: {TOKENS["orange_dark"]};
      --pink-base: {TOKENS["pink_base"]};
      --pink-dark: {TOKENS["pink_dark"]};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--surface);
      color: var(--ink);
      font-family: Arial, Helvetica, sans-serif;
    }}
    .app {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      height: 100vh;
      min-height: 100vh;
      overflow: hidden;
    }}
    .map-wrap {{
      position: relative;
      min-height: 0;
      height: 100vh;
      border-right: 1px solid var(--axis);
    }}
    #map {{ position: absolute; inset: 0; }}
    .title {{
      position: absolute;
      top: 24px;
      left: 24px;
      z-index: 700;
      max-width: 760px;
      padding: 16px 18px;
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--axis);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(31, 36, 48, 0.12);
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
      line-height: 1.12;
      letter-spacing: 0;
    }}
    .title p, .panel p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.42;
    }}
    .panel {{
      display: flex;
      flex-direction: column;
      gap: 24px;
      padding: 28px 24px;
      background: var(--panel);
      height: 100vh;
      overflow-y: auto;
    }}
    .metric {{
      padding-bottom: 18px;
      border-bottom: 1px solid var(--axis);
    }}
    .metric strong {{
      display: block;
      color: #2E4780;
      font-size: 34px;
      line-height: 1.05;
      margin-bottom: 4px;
    }}
    .metric span {{
      color: var(--muted);
      font-size: 14px;
    }}
    .section-title {{
      margin: 0 0 10px;
      font-size: 17px;
      line-height: 1.2;
    }}
    .coords {{
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 13px;
      color: var(--muted);
      word-break: break-word;
    }}
    .legend {{
      display: grid;
      gap: 9px;
      font-size: 14px;
      color: var(--ink);
    }}
    .legend-row {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .swatch {{
      width: 36px;
      height: 0;
      border-top: 4px solid var(--blue-dark);
      box-shadow: inset 0 0 0 2px var(--blue-base);
    }}
    .swatch.route {{ border-top-color: var(--gold-dark); }}
    .swatch.point {{
      width: 12px;
      height: 12px;
      border: 1px solid var(--orange-dark);
      border-radius: 50%;
      background: var(--orange-base);
      box-shadow: none;
    }}
    .swatch.airspace {{
      height: 14px;
      border: 2px solid var(--pink-dark);
      background: rgba(243, 144, 202, 0.22);
      box-shadow: none;
    }}
    button {{
      width: 100%;
      padding: 11px 12px;
      border: 1px solid var(--axis);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ border-color: var(--blue-dark); }}
    .leaflet-tooltip.route-label {{
      border: 1px solid var(--axis);
      border-radius: 6px;
      box-shadow: 0 8px 18px rgba(31, 36, 48, 0.12);
      color: var(--ink);
      font-weight: 700;
    }}
    .stop-label {{
      width: 24px;
      height: 24px;
      border: 3px solid var(--gold-dark);
      border-radius: 50%;
      background: var(--gold-base);
      color: var(--ink);
      display: grid;
      place-items: center;
      font-size: 13px;
      font-weight: 800;
      box-shadow: 0 3px 10px rgba(31, 36, 48, 0.22);
    }}
    .flag-list {{
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.35;
    }}
    .flag-list li {{
      padding: 9px 10px;
      border: 1px solid var(--axis);
      border-radius: 6px;
      background: #fff;
    }}
    .flag-list strong {{
      display: block;
      color: var(--ink);
      font-size: 14px;
    }}
    @media (max-width: 860px) {{
      .app {{ grid-template-columns: 1fr; height: auto; overflow: visible; }}
      .map-wrap {{ min-height: 68vh; height: 68vh; border-right: 0; border-bottom: 1px solid var(--axis); }}
      .panel {{ height: auto; overflow: visible; }}
      .title {{ max-width: calc(100% - 32px); left: 16px; top: 16px; }}
      h1 {{ font-size: 20px; }}
    }}
  </style>
</head>
<body>
  <main class="app">
    <section class="map-wrap">
      <div id="map"></div>
      <div class="title">
        <h1>Pick transmission-line stops from YUL</h1>
        <p>Click cable segments to add snapped stops. The route reorders them to minimize total straight-line distance from YUL.</p>
      </div>
    </section>
    <aside class="panel">
      <section>
        <h2 class="section-title">Route readout</h2>
        <div class="metric"><strong id="distance">{default_route_km:.1f} km</strong><span>Optimized route distance</span></div>
        <div class="metric"><strong id="bearing">{default_bearing:03.0f}° {compass_label(default_bearing)}</strong><span>First-leg bearing</span></div>
        <div class="metric"><strong id="stopCount">1 stop</strong><span>Selected line stops</span></div>
        <div class="metric"><strong>YUL / Dorval</strong><span>Origin</span></div>
        <p class="coords" id="coords">1. {default_lat:.5f}, {default_lon:.5f}</p>
      </section>
      <section>
        <h2 class="section-title">Map layers</h2>
        <div class="legend">
          <div class="legend-row"><span class="swatch route"></span><span>Conceptual shortest route</span></div>
          <div class="legend-row"><span class="swatch"></span><span>Transmission cable</span></div>
          <div class="legend-row"><span class="swatch point"></span><span>Support/base structures</span></div>
          <div class="legend-row"><span class="swatch airspace"></span><span>DAH airspace reference</span></div>
        </div>
      </section>
      <section>
        <h2 class="section-title">Airspace flags</h2>
        <ul class="flag-list" id="airspaceFlags">
          <li><strong>Reference layer</strong>Select stops to evaluate route intersections.</li>
        </ul>
      </section>
      <section>
        <h2 class="section-title">Assumptions</h2>
        <p>This optimizes only for shortest horizontal distance. It does not account for weather, altitude, controlled airspace, ATC clearance, obstacles, terrain, noise abatement, or company operating limits.</p>
      </section>
      <button id="reset">Reset to default midpoint</button>
      <button id="clear">Clear all stops</button>
    </aside>
  </main>
  <script>
    const yul = [{YUL_AIRPORT["lat"]}, {YUL_AIRPORT["lon"]}];
    const defaultDestination = [{default_lat}, {default_lon}];
    const lineGeojson = {json.dumps(line_geojson, separators=(",", ":"))};
    const structureGeojson = {json.dumps(structure_geojson, separators=(",", ":"))};
    const airspaceAreas = [
      {{
        name: "Montréal TCA reference - YUL 30 NM",
        center: [45.468056, -73.741389],
        radiusNm: 30,
        classLabel: "Class C",
        altitude: "DAH sectors include 5000 ft to 12500 ft shelves",
        source: "NAV CANADA DAH issue 321, effective 2026-05-14 to 2026-07-09"
      }},
      {{
        name: "Montréal TCA reference - YUL 25 NM",
        center: [45.468056, -73.741389],
        radiusNm: 25,
        classLabel: "Class C",
        altitude: "DAH sectors reference YUL 25 NM arcs with multiple shelves",
        source: "NAV CANADA DAH Montréal TCA descriptions"
      }},
      {{
        name: "Montréal TCA lower reference - YUL 12 NM",
        center: [45.468056, -73.741389],
        radiusNm: 12,
        classLabel: "Class C",
        altitude: "DAH sectors include 1500 ft to 2000 ft shelves",
        source: "NAV CANADA DAH Montréal TCA descriptions"
      }},
      {{
        name: "St-Hubert reference - 12 NM",
        center: [45.5175, -73.416944],
        radiusNm: 12,
        classLabel: "Class C",
        altitude: "DAH sector reference, altitude varies by sector",
        source: "NAV CANADA DAH Montréal TCA descriptions"
      }},
      {{
        name: "Mirabel CYMX reference - 16 NM",
        center: [45.682, -74.005167],
        radiusNm: 16,
        classLabel: "Class C",
        altitude: "DAH sectors include 5000 ft to 12500 ft shelves",
        source: "NAV CANADA DAH Montréal TCA descriptions"
      }},
      {{
        name: "Mirabel CYMX lower reference - 11 NM",
        center: [45.668289, -74.065633],
        radiusNm: 11,
        classLabel: "Class C",
        altitude: "DAH lower-sector reference, exact sector geometry not represented",
        source: "NAV CANADA DAH Montréal TCA descriptions"
      }}
    ];

    const map = L.map("map", {{ zoomControl: true }}).setView([45.51, -73.67], 11);
    const streetTiles = L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);
    const satelliteTiles = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
      maxZoom: 19,
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and contributors"
    }});

    const helicopterModel = {{
      name: "Airbus H125",
      cruiseKmh: 252,
      enduranceHours: 4 + 27 / 60,
      rangeKm: 630,
      reserveMinutes: 30,
      hoverMinutesPerStop: 5,
      hoverFuelMultiplier: 1.15,
      maxFleetSize: 4,
      source: "Airbus H125 specs: 136 kt fast cruise, 4h 27min endurance, 340 NM / 630 km range at MTOW"
    }};
    const routePalette = [
      {{ halo: "{TOKENS["gold_dark"]}", fill: "{TOKENS["gold_base"]}" }},
      {{ halo: "#245C6B", fill: "#5FB4C8" }},
      {{ halo: "#7C3F58", fill: "#F390CA" }},
      {{ halo: "#2F6E45", fill: "#82C48C" }}
    ];
    const reserveRangeKm = helicopterModel.cruiseKmh * helicopterModel.reserveMinutes / 60;
    const usableRangeKm = Math.max(0, helicopterModel.rangeKm - reserveRangeKm);
    const reserveFuelPercent = reserveRangeKm / helicopterModel.rangeKm * 100;
    const usableFuelPercent = Math.max(0, 100 - reserveFuelPercent);

    const cableStyle = {{ color: "{TOKENS["blue_dark"]}", weight: 5, opacity: 0.98 }};
    const cableHaloStyle = {{ color: "{TOKENS["blue_base"]}", weight: 8, opacity: 0.5 }};
    L.geoJSON(lineGeojson, {{ style: cableHaloStyle }}).addTo(map);
    const cableLayer = L.geoJSON(lineGeojson, {{
      style: cableStyle,
      onEachFeature: (feature, layer) => {{
        layer.on("click", (event) => addSelectedPoint(snapToNetwork(event.latlng)));
      }}
    }}).addTo(map);

    const structureLayer = L.geoJSON(structureGeojson, {{
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {{
        radius: 3,
        color: "{TOKENS["orange_dark"]}",
        weight: 1,
        fillColor: "{TOKENS["orange_base"]}",
        fillOpacity: 0.9
      }})
    }}).addTo(map);

    const airspaceLayer = L.layerGroup(airspaceAreas.map(area => L.circle(area.center, {{
      radius: area.radiusNm * 1852,
      color: "{TOKENS["pink_dark"]}",
      weight: 2,
      opacity: 0.85,
      fillColor: "{TOKENS["pink_base"]}",
      fillOpacity: 0.12,
      interactive: false
    }}))).addTo(map);

    const yulMarker = L.circleMarker(yul, {{
      radius: 9,
      color: "{TOKENS["ink"]}",
      weight: 3,
      fillColor: "#fff",
      fillOpacity: 1
    }}).bindTooltip("YUL", {{ permanent: true, direction: "right", className: "route-label" }}).addTo(map);

    let routeLine = L.polyline([], {{
      color: "{TOKENS["gold_dark"]}",
      weight: 8,
      opacity: 0.95
    }}).addTo(map);
    let routeFill = L.polyline([], {{
      color: "{TOKENS["gold_base"]}",
      weight: 4,
      opacity: 1
    }}).addTo(map);
    let selectedPoints = [];
    let stopMarkers = [];

    const allBounds = L.featureGroup([cableLayer, structureLayer, yulMarker, routeLine]).getBounds();
    map.fitBounds(allBounds.pad(0.05));

    L.control.layers(null, {{
      "DAH airspace reference": airspaceLayer,
      "Transmission cable": cableLayer,
      "Support/base structures": structureLayer
    }}, {{ collapsed: false, position: "bottomleft" }}).addTo(map);

    map.on("click", (event) => {{
      const snapped = snapToNetwork(event.latlng);
      if (snapped.distanceMeters <= 1500) addSelectedPoint(snapped);
    }});

    document.getElementById("reset").addEventListener("click", () => {{
      resetDefault();
    }});

    document.getElementById("clear").addEventListener("click", () => {{
      selectedPoints = [];
      updateRoute();
    }});

    resetDefault();

    function resetDefault() {{
      selectedPoints = [{{ lat: defaultDestination[0], lng: defaultDestination[1], distanceMeters: 0 }}];
      updateRoute();
    }}

    function addSelectedPoint(point) {{
      const duplicate = selectedPoints.some(existing => haversineKm(existing.lng, existing.lat, point.lng, point.lat) < 0.08);
      if (duplicate) return;
      selectedPoints.push({{ lat: point.lat, lng: point.lng, distanceMeters: point.distanceMeters }});
      updateRoute();
    }}

    function removeSelectedPoint(index) {{
      selectedPoints.splice(index, 1);
      updateRoute();
    }}

    function updateRoute() {{
      for (const marker of stopMarkers) marker.remove();
      stopMarkers = [];

      if (selectedPoints.length === 0) {{
        routeLine.setLatLngs([]);
        routeFill.setLatLngs([]);
        document.getElementById("distance").textContent = "0.0 km";
        document.getElementById("bearing").textContent = "--";
        document.getElementById("stopCount").textContent = "0 stops";
        document.getElementById("coords").textContent = "No stops selected.";
        renderAirspaceFlags([]);
        return;
      }}

      const order = optimizeStopOrder(selectedPoints);
      const orderedStops = order.map(index => selectedPoints[index]);
      const route = [yul, ...orderedStops.map(point => [point.lat, point.lng])];
      routeLine.setLatLngs(route);
      routeFill.setLatLngs(route);

      orderedStops.forEach((point, sequenceIndex) => {{
        const originalIndex = order[sequenceIndex];
        const marker = L.marker([point.lat, point.lng], {{
          icon: L.divIcon({{
            className: "",
            html: `<div class="stop-label">${{sequenceIndex + 1}}</div>`,
            iconSize: [24, 24],
            iconAnchor: [12, 12]
          }})
        }}).addTo(map);
        marker.on("click", () => removeSelectedPoint(originalIndex));
        marker.bindTooltip("Click to remove stop", {{ direction: "right", className: "route-label" }});
        stopMarkers.push(marker);
      }});

      const distance = routeDistanceKm(route);
      const firstStop = orderedStops[0];
      const bearing = initialBearing(yul[1], yul[0], firstStop.lng, firstStop.lat);
      document.getElementById("distance").textContent = `${{distance.toFixed(1)}} km`;
      document.getElementById("bearing").textContent = `${{Math.round(bearing).toString().padStart(3, "0")}}° ${{compassLabel(bearing)}}`;
      document.getElementById("stopCount").textContent = `${{selectedPoints.length}} ${{selectedPoints.length === 1 ? "stop" : "stops"}}`;
      document.getElementById("coords").textContent = orderedStops
        .map((point, index) => `${{index + 1}}. ${{point.lat.toFixed(5)}}, ${{point.lng.toFixed(5)}}`)
        .join("\\n");
      updateAirspaceFlags(route);
    }}

    function updateAirspaceFlags(route) {{
      const intersected = airspaceAreas.filter(area => routeIntersectsArea(route, area));
      renderAirspaceFlags(intersected);
    }}

    function renderAirspaceFlags(areas) {{
      const list = document.getElementById("airspaceFlags");
      if (areas.length === 0) {{
        list.innerHTML = "<li><strong>No reference intersections</strong>The current route does not cross the simplified DAH reference rings shown on this map.</li>";
        return;
      }}
      list.innerHTML = areas.map(area => `
        <li>
          <strong>${{area.name}}</strong>
          ${{area.classLabel}}; ${{area.altitude}}. ${{area.source}}. Reference only; verify exact sector geometry, altitude, ATC requirements, and NOTAMs before any operational planning.
        </li>
      `).join("");
    }}

    function routeIntersectsArea(route, area) {{
      for (let i = 0; i < route.length - 1; i++) {{
        if (pointInsideArea(route[i], area) || pointInsideArea(route[i + 1], area)) return true;
        if (segmentDistanceToAreaCenterMeters(route[i], route[i + 1], area) <= area.radiusNm * 1852) return true;
      }}
      return false;
    }}

    function pointInsideArea(point, area) {{
      return haversineKm(point[1], point[0], area.center[1], area.center[0]) <= area.radiusNm * 1.852;
    }}

    function segmentDistanceToAreaCenterMeters(a, b, area) {{
      const ap = localMetersFromAreaCenter(a, area);
      const bp = localMetersFromAreaCenter(b, area);
      const dx = bp.x - ap.x;
      const dy = bp.y - ap.y;
      const denom = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, -(ap.x * dx + ap.y * dy) / denom));
      const x = ap.x + t * dx;
      const y = ap.y + t * dy;
      return Math.hypot(x, y);
    }}

    function localMetersFromAreaCenter(point, area) {{
      const latScale = 110540;
      const lngScale = 111320 * Math.cos(area.center[0] * Math.PI / 180);
      return {{
        x: (point[1] - area.center[1]) * lngScale,
        y: (point[0] - area.center[0]) * latScale
      }};
    }}

    function optimizeStopOrder(points) {{
      if (points.length <= 1) return points.map((_, index) => index);
      if (points.length <= 8) return exactShortestOpenRoute(points);
      return twoOptImprove(nearestNeighborRoute(points), points);
    }}

    function exactShortestOpenRoute(points) {{
      const remaining = points.map((_, index) => index);
      let bestOrder = remaining.slice();
      let bestDistance = Infinity;

      function walk(prefix, unused) {{
        if (unused.length === 0) {{
          const distance = orderedDistanceKm(prefix, points);
          if (distance < bestDistance) {{
            bestDistance = distance;
            bestOrder = prefix.slice();
          }}
          return;
        }}

        for (let i = 0; i < unused.length; i++) {{
          const next = unused[i];
          const nextUnused = unused.slice(0, i).concat(unused.slice(i + 1));
          walk(prefix.concat(next), nextUnused);
        }}
      }}

      walk([], remaining);
      return bestOrder;
    }}

    function nearestNeighborRoute(points) {{
      const unused = new Set(points.map((_, index) => index));
      const order = [];
      let current = {{ lng: yul[1], lat: yul[0] }};

      while (unused.size) {{
        let bestIndex = null;
        let bestDistance = Infinity;
        for (const index of unused) {{
          const candidate = points[index];
          const distance = haversineKm(current.lng, current.lat, candidate.lng, candidate.lat);
          if (distance < bestDistance) {{
            bestDistance = distance;
            bestIndex = index;
          }}
        }}
        order.push(bestIndex);
        unused.delete(bestIndex);
        current = points[bestIndex];
      }}
      return order;
    }}

    function twoOptImprove(order, points) {{
      let improved = true;
      let best = order.slice();
      let bestDistance = orderedDistanceKm(best, points);

      while (improved) {{
        improved = false;
        for (let i = 0; i < best.length - 1; i++) {{
          for (let k = i + 1; k < best.length; k++) {{
            const candidate = best.slice(0, i).concat(best.slice(i, k + 1).reverse(), best.slice(k + 1));
            const distance = orderedDistanceKm(candidate, points);
            if (distance + 0.001 < bestDistance) {{
              best = candidate;
              bestDistance = distance;
              improved = true;
            }}
          }}
        }}
      }}
      return best;
    }}

    function orderedDistanceKm(order, points) {{
      return routeDistanceKm([yul, ...order.map(index => [points[index].lat, points[index].lng])]);
    }}

    function routeDistanceKm(route) {{
      let total = 0;
      for (let i = 0; i < route.length - 1; i++) {{
        total += haversineKm(route[i][1], route[i][0], route[i + 1][1], route[i + 1][0]);
      }}
      return total;
    }}

    function snapToNetwork(latlng) {{
      const p = mercator(latlng.lng, latlng.lat);
      let best = {{ distanceMeters: Infinity, lat: latlng.lat, lng: latlng.lng }};
      for (const feature of lineGeojson.features) {{
        const coords = feature.geometry.coordinates;
        for (let i = 0; i < coords.length - 1; i++) {{
          const a = mercator(coords[i][0], coords[i][1]);
          const b = mercator(coords[i + 1][0], coords[i + 1][1]);
          const snapped = nearestOnSegment(p, a, b);
          if (snapped.distanceMeters < best.distanceMeters) {{
            const lonLat = inverseMercator(snapped.x, snapped.y);
            best = {{ distanceMeters: snapped.distanceMeters, lat: lonLat.lat, lng: lonLat.lng }};
          }}
        }}
      }}
      return best;
    }}

    function mercator(lng, lat) {{
      const radius = 6378137;
      const clampedLat = Math.max(Math.min(lat, 85.05112878), -85.05112878);
      return {{
        x: radius * lng * Math.PI / 180,
        y: radius * Math.log(Math.tan(Math.PI / 4 + clampedLat * Math.PI / 360))
      }};
    }}

    function inverseMercator(x, y) {{
      const radius = 6378137;
      const lng = x / radius * 180 / Math.PI;
      const lat = (2 * Math.atan(Math.exp(y / radius)) - Math.PI / 2) * 180 / Math.PI;
      return {{ lng, lat }};
    }}

    function nearestOnSegment(p, a, b) {{
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const denom = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / denom));
      const x = a.x + t * dx;
      const y = a.y + t * dy;
      return {{ x, y, distanceMeters: Math.hypot(p.x - x, p.y - y) }};
    }}

    function haversineKm(lon1, lat1, lon2, lat2) {{
      const toRad = degrees => degrees * Math.PI / 180;
      const dlon = toRad(lon2 - lon1);
      const dlat = toRad(lat2 - lat1);
      const phi1 = toRad(lat1);
      const phi2 = toRad(lat2);
      const a = Math.sin(dlat / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlon / 2) ** 2;
      return 6371.0088 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }}

    function initialBearing(lon1, lat1, lon2, lat2) {{
      const toRad = degrees => degrees * Math.PI / 180;
      const y = Math.sin(toRad(lon2 - lon1)) * Math.cos(toRad(lat2));
      const x = Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
        Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(toRad(lon2 - lon1));
      return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }}

    function compassLabel(bearing) {{
      const labels = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
      return labels[Math.floor((bearing + 11.25) / 22.5) % 16];
    }}
  </script>
</body>
</html>
"""
    INTERACTIVE_HTML_PATH.write_text(html, encoding="utf-8")


NIPAWIN_LINES_PATH = DATA_DIR / "nipawin_overpass.json"
NIPAWIN_STRUCTURES_PATH = DATA_DIR / "nipawin_structures_overpass.json"
NIPAWIN_AIRPORT_PATH = DATA_DIR / "nipawin_airport_overpass.json"
NIPAWIN_FOREST_PATH = DATA_DIR / "nipawin_forest_overpass.json"
NIPAWIN_WATER_PATH = DATA_DIR / "nipawin_water_overpass.json"
NIPAWIN_ROADS_PATH = DATA_DIR / "nipawin_roads_overpass.json"


def feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def dms_to_decimal(degrees: int, minutes: int, seconds: float, direction: str) -> float:
    value = degrees + minutes / 60 + seconds / 3600
    return -value if direction in {"S", "W"} else value


def geojson_parts(geojson: dict) -> list[list[tuple[float, float]]]:
    parts = []
    for feature in geojson["features"]:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "LineString":
            parts.append([tuple(coord) for coord in geometry["coordinates"]])
    return parts


def midpoint_along_lonlat_parts(parts: Iterable[Sequence[tuple[float, float]]]) -> tuple[float, float]:
    longest = max(parts, key=lambda part: sum(haversine_km(a, b) for a, b in zip(part, part[1:])) if len(part) >= 2 else 0)
    total = sum(haversine_km(a, b) for a, b in zip(longest, longest[1:]))
    target = total / 2
    walked = 0.0
    for start, end in zip(longest, longest[1:]):
        segment_km = haversine_km(start, end)
        if walked + segment_km >= target:
            ratio = (target - walked) / segment_km if segment_km else 0
            return (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
        walked += segment_km
    return longest[-1]


def load_overpass_json(path: Path) -> dict:
    if not path.exists():
        return {"elements": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"elements": []}


def osm_way_geojson(path: Path, layer_name: str) -> dict:
    features = []
    for element in load_overpass_json(path).get("elements", []):
        if element.get("type") != "way" or not element.get("geometry"):
            continue
        tags = element.get("tags") or {}
        coords = [[point["lon"], point["lat"]] for point in element["geometry"]]
        if len(coords) < 2:
            continue
        is_closed = coords[0] == coords[-1] and len(coords) >= 4
        if layer_name in {"forest", "water"} and is_closed:
            geometry = {"type": "Polygon", "coordinates": [coords]}
        else:
            geometry = {"type": "LineString", "coordinates": coords}
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": element.get("id"),
                    "kind": tags.get("natural") or tags.get("landuse") or tags.get("highway") or tags.get("waterway") or layer_name,
                    "name": tags.get("name", ""),
                    "source": "OpenStreetMap",
                },
                "geometry": geometry,
            }
        )
    return feature_collection(features)


def load_geojson(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        geojson = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if geojson.get("type") == "FeatureCollection" and isinstance(geojson.get("features"), list):
        return geojson
    return None


def context_geojson(area_id: str, layer_name: str, fallback_overpass_path: Path | None = None) -> dict:
    processed = load_geojson(PROCESSED_DIR / area_id / f"{layer_name}.geojson")
    if processed is not None:
        return processed
    if fallback_overpass_path is not None:
        return osm_way_geojson(fallback_overpass_path, layer_name)
    return feature_collection([])


def processed_airport_origin(area_id: str, label: str, fallback_lat: float, fallback_lng: float) -> dict:
    airport_geojson = context_geojson(area_id, "airport")
    features = airport_geojson.get("features", [])
    if features:
        preferred = next(
            (
                feature for feature in features
                if (
                    feature.get("properties", {}).get("icao")
                    and feature.get("properties", {}).get("icao") in label
                )
                or label.split(" / ")[0].lower() in feature.get("properties", {}).get("name", "").lower()
            ),
            features[0],
        )
        coords = preferred["geometry"]["coordinates"]
        return {"label": label, "lat": coords[1], "lng": coords[0]}
    return {"label": label, "lat": fallback_lat, "lng": fallback_lng}


def default_destination_from_lines(line_geojson: dict, fallback_lon: float, fallback_lat: float) -> tuple[float, float]:
    parts = geojson_parts(line_geojson)
    if parts:
        return midpoint_along_lonlat_parts(parts)
    return fallback_lon, fallback_lat


def nipawin_line_geojson() -> dict:
    features = []
    for element in load_overpass_json(NIPAWIN_LINES_PATH).get("elements", []):
        if element.get("type") != "way" or not element.get("geometry"):
            continue
        tags = element.get("tags") or {}
        coords = [[point["lon"], point["lat"]] for point in element["geometry"]]
        if len(coords) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": element["id"],
                    "kind": tags.get("power", "line"),
                    "voltage": tags.get("voltage", "unknown"),
                    "source": "OpenStreetMap",
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return feature_collection(features)


def centroid_lonlat(coords: Sequence[Sequence[float]]) -> tuple[float, float]:
    return (
        sum(coord[0] for coord in coords) / len(coords),
        sum(coord[1] for coord in coords) / len(coords),
    )


def nipawin_structure_geojson() -> dict:
    features = []
    for element in load_overpass_json(NIPAWIN_STRUCTURES_PATH).get("elements", []):
        tags = element.get("tags") or {}
        power = tags.get("power")
        if element.get("type") == "node" and power in {"tower", "pole", "substation"}:
            coords = [element["lon"], element["lat"]]
        elif element.get("type") == "way" and power == "substation" and element.get("geometry"):
            coords = list(centroid_lonlat([[point["lon"], point["lat"]] for point in element["geometry"]]))
        else:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"id": element["id"], "kind": power, "source": "OpenStreetMap"},
                "geometry": {"type": "Point", "coordinates": coords},
            }
        )
    return feature_collection(features)


def nipawin_airport_origin() -> dict:
    for element in load_overpass_json(NIPAWIN_AIRPORT_PATH).get("elements", []):
        tags = element.get("tags") or {}
        if tags.get("icao") != "CYBU" and "nipawin" not in tags.get("name", "").lower():
            continue
        if element.get("type") == "node":
            return {"label": "CYBU / Nipawin", "lat": element["lat"], "lng": element["lon"]}
        if element.get("geometry"):
            lon, lat = centroid_lonlat([[point["lon"], point["lat"]] for point in element["geometry"]])
            return {"label": "CYBU / Nipawin", "lat": lat, "lng": lon}
    return {"label": "CYBU / Nipawin", "lat": 53.332, "lng": -104.008}


def point_in_ring(point: tuple[float, float], ring: Sequence[Sequence[float]]) -> bool:
    x, y = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def point_in_any_polygon(point: tuple[float, float], polygons: Sequence[Sequence[Sequence[float]]]) -> bool:
    return any(point_in_ring(point, polygon) for polygon in polygons)


def line_length_km(parts: Iterable[Sequence[tuple[float, float]]]) -> float:
    return sum(haversine_km(start, end) for part in parts for start, end in zip(part, part[1:]))


def estimate_line_forest_km(line_geojson: dict, forest_geojson: dict) -> float:
    polygons = [
        feature["geometry"]["coordinates"][0]
        for feature in forest_geojson["features"]
        if feature.get("geometry", {}).get("type") == "Polygon"
    ]
    if not polygons:
        return 0.0
    forest_km = 0.0
    for part in geojson_parts(line_geojson):
        for start, end in zip(part, part[1:]):
            midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            if point_in_any_polygon(midpoint, polygons):
                forest_km += haversine_km(start, end)
    return forest_km


def corridor_metrics(line_geojson: dict, structure_geojson: dict, forest_geojson: dict, water_geojson: dict, road_geojson: dict) -> dict:
    parts = geojson_parts(line_geojson)
    total_km = line_length_km(parts)
    forest_km = estimate_line_forest_km(line_geojson, forest_geojson)
    voltages: dict[str, int] = {}
    for feature in line_geojson["features"]:
        voltage = feature.get("properties", {}).get("voltage", "unknown")
        voltages[voltage] = voltages.get(voltage, 0) + 1
    return {
        "lineFeatures": len(line_geojson["features"]),
        "lineKm": round(total_km, 1),
        "forestLineKm": round(forest_km, 1),
        "forestShare": round((forest_km / total_km * 100) if total_km else 0, 1),
        "supportFeatures": len(structure_geojson["features"]),
        "forestPolygons": len(forest_geojson["features"]),
        "waterFeatures": len(water_geojson["features"]),
        "roadFeatures": len(road_geojson["features"]),
        "voltages": voltages,
    }


def montreal_airspace_areas() -> list[dict]:
    return [
        {
            "name": "Montréal TCA reference - YUL 30 NM",
            "center": [45.468056, -73.741389],
            "radiusNm": 30,
            "classLabel": "Class C",
            "altitude": "DAH sectors include 5000 ft to 12500 ft shelves",
            "source": "NAV CANADA DAH issue 321, effective 2026-05-14 to 2026-07-09",
        },
        {
            "name": "Montréal TCA reference - YUL 25 NM",
            "center": [45.468056, -73.741389],
            "radiusNm": 25,
            "classLabel": "Class C",
            "altitude": "DAH sectors reference YUL 25 NM arcs with multiple shelves",
            "source": "NAV CANADA DAH Montréal TCA descriptions",
        },
        {
            "name": "Montréal TCA lower reference - YUL 12 NM",
            "center": [45.468056, -73.741389],
            "radiusNm": 12,
            "classLabel": "Class C",
            "altitude": "DAH sectors include 1500 ft to 2000 ft shelves",
            "source": "NAV CANADA DAH Montréal TCA descriptions",
        },
        {
            "name": "St-Hubert reference - 12 NM",
            "center": [45.5175, -73.416944],
            "radiusNm": 12,
            "classLabel": "Class C",
            "altitude": "DAH sector reference, altitude varies by sector",
            "source": "NAV CANADA DAH Montréal TCA descriptions",
        },
        {
            "name": "Mirabel CYMX reference - 16 NM",
            "center": [45.682, -74.005167],
            "radiusNm": 16,
            "classLabel": "Class C",
            "altitude": "DAH sectors include 5000 ft to 12500 ft shelves",
            "source": "NAV CANADA DAH Montréal TCA descriptions",
        },
        {
            "name": "Mirabel CYMX lower reference - 11 NM",
            "center": [45.668289, -74.065633],
            "radiusNm": 11,
            "classLabel": "Class C",
            "altitude": "DAH lower-sector reference, exact sector geometry not represented",
            "source": "NAV CANADA DAH Montréal TCA descriptions",
        },
    ]


def nipawin_airspace_areas() -> list[dict]:
    prince_albert_ad = [
        dms_to_decimal(53, 12, 51, "N"),
        dms_to_decimal(105, 40, 22, "W"),
    ]
    prince_albert_vor = [
        dms_to_decimal(53, 12, 59, "N"),
        dms_to_decimal(105, 40, 0, "W"),
    ]
    la_ronge_ad = [
        dms_to_decimal(55, 9, 5, "N"),
        dms_to_decimal(105, 15, 43, "W"),
    ]
    cyr309 = [
        dms_to_decimal(53, 11, 50, "N"),
        dms_to_decimal(105, 48, 55, "W"),
    ]
    return [
        {
            "name": "Prince Albert transition reference - 15 NM",
            "center": prince_albert_ad,
            "radiusNm": 15,
            "classLabel": "Class E",
            "altitude": "Transition area; Class B above 12500 ft and Class E at/below 12500 ft in DAH section 3.3.1",
            "source": "NAV CANADA DAH issue 321, Prince Albert (Glass Field), SK",
        },
        {
            "name": "Prince Albert CAE reference - 25 NM",
            "center": prince_albert_vor,
            "radiusNm": 25,
            "classLabel": "Class E / Class B by altitude",
            "altitude": "Control area extension; 25 NM circle plus separate high-level 60 NM reference",
            "source": "NAV CANADA DAH issue 321, section 3.3.2-40",
        },
        {
            "name": "CYR309 Prince Albert restricted area",
            "center": cyr309,
            "radiusNm": 0.5,
            "classLabel": "Class F restricted",
            "altitude": "Surface to 1900 ft; continuous",
            "source": "NAV CANADA DAH issue 321, CYR309 Prince Albert, SK",
        },
        {
            "name": "La Ronge transition reference - 15 NM",
            "center": la_ronge_ad,
            "radiusNm": 15,
            "classLabel": "Class E",
            "altitude": "Transition area; included for northern Saskatchewan context",
            "source": "NAV CANADA DAH issue 321, La Ronge (Barber Field), SK",
        },
    ]


def laronge_airspace_areas() -> list[dict]:
    la_ronge_ad = [
        dms_to_decimal(55, 9, 5, "N"),
        dms_to_decimal(105, 15, 43, "W"),
    ]
    return [
        {
            "name": "La Ronge transition reference - 15 NM",
            "center": la_ronge_ad,
            "radiusNm": 15,
            "classLabel": "Class E",
            "altitude": "Transition area; included as simplified DAH reference geometry",
            "source": "NAV CANADA DAH issue 321, La Ronge (Barber Field), SK",
        },
    ]


def build_interactive_area_data(lines: GeometrySet, structures: GeometrySet) -> dict:
    montreal_line_geojson, montreal_structure_geojson = geojson_for_interactive(lines, structures)
    montreal_parts = geojson_parts(montreal_line_geojson)
    montreal_default_lon, montreal_default_lat = midpoint_along_lonlat_parts(montreal_parts)
    montreal_forest = context_geojson("montreal", "forest")
    montreal_water = context_geojson("montreal", "water")
    montreal_roads = context_geojson("montreal", "roads")

    nipawin_lines = nipawin_line_geojson()
    nipawin_structures = nipawin_structure_geojson()
    nipawin_forest = context_geojson("nipawin", "forest", NIPAWIN_FOREST_PATH)
    nipawin_water = context_geojson("nipawin", "water", NIPAWIN_WATER_PATH)
    nipawin_roads = context_geojson("nipawin", "roads", NIPAWIN_ROADS_PATH)
    nipawin_parts = geojson_parts(nipawin_lines)
    if nipawin_parts:
        nipawin_default_lon, nipawin_default_lat = midpoint_along_lonlat_parts(nipawin_parts)
    else:
        nipawin_default_lon, nipawin_default_lat = -104.0, 53.35
    nipawin_origin = nipawin_airport_origin()

    laronge_lines = context_geojson("laronge", "lines")
    laronge_structures = context_geojson("laronge", "structures")
    laronge_forest = context_geojson("laronge", "forest")
    laronge_water = context_geojson("laronge", "water")
    laronge_roads = context_geojson("laronge", "roads")
    laronge_default_lon, laronge_default_lat = default_destination_from_lines(
        laronge_lines,
        fallback_lon=-105.20,
        fallback_lat=55.10,
    )
    laronge_origin = processed_airport_origin(
        "laronge",
        "CYVC / La Ronge",
        fallback_lat=55.1514,
        fallback_lng=-105.2619,
    )

    return {
        "montreal": {
            "label": "Montréal / YUL",
            "title": "Pick transmission-line stops from YUL",
            "subtitle": "Click cable segments to add snapped stops. The route reorders them to minimize total straight-line distance from the selected airport.",
            "origin": {"label": "YUL / Dorval", "lat": YUL_AIRPORT["lat"], "lng": YUL_AIRPORT["lon"]},
            "defaultDestination": [montreal_default_lat, montreal_default_lon],
            "lineGeojson": montreal_line_geojson,
            "structureGeojson": montreal_structure_geojson,
            "contextLayers": {
                "forest": montreal_forest,
                "water": montreal_water,
                "roads": montreal_roads,
            },
            "corridorMetrics": corridor_metrics(montreal_line_geojson, montreal_structure_geojson, montreal_forest, montreal_water, montreal_roads),
            "airspaceAreas": montreal_airspace_areas(),
            "clickToleranceMeters": 1500,
            "sourceNote": "Transmission data: City of Montréal GeoPackage. Context layers: processed OpenStreetMap/Overpass extracts when available. Airspace: simplified NAV CANADA DAH reference rings.",
        },
        "nipawin": {
            "label": "Nipawin / CYBU",
            "title": "Pick transmission-line stops from CYBU",
            "subtitle": "Click OSM transmission segments around Nipawin/Codette/Tobin Lake. The route reorders selected stops from Nipawin Airport.",
            "origin": nipawin_origin,
            "defaultDestination": [nipawin_default_lat, nipawin_default_lon],
            "lineGeojson": nipawin_lines,
            "structureGeojson": nipawin_structures,
            "contextLayers": {
                "forest": nipawin_forest,
                "water": nipawin_water,
                "roads": nipawin_roads,
            },
            "corridorMetrics": corridor_metrics(nipawin_lines, nipawin_structures, nipawin_forest, nipawin_water, nipawin_roads),
            "airspaceAreas": nipawin_airspace_areas(),
            "clickToleranceMeters": 2500,
            "sourceNote": "Transmission, tower, forest, water, and road context: OpenStreetMap/Overpass prototype extracts. Airspace: simplified NAV CANADA DAH references; CYBU itself was not found as a named DAH controlled-airspace entry in the checked issue.",
        },
        "laronge": {
            "label": "Lac La Ronge / CYVC",
            "title": "Pick transmission-line stops from CYVC",
            "subtitle": "Click OSM transmission segments around La Ronge and Lac La Ronge. The route reorders selected stops from La Ronge Airport.",
            "origin": laronge_origin,
            "defaultDestination": [laronge_default_lat, laronge_default_lon],
            "lineGeojson": laronge_lines,
            "structureGeojson": laronge_structures,
            "contextLayers": {
                "forest": laronge_forest,
                "water": laronge_water,
                "roads": laronge_roads,
            },
            "corridorMetrics": corridor_metrics(laronge_lines, laronge_structures, laronge_forest, laronge_water, laronge_roads),
            "airspaceAreas": laronge_airspace_areas(),
            "clickToleranceMeters": 3000,
            "sourceNote": "Transmission, tower, forest, water, and road context: OpenStreetMap/Overpass prototype extracts. Airspace: simplified NAV CANADA DAH reference for La Ronge; verify current DAH/NOTAMs before operational planning.",
        },
    }


def write_interactive_picker_html(lines: GeometrySet, structures: GeometrySet) -> None:
    area_data = build_interactive_area_data(lines, structures)
    default_area = area_data["montreal"]
    origin = default_area["origin"]
    dest_lat, dest_lng = default_area["defaultDestination"]
    default_route_km = haversine_km((origin["lng"], origin["lat"]), (dest_lng, dest_lat))
    default_bearing = initial_bearing((origin["lng"], origin["lat"]), (dest_lng, dest_lat))
    area_options = "\n".join(
        f'<option value="{area_id}">{area["label"]}</option>'
        for area_id, area in area_data.items()
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transmission Flight Path Planner</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQ5kT9Tw2VpMZrjLv2Kc7f6D5gC5p8o=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    .leaflet-container {{ overflow: hidden; background: #ddd; outline-offset: 1px; font-family: Arial, Helvetica, sans-serif; }}
    .leaflet-pane, .leaflet-tile, .leaflet-marker-icon, .leaflet-marker-shadow, .leaflet-tile-container, .leaflet-pane > svg, .leaflet-pane > canvas, .leaflet-zoom-box, .leaflet-image-layer, .leaflet-layer {{ position: absolute; left: 0; top: 0; }}
    .leaflet-container {{ -webkit-tap-highlight-color: transparent; }}
    .leaflet-container a {{ color: #0078A8; }}
    .leaflet-tile {{ filter: inherit; visibility: hidden; user-select: none; -webkit-user-drag: none; }}
    .leaflet-tile-loaded {{ visibility: inherit; }}
    .leaflet-tile-container {{ pointer-events: none; }}
    .leaflet-pane {{ z-index: 400; }}
    .leaflet-tile-pane {{ z-index: 200; }}
    .leaflet-overlay-pane {{ z-index: 400; }}
    .leaflet-shadow-pane {{ z-index: 500; }}
    .leaflet-marker-pane {{ z-index: 600; }}
    .leaflet-tooltip-pane {{ z-index: 650; }}
    .leaflet-popup-pane {{ z-index: 700; }}
    .leaflet-map-pane canvas {{ z-index: 100; }}
    .leaflet-map-pane svg {{ z-index: 200; }}
    .leaflet-control {{ position: relative; z-index: 800; pointer-events: auto; }}
    .leaflet-top, .leaflet-bottom {{ position: absolute; z-index: 1000; pointer-events: none; }}
    .leaflet-top {{ top: 0; }}
    .leaflet-right {{ right: 0; }}
    .leaflet-bottom {{ bottom: 0; }}
    .leaflet-left {{ left: 0; }}
    .leaflet-control {{ float: left; clear: both; }}
    .leaflet-right .leaflet-control {{ float: right; }}
    .leaflet-top .leaflet-control {{ margin-top: 10px; }}
    .leaflet-bottom .leaflet-control {{ margin-bottom: 10px; }}
    .leaflet-left .leaflet-control {{ margin-left: 10px; }}
    .leaflet-right .leaflet-control {{ margin-right: 10px; }}
    .leaflet-bottom.leaflet-left {{ left: 50%; max-width: calc(100% - 24px); transform: translateX(-50%); }}
    .leaflet-bottom.leaflet-left .leaflet-control {{ margin-left: 0; margin-bottom: 16px; }}
    .leaflet-control-zoom a {{ display: block; width: 30px; height: 30px; line-height: 30px; text-align: center; text-decoration: none; background: #fff; border-bottom: 1px solid #ccc; color: #1F2430; font: bold 18px Arial, Helvetica, sans-serif; }}
    .leaflet-control-zoom a:first-child {{ border-radius: 4px 4px 0 0; }}
    .leaflet-control-zoom a:last-child {{ border-radius: 0 0 4px 4px; border-bottom: 0; }}
    .leaflet-control-layers {{ max-width: min(720px, calc(100vw - 24px)); background: rgba(255,255,255,.9); border-radius: 6px; box-shadow: 0 4px 16px rgba(31,36,48,.14); }}
    .leaflet-control-layers label {{ display: inline-flex; align-items: center; gap: 4px; margin: 0 10px 4px 0; white-space: normal; line-height: 1.15; }}
    .leaflet-control-layers input {{ flex: 0 0 auto; }}
    .leaflet-control-attribution {{ padding: 2px 6px; background: rgba(255,255,255,.82); color: #333; font-size: 11px; }}
    .leaflet-interactive {{ cursor: pointer; }}
    .leaflet-tooltip {{ position: absolute; padding: 6px 8px; background-color: #fff; border: 1px solid #fff; border-radius: 3px; color: #222; white-space: nowrap; user-select: none; pointer-events: none; box-shadow: 0 1px 3px rgba(0,0,0,.25); }}
    :root {{
      --surface: {TOKENS["surface"]};
      --panel: {TOKENS["panel"]};
      --ink: {TOKENS["ink"]};
      --muted: {TOKENS["muted"]};
      --axis: {TOKENS["axis"]};
      --blue-base: {TOKENS["blue_base"]};
      --blue-dark: {TOKENS["blue_dark"]};
      --gold-base: {TOKENS["gold_base"]};
      --gold-dark: {TOKENS["gold_dark"]};
      --orange-base: {TOKENS["orange_base"]};
      --orange-dark: {TOKENS["orange_dark"]};
      --pink-base: {TOKENS["pink_base"]};
      --pink-dark: {TOKENS["pink_dark"]};
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: var(--surface); color: var(--ink); font-family: Arial, Helvetica, sans-serif; }}
    .app {{ display: grid; grid-template-columns: minmax(0, 1fr) 360px; height: 100vh; min-height: 100vh; overflow: hidden; }}
    .map-wrap {{ position: relative; min-height: 0; height: 100vh; border-right: 1px solid var(--axis); }}
    #map {{ position: absolute; inset: 0; }}
    .title {{ position: absolute; top: 24px; left: 56px; z-index: 700; max-width: 760px; padding: 16px 18px; background: rgba(255,255,255,.92); border: 1px solid var(--axis); border-radius: 8px; box-shadow: 0 8px 24px rgba(31,36,48,.12); }}
    h1 {{ margin: 0 0 6px; font-size: 24px; line-height: 1.12; letter-spacing: 0; }}
    .title p, .panel p {{ margin: 0; color: var(--muted); line-height: 1.42; }}
    .panel {{ display: flex; flex-direction: column; gap: 22px; padding: 28px 24px; background: var(--panel); height: 100vh; overflow-y: auto; }}
    .field {{ display: grid; gap: 8px; }}
    label, .metric span {{ color: var(--muted); font-size: 14px; }}
    select {{ width: 100%; min-height: 42px; padding: 9px 10px; border: 1px solid var(--axis); border-radius: 6px; background: #fff; color: var(--ink); font: 700 15px Arial, Helvetica, sans-serif; }}
    input[type="range"] {{ width: 100%; accent-color: var(--blue-dark); }}
    .metric {{ padding-bottom: 18px; border-bottom: 1px solid var(--axis); }}
    .metric strong {{ display: block; color: var(--blue-dark); font-size: 34px; line-height: 1.05; margin-bottom: 4px; }}
    .section-title {{ margin: 0 0 10px; font-size: 17px; line-height: 1.2; }}
    .coords {{ font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 13px; color: var(--muted); white-space: pre-line; word-break: break-word; }}
    .legend {{ display: grid; gap: 9px; font-size: 14px; color: var(--ink); }}
    .legend-row {{ display: flex; align-items: center; gap: 10px; }}
    .route-color-legend {{ display: grid; gap: 8px; margin-top: 12px; }}
    .route-color-row {{ display: grid; grid-template-columns: 44px 1fr; gap: 10px; align-items: center; color: var(--muted); font-size: 13px; line-height: 1.3; }}
    .route-color-row strong {{ display: block; color: var(--ink); font-size: 14px; }}
    .route-color-swatch {{ height: 0; border-top: 8px solid var(--route-halo); box-shadow: inset 0 0 0 2px var(--route-fill); border-radius: 999px; }}
    .swatch {{ width: 36px; height: 0; border-top: 4px solid var(--blue-dark); box-shadow: inset 0 0 0 2px var(--blue-base); }}
    .swatch.route {{ border-top-color: var(--gold-dark); }}
    .swatch.point {{ width: 12px; height: 12px; border: 1px solid var(--orange-dark); border-radius: 50%; background: var(--orange-base); box-shadow: none; }}
    .swatch.airspace {{ height: 14px; border: 2px solid var(--pink-dark); background: rgba(243,144,202,.22); box-shadow: none; }}
    .swatch.forest {{ height: 14px; border: 2px solid #2F6E45; background: rgba(83, 145, 94, .28); box-shadow: none; }}
    .swatch.water {{ height: 14px; border: 2px solid #3F79A8; background: rgba(91, 158, 205, .24); box-shadow: none; }}
    .swatch.road {{ border-top-color: #6B7280; box-shadow: none; }}
    .swatch.forest-water {{ border-top-color: #0F766E; box-shadow: inset 0 0 0 2px #A7F3D0; }}
    .swatch.unplanned {{ width: 12px; height: 12px; border: 2px solid #991B1B; border-radius: 50%; background: rgba(248,113,113,.18); box-shadow: none; }}
    .swatch.scope {{ height: 14px; border: 2px solid #0F766E; background: rgba(20,184,166,.16); box-shadow: none; }}
    .swatch.scope-point {{ width: 12px; height: 12px; border: 2px solid #0F766E; border-radius: 50%; background: #99F6E4; box-shadow: none; }}
    button {{ width: 100%; padding: 11px 12px; border: 1px solid var(--axis); border-radius: 6px; background: var(--panel); color: var(--ink); font-weight: 700; cursor: pointer; }}
    button:hover {{ border-color: var(--blue-dark); }}
    button:disabled {{ cursor: progress; opacity: .62; }}
    .toggle-row {{ display: flex; align-items: flex-start; gap: 10px; padding: 10px; border: 1px solid var(--axis); border-radius: 6px; background: #fff; }}
    .toggle-row input {{ margin-top: 2px; width: 18px; height: 18px; flex: 0 0 auto; accent-color: var(--blue-dark); }}
    .toggle-copy {{ display: grid; gap: 2px; line-height: 1.3; }}
    .toggle-copy strong {{ font-size: 14px; }}
    .toggle-copy span {{ color: var(--muted); font-size: 13px; }}
    .planner-grid {{ display: grid; gap: 12px; }}
    .planner-row {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; }}
    .planner-row select {{ width: 96px; }}
    .planner-actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    .planner-value {{ color: var(--blue-dark); font-size: 18px; font-weight: 800; }}
    .panel-note {{ font-size: 13px; color: var(--muted); line-height: 1.35; }}
    .panel-note.is-working {{ color: var(--blue-dark); font-weight: 700; }}
    .panel-note.is-warning {{ color: #991B1B; font-weight: 700; }}
    .panel-note.is-active {{ color: #0F766E; font-weight: 700; }}
    .leaflet-tooltip.route-label {{ border: 1px solid var(--axis); border-radius: 6px; box-shadow: 0 8px 18px rgba(31,36,48,.12); color: var(--ink); font-weight: 700; }}
    .stop-label {{ min-width: 24px; width: auto; height: 24px; padding: 0 4px; border: 3px solid var(--stop-halo, var(--gold-dark)); border-radius: 999px; background: var(--stop-fill, var(--gold-base)); color: var(--ink); display: grid; place-items: center; font-size: 13px; font-weight: 800; box-shadow: 0 3px 10px rgba(31,36,48,.22); }}
    .flag-list {{ display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; color: var(--muted); font-size: 14px; line-height: 1.35; }}
    .flag-list li {{ padding: 9px 10px; border: 1px solid var(--axis); border-radius: 6px; background: #fff; }}
    .flag-list strong {{ display: block; color: var(--ink); font-size: 14px; }}
    .flag-list li.warn {{ border-color: var(--pink-dark); background: rgba(243,144,202,.12); }}
    .flag-list li.good {{ border-color: #2F6E45; background: rgba(83,145,94,.12); }}
    @media (max-width: 860px) {{
      .app {{ grid-template-columns: 1fr; height: auto; overflow: visible; }}
      .map-wrap {{ min-height: 68vh; height: 68vh; border-right: 0; border-bottom: 1px solid var(--axis); }}
      .panel {{ height: auto; overflow: visible; }}
      .leaflet-control-layers {{ max-width: calc(100vw - 20px); font-size: 13px; }}
      .leaflet-control-layers label {{ margin-right: 8px; }}
      .title {{ max-width: calc(100% - 72px); left: 56px; top: 16px; bottom: auto; padding: 12px 14px; }}
      h1 {{ font-size: 20px; }}
    }}
  </style>
</head>
<body>
  <main class="app">
    <section class="map-wrap">
      <div id="map"></div>
      <div class="title">
        <h1 id="pageTitle">{default_area["title"]}</h1>
        <p id="pageSubtitle">{default_area["subtitle"]}</p>
      </div>
    </section>
    <aside class="panel">
      <section class="field">
        <label for="areaSelect">Study area</label>
        <select id="areaSelect">{area_options}</select>
      </section>
      <section>
        <h2 class="section-title">Route readout</h2>
        <div class="metric"><strong id="distance">{default_route_km:.1f} km</strong><span>Optimized route distance</span></div>
        <div class="metric"><strong id="bearing">{default_bearing:03.0f}° {compass_label(default_bearing)}</strong><span>First-leg bearing</span></div>
        <div class="metric"><strong id="stopCount">1 stop</strong><span>Selected line stops</span></div>
        <div class="metric"><strong id="originName">{origin["label"]}</strong><span>Origin</span></div>
        <p class="coords" id="coords">1. {dest_lat:.5f}, {dest_lng:.5f}</p>
        <div class="route-color-legend" id="routeColorLegend"></div>
        <label class="toggle-row" for="multiHeli">
          <input id="multiHeli" type="checkbox">
          <span class="toggle-copy">
            <strong>Allow multi-helicopter planning</strong>
            <span>Use the selected fleet size for manually selected stops.</span>
          </span>
        </label>
      </section>
      <section>
        <h2 class="section-title">Fleet inspection planning</h2>
        <div class="planner-grid">
          <div class="planner-actions">
            <button id="drawLasso" type="button">Draw lasso</button>
            <button id="clearLasso" type="button">Clear lasso</button>
          </div>
          <p class="panel-note" id="lassoNote">Optional: draw one or more lasso sections on the map to restrict tower coverage planning to selected corridor areas.</p>
          <div class="planner-row">
            <label for="fleetCount">Helicopters</label>
            <select id="fleetCount">
              <option value="1">1</option>
              <option value="2" selected>2</option>
              <option value="3">3</option>
              <option value="4">4</option>
            </select>
          </div>
          <div class="planner-row">
            <label for="coverageTarget">Tower coverage target</label>
            <strong class="planner-value" id="coverageValue">25%</strong>
          </div>
          <input id="coverageTarget" type="range" min="5" max="100" step="5" value="25">
          <label class="toggle-row" for="forceFleetUse">
            <input id="forceFleetUse" type="checkbox" checked>
            <span class="toggle-copy">
              <strong>Use all selected helicopters</strong>
              <span>Seed one inspection route per helicopter before optimizing tower assignments.</span>
            </span>
          </label>
          <button id="planTowers">Plan tower coverage</button>
          <p class="panel-note" id="towerPlanNote">Choose a fleet size and target percentage, then auto-select the lowest-fuel tower/support inspection set for the current study area.</p>
        </div>
      </section>
      <section>
        <h2 class="section-title">Map layers</h2>
        <div class="legend">
          <div class="legend-row"><span class="swatch route"></span><span>Conceptual shortest route</span></div>
          <div class="legend-row"><span class="swatch"></span><span>Transmission cable</span></div>
          <div class="legend-row"><span class="swatch point"></span><span>Support/base structures</span></div>
          <div class="legend-row"><span class="swatch airspace"></span><span>DAH airspace reference</span></div>
          <div class="legend-row"><span class="swatch forest"></span><span>Forest / wood cover</span></div>
          <div class="legend-row"><span class="swatch water"></span><span>Water / drainage</span></div>
          <div class="legend-row"><span class="swatch road"></span><span>Road / access context</span></div>
          <div class="legend-row"><span class="swatch forest-water"></span><span>Forest-to-water scout path</span></div>
          <div class="legend-row"><span class="swatch scope"></span><span>Planning lasso area</span></div>
          <div class="legend-row"><span class="swatch scope-point"></span><span>Tower/support inside lasso</span></div>
          <div class="legend-row"><span class="swatch unplanned"></span><span>Unplanned tower/support due to fuel</span></div>
        </div>
      </section>
      <section>
        <h2 class="section-title">Corridor intelligence</h2>
        <ul class="flag-list" id="corridorStats">
          <li><strong>Loading corridor context</strong>Switch study areas to refresh this readout.</li>
        </ul>
        <ul class="flag-list" id="waterScoutStats">
          <li><strong>Forest-to-water scout</strong>Right-click near a mapped forest area to draw the shortest straight-line path to mapped water.</li>
        </ul>
        <ul class="flag-list" id="fuelStats">
          <li><strong>Loading fuel model</strong>Select stops to evaluate H125 return-to-base fuel.</li>
        </ul>
      </section>
      <section>
        <h2 class="section-title">Airspace flags</h2>
        <ul class="flag-list" id="airspaceFlags">
          <li><strong>Reference layer</strong>Select stops to evaluate route intersections.</li>
        </ul>
      </section>
      <section>
        <h2 class="section-title">Assumptions</h2>
        <p>This optimizes horizontal distance with a range-derived Airbus H125 fuel model plus a fixed 5-minute hover allowance at each stop. It does not account for weather, altitude, payload, winds, variable loiter time, ATC clearance, obstacles, terrain, noise abatement, or company operating limits.</p>
      </section>
      <section>
        <h2 class="section-title">Data sources</h2>
        <p id="sourceNote">{default_area["sourceNote"]}</p>
      </section>
      <button id="reset">Reset to default midpoint</button>
      <button id="clear">Clear all stops</button>
    </aside>
  </main>
  <script>
    const areas = {json.dumps(area_data, separators=(",", ":"))};
    const map = L.map("map", {{ zoomControl: true }}).setView([45.51, -73.67], 11);
    const streetTiles = L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);
    const satelliteTiles = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
      maxZoom: 19,
      attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics, and contributors"
    }});

    const helicopterModel = {{
      name: "Airbus H125",
      cruiseKmh: 252,
      enduranceHours: 4 + 27 / 60,
      rangeKm: 630,
      reserveMinutes: 30,
      hoverMinutesPerStop: 5,
      hoverFuelMultiplier: 1.15,
      maxFleetSize: 4,
      source: "Airbus H125 specs: 136 kt fast cruise, 4h 27min endurance, 340 NM / 630 km range at MTOW"
    }};
    const routePalette = [
      {{ halo: "{TOKENS["gold_dark"]}", fill: "{TOKENS["gold_base"]}" }},
      {{ halo: "#245C6B", fill: "#5FB4C8" }},
      {{ halo: "#7C3F58", fill: "#F390CA" }},
      {{ halo: "#2F6E45", fill: "#82C48C" }}
    ];
    const reserveRangeKm = helicopterModel.cruiseKmh * helicopterModel.reserveMinutes / 60;
    const usableRangeKm = Math.max(0, helicopterModel.rangeKm - reserveRangeKm);
    const reserveFuelPercent = reserveRangeKm / helicopterModel.rangeKm * 100;
    const usableFuelPercent = Math.max(0, 100 - reserveFuelPercent);

    const cableStyle = {{ color: "{TOKENS["blue_dark"]}", weight: 5, opacity: 0.98 }};
    const cableHaloStyle = {{ color: "{TOKENS["blue_base"]}", weight: 8, opacity: 0.5 }};
    const cableHaloGroup = L.layerGroup().addTo(map);
    const cableLayerGroup = L.layerGroup().addTo(map);
    const structureLayerGroup = L.layerGroup().addTo(map);
    const airspaceLayerGroup = L.layerGroup().addTo(map);
    const originLayerGroup = L.layerGroup().addTo(map);
    const forestLayerGroup = L.layerGroup().addTo(map);
    const waterLayerGroup = L.layerGroup().addTo(map);
    const roadLayerGroup = L.layerGroup().addTo(map);
    const routeLayerGroup = L.layerGroup().addTo(map);
    const waterScoutLayerGroup = L.layerGroup().addTo(map);
    const unplannedTowerLayerGroup = L.layerGroup().addTo(map);
    const lassoLayerGroup = L.layerGroup().addTo(map);
    const lassoTowerLayerGroup = L.layerGroup().addTo(map);

    let currentAreaId = "montreal";
    let currentArea = areas[currentAreaId];
    let selectedPoints = [];
    let stopMarkers = [];
    let lastFuelWarning = "";
    let plannerMode = "manual";
    let forcedAircraftCount = null;
    let lastCoverageSummary = null;
    let precomputedRoutePlan = null;
    let activePlanRunId = 0;
    let lassoMode = false;
    let lassoDrawing = false;
    let lassoLatLngs = [];
    let lassoPolygons = [];
    let lassoPreviewLayer = null;
    let scopedTowerIds = new Set();
    let suppressNextMapClick = false;

    L.control.layers({{
      "Street map": streetTiles,
      "Satellite imagery": satelliteTiles
    }}, {{
      "DAH airspace reference": airspaceLayerGroup,
      "Conceptual shortest route": routeLayerGroup,
      "Forest-to-water scout path": waterScoutLayerGroup,
      "Transmission cable": cableLayerGroup,
      "Support/base structures": structureLayerGroup,
      "Forest / wood cover": forestLayerGroup,
      "Water / drainage": waterLayerGroup,
      "Road / access context": roadLayerGroup,
      "Planning lasso area": lassoLayerGroup,
      "Tower/support inside lasso": lassoTowerLayerGroup,
      "Unplanned tower/support due to fuel": unplannedTowerLayerGroup
    }}, {{ collapsed: false, position: "bottomleft" }}).addTo(map);

    document.getElementById("areaSelect").addEventListener("change", event => loadArea(event.target.value));
    document.getElementById("areaSelect").addEventListener("input", event => loadArea(event.target.value));
    window.setInterval(() => {{
      const selected = document.getElementById("areaSelect").value;
      if (selected !== currentAreaId) loadArea(selected);
    }}, 250);
    document.getElementById("reset").addEventListener("click", resetDefault);
    document.getElementById("clear").addEventListener("click", () => {{
      selectedPoints = [];
      lastFuelWarning = "";
      setManualMode();
      updateRoute();
    }});
    document.getElementById("multiHeli").addEventListener("change", () => {{
      lastFuelWarning = "";
      setManualMode({{ keepForcedAircraft: false }});
      updateRoute();
    }});
    document.getElementById("fleetCount").addEventListener("change", () => {{
      if (plannerMode === "towerCoverage") planTowerCoverage();
      else if (document.getElementById("multiHeli").checked) updateRoute();
    }});
    document.getElementById("coverageTarget").addEventListener("input", event => {{
      document.getElementById("coverageValue").textContent = `${{event.target.value}}%`;
    }});
    document.getElementById("coverageTarget").addEventListener("change", () => {{
      if (plannerMode === "towerCoverage") planTowerCoverage();
    }});
    document.getElementById("forceFleetUse").addEventListener("change", () => {{
      if (plannerMode === "towerCoverage") planTowerCoverage();
    }});
    document.getElementById("planTowers").addEventListener("click", planTowerCoverage);
    document.getElementById("drawLasso").addEventListener("click", () => setLassoMode(!lassoMode));
    document.getElementById("clearLasso").addEventListener("click", clearPlanningLasso);
    map.on("mousedown", startLasso);
    map.on("mousemove", extendLasso);
    map.on("mouseup", finishLasso);
    map.on("click", event => {{
      if (suppressNextMapClick) {{
        suppressNextMapClick = false;
        return;
      }}
      if (lassoMode) return;
      const snapped = snapToNetwork(event.latlng);
      if (snapped.distanceMeters <= currentArea.clickToleranceMeters) addSelectedPoint(snapped);
    }});
    map.on("contextmenu", event => {{
      if (lassoMode) return;
      planForestWaterPath(event.latlng);
    }});

    loadArea(currentAreaId);

    function loadArea(areaId) {{
      currentAreaId = areaId;
      currentArea = areas[areaId];
      selectedPoints = [];
      for (const marker of stopMarkers) marker.remove();
      stopMarkers = [];
      cableHaloGroup.clearLayers();
      cableLayerGroup.clearLayers();
      structureLayerGroup.clearLayers();
      airspaceLayerGroup.clearLayers();
      originLayerGroup.clearLayers();
      forestLayerGroup.clearLayers();
      waterLayerGroup.clearLayers();
      roadLayerGroup.clearLayers();
      routeLayerGroup.clearLayers();
      waterScoutLayerGroup.clearLayers();
      unplannedTowerLayerGroup.clearLayers();
      lassoLayerGroup.clearLayers();
      lassoTowerLayerGroup.clearLayers();
      lastFuelWarning = "";
      precomputedRoutePlan = null;
      lassoMode = false;
      lassoDrawing = false;
      lassoLatLngs = [];
      lassoPolygons = [];
      lassoPreviewLayer = null;
      scopedTowerIds = new Set();
      map.dragging.enable();
      map.getContainer().style.cursor = "";
      setManualMode();
      document.getElementById("drawLasso").textContent = "Draw lasso";
      document.getElementById("lassoNote").classList.remove("is-active");
      document.getElementById("lassoNote").textContent = "Optional: draw one or more lasso sections on the map to restrict tower coverage planning to selected corridor areas.";

      document.getElementById("pageTitle").textContent = currentArea.title;
      document.getElementById("pageSubtitle").textContent = currentArea.subtitle;
      document.getElementById("originName").textContent = currentArea.origin.label;
      document.getElementById("sourceNote").textContent = currentArea.sourceNote;

      L.geoJSON(currentArea.lineGeojson, {{ style: cableHaloStyle }}).addTo(cableHaloGroup);
      L.geoJSON(currentArea.lineGeojson, {{
        style: cableStyle,
        onEachFeature: (feature, layer) => {{
          layer.on("click", event => addSelectedPoint(snapToNetwork(event.latlng)));
        }}
      }}).addTo(cableLayerGroup);
      L.geoJSON(currentArea.structureGeojson, {{
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {{
          radius: feature.properties.kind === "substation" ? 5 : 3,
          color: "{TOKENS["orange_dark"]}",
          weight: 1,
          fillColor: "{TOKENS["orange_base"]}",
          fillOpacity: 0.9
        }})
      }}).addTo(structureLayerGroup);
      L.geoJSON(currentArea.contextLayers.forest, {{
        style: () => ({{
          color: "#2F6E45",
          weight: 1,
          opacity: 0.62,
          fillColor: "#53915E",
          fillOpacity: 0.22,
          interactive: false
        }})
      }}).addTo(forestLayerGroup);
      L.geoJSON(currentArea.contextLayers.water, {{
        style: feature => ({{
          color: "#3F79A8",
          weight: feature.geometry.type === "LineString" ? 2 : 1,
          opacity: 0.72,
          fillColor: "#5B9ECD",
          fillOpacity: 0.18,
          interactive: false
        }})
      }}).addTo(waterLayerGroup);
      L.geoJSON(currentArea.contextLayers.roads, {{
        style: () => ({{
          color: "#6B7280",
          weight: 2,
          opacity: 0.72,
          interactive: false
        }})
      }}).addTo(roadLayerGroup);
      for (const area of currentArea.airspaceAreas) {{
        L.circle(area.center, {{
          radius: area.radiusNm * 1852,
          color: "{TOKENS["pink_dark"]}",
          weight: 2,
          opacity: 0.85,
          fillColor: "{TOKENS["pink_base"]}",
          fillOpacity: 0.12,
          interactive: false
        }}).addTo(airspaceLayerGroup);
      }}
      L.circleMarker([currentArea.origin.lat, currentArea.origin.lng], {{
        radius: 9,
        color: "{TOKENS["ink"]}",
        weight: 3,
        fillColor: "#fff",
        fillOpacity: 1
      }}).bindTooltip(currentArea.origin.label.split(" / ")[0], {{ permanent: true, direction: "right", className: "route-label" }}).addTo(originLayerGroup);

      resetDefault();
      renderCorridorStats();
      renderWaterScoutStats(null);
      const bounds = boundsForCurrentArea();
      if (bounds.isValid()) map.fitBounds(bounds.pad(0.08));
    }}

    function boundsForCurrentArea() {{
      const bounds = L.latLngBounds([[currentArea.origin.lat, currentArea.origin.lng]]);
      extendBoundsFromGeojson(bounds, currentArea.lineGeojson);
      extendBoundsFromGeojson(bounds, currentArea.structureGeojson);
      return bounds;
    }}

    function extendBoundsFromGeojson(bounds, geojson) {{
      for (const feature of geojson.features || []) {{
        extendBoundsFromCoordinates(bounds, feature.geometry.coordinates);
      }}
    }}

    function extendBoundsFromCoordinates(bounds, coords) {{
      if (!Array.isArray(coords) || coords.length === 0) return;
      if (typeof coords[0] === "number") {{
        bounds.extend([coords[1], coords[0]]);
        return;
      }}
      for (const child of coords) extendBoundsFromCoordinates(bounds, child);
    }}

    function renderCorridorStats() {{
      const metrics = currentArea.corridorMetrics;
      const voltageText = Object.entries(metrics.voltages || {{}})
        .map(([voltage, count]) => `${{voltage}}: ${{count}}`)
        .join(", ") || "unknown";
      document.getElementById("corridorStats").innerHTML = `
        <li><strong>${{metrics.lineKm.toFixed(1)}} km linework</strong>${{metrics.lineFeatures}} transmission features; voltage tags: ${{voltageText}}.</li>
        <li><strong>${{metrics.forestLineKm.toFixed(1)}} km through mapped forest</strong>${{metrics.forestShare.toFixed(1)}}% of linework based on OSM forest polygon midpoint sampling.</li>
        <li><strong>${{metrics.supportFeatures}} structures</strong>${{metrics.forestPolygons}} forest polygons, ${{metrics.waterFeatures}} water/drainage features, and ${{metrics.roadFeatures}} road/access features loaded.</li>
      `;
    }}

    function renderWaterScoutStats(result) {{
      const list = document.getElementById("waterScoutStats");
      if (!result) {{
        list.innerHTML = "<li><strong>Forest-to-water scout</strong>Right-click near a mapped forest area to draw the shortest straight-line path to mapped water.</li>";
        return;
      }}
      if (!result.ok) {{
        list.innerHTML = `<li class="warn"><strong>Forest-to-water scout</strong>${{result.message}}</li>`;
        return;
      }}
      list.innerHTML = `
        <li class="good"><strong>${{result.distanceKm.toFixed(2)}} km to mapped water</strong>Shortest straight-line path from mapped forest to the nearest mapped water/drainage geometry.</li>
        <li><strong>Forest point</strong>${{result.forest.lat.toFixed(5)}}, ${{result.forest.lng.toFixed(5)}}; right-click was ${{(result.forest.distanceMeters / 1000).toFixed(2)}} km from mapped forest.</li>
        <li><strong>Water point</strong>${{result.water.lat.toFixed(5)}}, ${{result.water.lng.toFixed(5)}}; bearing ${{Math.round(result.bearing).toString().padStart(3, "0")}}° ${{compassLabel(result.bearing)}}.</li>
      `;
    }}

    function planForestWaterPath(latlng) {{
      waterScoutLayerGroup.clearLayers();
      const forestFeatures = currentArea.contextLayers.forest.features || [];
      const waterFeatures = currentArea.contextLayers.water.features || [];
      if (forestFeatures.length === 0) {{
        renderWaterScoutStats({{ ok: false, message: "No mapped forest features are loaded for this study area." }});
        return;
      }}
      if (waterFeatures.length === 0) {{
        renderWaterScoutStats({{ ok: false, message: "No mapped water or drainage features are loaded for this study area." }});
        return;
      }}
      const forest = nearestPointOnGeojson(latlng, currentArea.contextLayers.forest, {{ polygonInteriorAsHit: true }});
      const maxForestSnapMeters = Math.max(currentArea.clickToleranceMeters * 2, 5000);
      if (!forest || forest.distanceMeters > maxForestSnapMeters) {{
        renderWaterScoutStats({{
          ok: false,
          message: `Right-click closer to mapped forest. Nearest forest feature is ${{forest ? (forest.distanceMeters / 1000).toFixed(2) : "unknown"}} km away.`
        }});
        return;
      }}
      const water = nearestPointOnGeojson(L.latLng(forest.lat, forest.lng), currentArea.contextLayers.water, {{ polygonInteriorAsHit: false }});
      if (!water) {{
        renderWaterScoutStats({{ ok: false, message: "Could not find a valid mapped water geometry for this study area." }});
        return;
      }}
      const route = [[forest.lat, forest.lng], [water.lat, water.lng]];
      L.polyline(route, {{ color: "#064E3B", weight: 8, opacity: 0.9, dashArray: "10 8" }}).addTo(waterScoutLayerGroup);
      L.polyline(route, {{ color: "#A7F3D0", weight: 4, opacity: 1, dashArray: "10 8" }}).addTo(waterScoutLayerGroup);
      L.circleMarker(route[0], {{
        radius: 7,
        color: "#064E3B",
        weight: 2,
        fillColor: "#A7F3D0",
        fillOpacity: 1
      }}).bindTooltip("Mapped forest start", {{ direction: "right", className: "route-label" }}).addTo(waterScoutLayerGroup);
      L.circleMarker(route[1], {{
        radius: 7,
        color: "#0F4C81",
        weight: 2,
        fillColor: "#BFDBFE",
        fillOpacity: 1
      }}).bindTooltip("Nearest mapped water", {{ direction: "right", className: "route-label" }}).addTo(waterScoutLayerGroup);
      const distanceKm = haversineKm(forest.lng, forest.lat, water.lng, water.lat);
      const bearing = initialBearing(forest.lng, forest.lat, water.lng, water.lat);
      renderWaterScoutStats({{ ok: true, forest, water, distanceKm, bearing }});
    }}

    function nearestPointOnGeojson(latlng, geojson, options = {{}}) {{
      let best = null;
      for (const feature of geojson.features || []) {{
        const candidate = nearestPointOnGeometry(latlng, feature.geometry, options);
        if (candidate && (!best || candidate.distanceMeters < best.distanceMeters)) best = candidate;
      }}
      return best;
    }}

    function nearestPointOnGeometry(latlng, geometry, options = {{}}) {{
      if (!geometry) return null;
      const type = geometry.type;
      const coords = geometry.coordinates;
      if (type === "Point") return pointCandidate(latlng, coords);
      if (type === "LineString") return nearestPointOnLineString(latlng, coords);
      if (type === "MultiLineString") return nearestFromCollection(latlng, coords, line => nearestPointOnLineString(latlng, line));
      if (type === "Polygon") return nearestPointOnPolygon(latlng, coords, options);
      if (type === "MultiPolygon") return nearestFromCollection(latlng, coords, polygon => nearestPointOnPolygon(latlng, polygon, options));
      return null;
    }}

    function nearestFromCollection(latlng, collection, fn) {{
      let best = null;
      for (const item of collection || []) {{
        const candidate = fn(item);
        if (candidate && (!best || candidate.distanceMeters < best.distanceMeters)) best = candidate;
      }}
      return best;
    }}

    function pointCandidate(latlng, coord) {{
      return {{
        lat: coord[1],
        lng: coord[0],
        distanceMeters: haversineKm(latlng.lng, latlng.lat, coord[0], coord[1]) * 1000
      }};
    }}

    function nearestPointOnPolygon(latlng, rings, options = {{}}) {{
      if (options.polygonInteriorAsHit && rings?.[0] && pointInRingLonLat([latlng.lng, latlng.lat], rings[0])) {{
        return {{ lat: latlng.lat, lng: latlng.lng, distanceMeters: 0 }};
      }}
      return nearestFromCollection(latlng, rings || [], ring => nearestPointOnLineString(latlng, ring));
    }}

    function nearestPointOnLineString(latlng, coords) {{
      if (!coords || coords.length === 0) return null;
      if (coords.length === 1) return pointCandidate(latlng, coords[0]);
      const p = mercator(latlng.lng, latlng.lat);
      let best = null;
      for (let i = 0; i < coords.length - 1; i++) {{
        const a = mercator(coords[i][0], coords[i][1]);
        const b = mercator(coords[i + 1][0], coords[i + 1][1]);
        const snapped = nearestOnSegment(p, a, b);
        if (!best || snapped.distanceMeters < best.distanceMeters) {{
          const lonLat = inverseMercator(snapped.x, snapped.y);
          best = {{ lat: lonLat.lat, lng: lonLat.lng, distanceMeters: snapped.distanceMeters }};
        }}
      }}
      return best;
    }}

    function pointInRingLonLat(point, ring) {{
      const x = point[0];
      const y = point[1];
      let inside = false;
      let j = ring.length - 1;
      for (let i = 0; i < ring.length; i++) {{
        const xi = ring[i][0];
        const yi = ring[i][1];
        const xj = ring[j][0];
        const yj = ring[j][1];
        const intersects = ((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi);
        if (intersects) inside = !inside;
        j = i;
      }}
      return inside;
    }}

    function extendBounds(bounds, layer) {{
      for (const child of layer.getLayers()) {{
        if (typeof child.getBounds === "function") {{
          const childBounds = child.getBounds();
          if (childBounds.isValid()) bounds.extend(childBounds);
        }} else if (typeof child.getLatLng === "function") {{
          bounds.extend(child.getLatLng());
        }} else if (typeof child.eachLayer === "function") {{
          extendBounds(bounds, child);
        }}
      }}
    }}

    function setLassoMode(enabled) {{
      lassoMode = enabled;
      lassoDrawing = false;
      lassoLatLngs = [];
      if (lassoPreviewLayer) {{
        lassoPreviewLayer.remove();
        lassoPreviewLayer = null;
      }}
      const button = document.getElementById("drawLasso");
      const note = document.getElementById("lassoNote");
      button.textContent = lassoMode ? "Finish lasso mode" : "Draw lasso";
      note.classList.toggle("is-active", lassoMode);
      note.textContent = lassoMode
        ? "Lasso active: drag on the map around a corridor section. Repeat Draw lasso to add more sections."
        : lassoSummaryText();
      map.getContainer().style.cursor = lassoMode ? "crosshair" : "";
      if (lassoMode) map.dragging.disable();
      else map.dragging.enable();
    }}

    function startLasso(event) {{
      if (!lassoMode) return;
      if (event.originalEvent?.button !== 0) return;
      suppressNextMapClick = true;
      lassoDrawing = true;
      lassoLatLngs = [event.latlng];
      if (lassoPreviewLayer) lassoPreviewLayer.remove();
      lassoPreviewLayer = L.polyline(lassoLatLngs, {{
        color: "#0F766E",
        weight: 3,
        opacity: 0.95,
        dashArray: "6 6"
      }}).addTo(lassoLayerGroup);
    }}

    function extendLasso(event) {{
      if (!lassoMode || !lassoDrawing) return;
      const last = lassoLatLngs[lassoLatLngs.length - 1];
      const lastPoint = map.latLngToLayerPoint(last);
      const nextPoint = map.latLngToLayerPoint(event.latlng);
      if (lastPoint.distanceTo(nextPoint) < 8) return;
      lassoLatLngs.push(event.latlng);
      lassoPreviewLayer.setLatLngs(lassoLatLngs);
    }}

    function finishLasso(event) {{
      if (!lassoMode || !lassoDrawing) return;
      lassoDrawing = false;
      suppressNextMapClick = true;
      if (event.latlng) lassoLatLngs.push(event.latlng);
      if (lassoLatLngs.length < 3) {{
        document.getElementById("lassoNote").textContent = "Lasso was too small. Drag a wider loop around a corridor section.";
        return;
      }}
      completePlanningLasso(lassoLatLngs);
      setLassoMode(false);
    }}

    function completePlanningLasso(latLngs) {{
      const polygonLatLngs = latLngs.map(point => L.latLng(point.lat, point.lng));
      lassoPolygons.push(polygonLatLngs);
      L.polygon(polygonLatLngs, {{
        color: "#0F766E",
        weight: 3,
        opacity: 0.95,
        fillColor: "#14B8A6",
        fillOpacity: 0.14
      }}).addTo(lassoLayerGroup);
      updatePlanningScopeMarkers();
      if (plannerMode === "towerCoverage") planTowerCoverage();
    }}

    function clearPlanningLasso() {{
      lassoLayerGroup.clearLayers();
      lassoTowerLayerGroup.clearLayers();
      lassoPolygons = [];
      lassoLatLngs = [];
      scopedTowerIds = new Set();
      setLassoMode(false);
      document.getElementById("lassoNote").textContent = "No lasso active. Tower coverage planning will use all inspectable towers/supports in the selected study area.";
      if (plannerMode === "towerCoverage") planTowerCoverage();
    }}

    function updatePlanningScopeMarkers() {{
      lassoTowerLayerGroup.clearLayers();
      scopedTowerIds = new Set();
      if (lassoPolygons.length === 0) return;
      const scopedPoints = allInspectableTowerPoints().filter(point => pointInsideLasso(point));
      scopedPoints.forEach(point => scopedTowerIds.add(point.id));
      scopedPoints.slice(0, 1200).forEach(point => {{
        L.circleMarker([point.lat, point.lng], {{
          radius: 4,
          color: "#0F766E",
          weight: 2,
          fillColor: "#99F6E4",
          fillOpacity: 0.88,
          opacity: 0.95
        }}).bindTooltip("Inside planning lasso", {{ direction: "top", className: "route-label" }}).addTo(lassoTowerLayerGroup);
      }});
      document.getElementById("lassoNote").textContent = lassoSummaryText();
    }}

    function lassoSummaryText() {{
      if (lassoPolygons.length === 0) return "Optional: draw one or more lasso sections on the map to restrict tower coverage planning to selected corridor areas.";
      return `Planning lasso active: ${{lassoPolygons.length}} section${{lassoPolygons.length === 1 ? "" : "s"}}, ${{scopedTowerIds.size}} tower/support point${{scopedTowerIds.size === 1 ? "" : "s"}} inside scope. Coverage percentage will use this scoped total.`;
    }}

    function pointInsideLasso(point) {{
      if (lassoPolygons.length === 0) return true;
      return lassoPolygons.some(polygon => {{
        const ring = polygon.map(latlng => [latlng.lng, latlng.lat]);
        return pointInRingLonLat([point.lng, point.lat], ring);
      }});
    }}

    function setManualMode(options = {{}}) {{
      plannerMode = "manual";
      if (!options.keepForcedAircraft) forcedAircraftCount = null;
      lastCoverageSummary = null;
      precomputedRoutePlan = null;
      activePlanRunId += 1;
      document.getElementById("planTowers").disabled = false;
      document.getElementById("towerPlanNote").classList.remove("is-working");
      document.getElementById("towerPlanNote").classList.remove("is-warning");
      document.getElementById("towerPlanNote").textContent = "Choose a fleet size and target percentage, then auto-select the lowest-fuel tower/support inspection set for the current study area.";
      unplannedTowerLayerGroup.clearLayers();
    }}

    function getRequestedAircraftCount() {{
      const value = Number(document.getElementById("fleetCount").value || 1);
      return Math.max(1, Math.min(helicopterModel.maxFleetSize, value));
    }}

    function allInspectableTowerPoints() {{
      return (currentArea.structureGeojson.features || [])
        .filter(feature => feature.geometry?.type === "Point")
        .filter(feature => feature.properties?.kind !== "substation")
        .map((feature, index) => ({{
          lat: feature.geometry.coordinates[1],
          lng: feature.geometry.coordinates[0],
          distanceMeters: 0,
          source: "tower_plan",
          towerIndex: index,
          kind: feature.properties?.kind || "support",
          id: `${{feature.properties?.kind || "support"}}:${{feature.properties?.id ?? index}}:${{index}}`
        }}));
    }}

    function inspectableTowerPoints() {{
      const allPoints = allInspectableTowerPoints();
      if (lassoPolygons.length === 0) return allPoints;
      return allPoints.filter(point => scopedTowerIds.has(point.id));
    }}

    async function planTowerCoverage() {{
      const runId = ++activePlanRunId;
      const planButton = document.getElementById("planTowers");
      const note = document.getElementById("towerPlanNote");
      const towers = inspectableTowerPoints();
      const coveragePercent = Number(document.getElementById("coverageTarget").value || 25);
      const aircraftCount = getRequestedAircraftCount();
      const forceFleetUse = document.getElementById("forceFleetUse").checked;
      const targetCount = towers.length === 0 ? 0 : Math.max(1, Math.ceil(towers.length * coveragePercent / 100));
      plannerMode = "towerCoverage";
      forcedAircraftCount = Math.max(1, aircraftCount);
      precomputedRoutePlan = null;
      planButton.disabled = true;
      note.classList.add("is-working");
      note.classList.remove("is-warning");
      unplannedTowerLayerGroup.clearLayers();
      note.textContent = towers.length === 0
        ? "No inspectable tower/support points are loaded for this study area."
        : `Planning ${{targetCount}} of ${{towers.length}} towers/supports with ${{aircraftCount}} helicopter${{aircraftCount === 1 ? "" : "s"}}${{forceFleetUse ? " using all selected aircraft" : " allowing fewer aircraft"}}...`;
      await nextFrame();

      if (towers.length === 0) {{
        selectedPoints = [];
        forcedAircraftCount = null;
        lastCoverageSummary = "No inspectable tower/support points are loaded for this study area.";
        note.classList.remove("is-working");
        note.textContent = lastCoverageSummary;
        planButton.disabled = false;
        updateRoute();
        return;
      }}

      const coveragePlan = await greedyTowerCoveragePlan(towers, targetCount, aircraftCount, forceFleetUse, status => {{
        if (runId !== activePlanRunId) return;
        note.textContent = status;
      }});
      if (runId !== activePlanRunId) return;

      selectedPoints = coveragePlan.points;
      forcedAircraftCount = Math.max(1, Math.min(aircraftCount, selectedPoints.length || 1));
      lastFuelWarning = coveragePlan.feasible ? "" : coveragePlan.message;
      lastCoverageSummary = coveragePlan.message;
      precomputedRoutePlan = coveragePlan.routePlan;
      document.getElementById("multiHeli").checked = forcedAircraftCount > 1;
      note.classList.remove("is-working");
      note.classList.toggle("is-warning", !coveragePlan.feasible);
      note.textContent = coveragePlan.message;
      planButton.disabled = false;
      renderUnplannedTowerMarkers(coveragePlan.unplannedPoints || []);
      updateRoute();
    }}

    async function greedyTowerCoveragePlan(towers, targetCount, aircraftCount, forceFleetUse, onProgress) {{
      const aircraft = Array.from({{ length: Math.max(1, aircraftCount) }}, (_, index) => ({{
        index,
        sorties: 0,
        totalDistanceKm: 0,
        totalFuelPercent: 0
      }}));
      const sorties = [];
      const sortedTowers = towers
        .map((point, towerIndex) => ({{
          point,
          towerIndex,
          originDistanceKm: haversineKm(currentArea.origin.lng, currentArea.origin.lat, point.lng, point.lat)
        }}))
        .sort((a, b) => a.originDistanceKm - b.originDistanceKm);
      const plannedPoints = [];
      const plannedTowerIds = new Set();
      const fuelSkippedPoints = [];
      let skippedForFuel = 0;
      let lastYieldAt = Date.now();

      for (let candidateNumber = 0; candidateNumber < sortedTowers.length; candidateNumber++) {{
        const candidate = sortedTowers[candidateNumber];
        if (plannedPoints.length >= targetCount) break;
        const candidatePointIndex = plannedPoints.length;
        const candidatePoints = plannedPoints.concat(candidate.point);
        let best = null;

        const mustSeedUnusedAircraft = forceFleetUse
          && plannedPoints.length < Math.min(aircraftCount, targetCount)
          && aircraft.some(item => item.sorties === 0);

        if (!mustSeedUnusedAircraft) {{
          for (let sortieIndex = 0; sortieIndex < sorties.length; sortieIndex++) {{
            const sortie = sorties[sortieIndex];
            const insertion = bestInsertionForOrder(sortie.order, candidatePointIndex, candidatePoints, sortie.distanceKm);
            if (!routeFuelFeasible(insertion.distanceKm, insertion.order.length)) continue;
            const currentFuel = missionFuelPercent(sortie.distanceKm, sortie.order.length);
            const nextFuel = missionFuelPercent(insertion.distanceKm, insertion.order.length);
            const increase = nextFuel - currentFuel;
            if (!best || increase < best.increase) {{
              best = {{ type: "insert", sortieIndex, order: insertion.order, distanceKm: insertion.distanceKm, increase }};
            }}
          }}
        }}

        const newSortieDistance = routeDistanceKm(routeForOrder([candidatePointIndex], candidatePoints));
        if (routeFuelFeasible(newSortieDistance, 1)) {{
          const aircraftIndex = chooseAircraftForNewSortie(aircraft, forceFleetUse);
          const newSortieFuel = missionFuelPercent(newSortieDistance, 1);
          if (!best || mustSeedUnusedAircraft || newSortieFuel < best.increase) {{
            best = {{
              type: "new",
              aircraftIndex,
              order: [candidatePointIndex],
              distanceKm: newSortieDistance,
              increase: newSortieFuel
            }};
          }}
        }}

        if (!best) {{
          skippedForFuel += 1;
          fuelSkippedPoints.push(candidate.point);
          continue;
        }}
        plannedPoints.push(candidate.point);
        plannedTowerIds.add(candidate.point.id);
        if (best.type === "insert") {{
          const sortie = sorties[best.sortieIndex];
          const owner = aircraft[sortie.aircraftIndex];
          owner.totalDistanceKm += best.distanceKm - sortie.distanceKm;
          owner.totalFuelPercent += missionFuelPercent(best.distanceKm, best.order.length) - missionFuelPercent(sortie.distanceKm, sortie.order.length);
          sortie.order = best.order;
          sortie.distanceKm = best.distanceKm;
        }} else {{
          const owner = aircraft[best.aircraftIndex];
          owner.sorties += 1;
          owner.totalDistanceKm += best.distanceKm;
          owner.totalFuelPercent += missionFuelPercent(best.distanceKm, best.order.length);
          sorties.push({{
            aircraftIndex: best.aircraftIndex,
            sortieIndex: owner.sorties,
            order: best.order,
            distanceKm: best.distanceKm
          }});
        }}

        if (candidateNumber % 12 === 0 || Date.now() - lastYieldAt > 120) {{
          onProgress(`Planning towers: selected ${{plannedPoints.length}} of ${{targetCount}}; scanned ${{candidateNumber + 1}} of ${{sortedTowers.length}}; sorties ${{sorties.length}}.`);
          lastYieldAt = Date.now();
          await nextFrame();
        }}
      }}

      for (let sortieIndex = 0; sortieIndex < sorties.length; sortieIndex++) {{
        const sortie = sorties[sortieIndex];
        onProgress(`Polishing sortie ${{sortieIndex + 1}} of ${{sorties.length}}...`);
        await nextFrame();
        if (sortie.order.length <= 80) {{
          sortie.order = twoOptImproveClosed(sortie.order, plannedPoints);
          sortie.distanceKm = routeDistanceKm(routeForOrder(sortie.order, plannedPoints));
        }}
      }}
      const plannedCount = plannedPoints.length;
      const feasible = plannedCount >= targetCount;
      const totalDistance = sorties.reduce((sum, sortie) => sum + sortie.distanceKm, 0);
      const activeAircraftCount = new Set(sorties.map(sortie => sortie.aircraftIndex)).size;
      const targetText = `${{targetCount}} of ${{towers.length}} towers/supports (${{Math.round(targetCount / towers.length * 100)}}%)`;
      const plannedText = `${{plannedCount}} of ${{towers.length}} towers/supports (${{Math.round(plannedCount / towers.length * 100)}}%)`;
      const unplannedPoints = towers.filter(point => !plannedTowerIds.has(point.id));
      const message = feasible
        ? `Planned ${{plannedText}} with ${{activeAircraftCount}} active helicopter${{activeAircraftCount === 1 ? "" : "s"}} across ${{sorties.length}} sortie${{sorties.length === 1 ? "" : "s"}}${{forceFleetUse ? " using the selected fleet size" : " chosen by fuel/workload balancing"}}; estimated route distance ${{totalDistance.toFixed(1)}} km including return-to-base legs.`
        : `Fuel-limited coverage: planned ${{plannedText}} toward a target of ${{targetText}} with ${{aircraftCount}} helicopter${{aircraftCount === 1 ? "" : "s"}}. Red markers show ${{unplannedPoints.length}} unplanned towers/supports; ${{skippedForFuel}} candidate stops could not fit within a sortie after travel + hover + reserve fuel.`;
      const routePlan = plannedPoints.length
        ? finalizeCoverageRoutePlan(sorties, plannedPoints)
        : emptyRoutePlan();
      routePlan.feasible = feasible;
      routePlan.message = message;
      return {{ feasible, points: plannedPoints, buckets: sorties, routePlan, message, unplannedPoints, fuelSkippedPoints }};
    }}

    function renderUnplannedTowerMarkers(points) {{
      unplannedTowerLayerGroup.clearLayers();
      if (!points.length) return;
      const maxMarkers = 1200;
      points.slice(0, maxMarkers).forEach(point => {{
        L.circleMarker([point.lat, point.lng], {{
          radius: 4,
          color: "#991B1B",
          weight: 2,
          fillColor: "#FCA5A5",
          fillOpacity: 0.35,
          opacity: 0.95
        }}).bindTooltip("Unplanned by fuel constraint", {{ direction: "top", className: "route-label" }}).addTo(unplannedTowerLayerGroup);
      }});
    }}

    function chooseAircraftForNewSortie(aircraft, forceFleetUse) {{
      if (forceFleetUse) {{
        const unused = aircraft.find(item => item.sorties === 0);
        if (unused) return unused.index;
      }}
      return aircraft
        .slice()
        .sort((a, b) => a.totalFuelPercent - b.totalFuelPercent || a.sorties - b.sorties || a.index - b.index)[0].index;
    }}

    function finalizeCoverageRoutePlan(sorties, points) {{
      const plannedRoutes = sorties
        .slice()
        .sort((a, b) => a.aircraftIndex - b.aircraftIndex || a.sortieIndex - b.sortieIndex)
        .map(sortie => routeObject(sortie.order, points, sortie.aircraftIndex, sortie.sortieIndex));
      return {{
        feasible: true,
        totalDistanceKm: plannedRoutes.reduce((sum, route) => sum + route.distanceKm, 0),
        totalFuelPercent: plannedRoutes.reduce((sum, route) => sum + route.fuelPercent, 0),
        reservePercent: Math.min(...plannedRoutes.map(route => route.reservePercent)),
        aircraftCount: new Set(plannedRoutes.map(route => route.aircraftIndex)).size,
        sortieCount: plannedRoutes.length,
        routes: plannedRoutes,
        message: ""
      }};
    }}

    function nextFrame() {{
      return new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)));
    }}

    function bestInsertionForOrder(order, newIndex, points, currentDistanceKm = null) {{
      let bestOrder = null;
      let bestDistance = Infinity;
      const baseDistance = currentDistanceKm ?? routeDistanceKm(routeForOrder(order, points));
      for (let position = 0; position <= order.length; position++) {{
        const candidateOrder = order.slice(0, position).concat(newIndex, order.slice(position));
        const prev = position === 0 ? {{ lng: currentArea.origin.lng, lat: currentArea.origin.lat }} : points[order[position - 1]];
        const next = position === order.length ? {{ lng: currentArea.origin.lng, lat: currentArea.origin.lat }} : points[order[position]];
        const added = haversineKm(prev.lng, prev.lat, points[newIndex].lng, points[newIndex].lat)
          + haversineKm(points[newIndex].lng, points[newIndex].lat, next.lng, next.lat)
          - haversineKm(prev.lng, prev.lat, next.lng, next.lat);
        const distance = baseDistance + added;
        if (distance < bestDistance) {{
          bestDistance = distance;
          bestOrder = candidateOrder;
        }}
      }}
      return {{ order: bestOrder || [newIndex], distanceKm: bestDistance }};
    }}

    function resetDefault() {{
      selectedPoints = [{{ lat: currentArea.defaultDestination[0], lng: currentArea.defaultDestination[1], distanceMeters: 0 }}];
      lastFuelWarning = "";
      setManualMode();
      updateRoute();
    }}

    function addSelectedPoint(point) {{
      setManualMode();
      const duplicate = selectedPoints.some(existing => haversineKm(existing.lng, existing.lat, point.lng, point.lat) < 0.08);
      if (duplicate) return;
      const candidatePoints = selectedPoints.concat({{ lat: point.lat, lng: point.lng, distanceMeters: point.distanceMeters }});
      const candidatePlan = buildRoutePlan(candidatePoints);
      if (!candidatePlan.feasible) {{
        lastFuelWarning = candidatePlan.message;
        renderFuelStats(candidatePlan);
        return;
      }}
      selectedPoints = candidatePoints;
      lastFuelWarning = "";
      updateRoute();
    }}

    function removeSelectedPoint(index) {{
      setManualMode();
      selectedPoints.splice(index, 1);
      lastFuelWarning = "";
      updateRoute();
    }}

    function updateRoute() {{
      for (const marker of stopMarkers) marker.remove();
      stopMarkers = [];
      routeLayerGroup.clearLayers();
      if (plannerMode !== "towerCoverage") unplannedTowerLayerGroup.clearLayers();
      if (selectedPoints.length === 0) {{
        document.getElementById("distance").textContent = "0.0 km";
        document.getElementById("bearing").textContent = "--";
        document.getElementById("stopCount").textContent = "0 stops";
        document.getElementById("coords").textContent = "No stops selected.";
        renderRouteColorLegend(null);
        renderAirspaceFlags([]);
        renderFuelStats(emptyRoutePlan());
        return;
      }}
      const origin = [currentArea.origin.lat, currentArea.origin.lng];
      const plan = precomputedRoutePlan || buildRoutePlan(selectedPoints);
      drawRoutePlan(plan);
      const firstStop = plan.routes[0]?.orderedStops[0] || selectedPoints[0];
      const bearing = initialBearing(origin[1], origin[0], firstStop.lng, firstStop.lat);
      document.getElementById("distance").textContent = `${{plan.totalDistanceKm.toFixed(1)}} km`;
      document.getElementById("bearing").textContent = `${{Math.round(bearing).toString().padStart(3, "0")}}° ${{compassLabel(bearing)}}`;
      document.getElementById("stopCount").textContent = `${{selectedPoints.length}} ${{selectedPoints.length === 1 ? "stop" : "stops"}}`;
      document.getElementById("coords").textContent = routePlanText(plan);
      renderRouteColorLegend(plan);
      renderFuelStats(plan);
      updateAirspaceFlags(plan.routes.flatMap(route => route.latLngs));
    }}

    function buildRoutePlan(points) {{
      const multiEnabled = document.getElementById("multiHeli").checked;
      const singlePlan = singleAircraftPlan(points);
      const requestedAircraft = Math.max(1, Math.min(getRequestedAircraftCount(), points.length || 1));
      const shouldUseFleet = forcedAircraftCount || multiEnabled;
      if (!shouldUseFleet || points.length < 2 || requestedAircraft < 2) {{
        if (!singlePlan.feasible) {{
          singlePlan.message = singlePlan.message || "Single-helicopter route would not preserve the fuel reserve needed to return home.";
        }}
        return singlePlan;
      }}
      const aircraftCount = Math.max(1, Math.min(forcedAircraftCount || requestedAircraft, points.length));
      const fleetPlan = points.length <= 10 ? exactFleetPlan(points, aircraftCount, aircraftCount) : greedyFleetPlan(points, aircraftCount, aircraftCount);
      if (!fleetPlan.feasible) {{
        fleetPlan.message = `No feasible plan found with ${{aircraftCount}} ${{helicopterModel.name}} helicopters while accounting for ${{helicopterModel.hoverMinutesPerStop}} minutes hover per stop and preserving a ${{helicopterModel.reserveMinutes}} minute reserve.`;
      }} else if (plannerMode === "towerCoverage" && lastCoverageSummary) {{
        fleetPlan.message = lastCoverageSummary;
      }} else if (singlePlan.feasible) {{
        fleetPlan.message = `Fleet mode: assigned stops across ${{fleetPlan.aircraftCount}} helicopters so each flight path is shown separately by color. Single-helicopter fuel check is also feasible at ${{singlePlan.totalDistanceKm.toFixed(1)}} km round trip.`;
      }}
      return fleetPlan;
    }}

    function emptyRoutePlan() {{
      return {{
        feasible: true,
        totalDistanceKm: 0,
        totalFuelPercent: 0,
        reservePercent: 100,
        aircraftCount: 0,
        sortieCount: 0,
        routes: [],
        message: lastFuelWarning
      }};
    }}

    function singleAircraftPlan(points) {{
      const indices = points.map((_, index) => index);
      const bestRoute = bestClosedRouteForIndices(indices, points);
      const distance = bestRoute.distanceKm;
      const fuelPercent = missionFuelPercent(distance, indices.length);
      const feasible = fuelPercent <= usableFuelPercent;
      return {{
        feasible,
        totalDistanceKm: distance,
        totalFuelPercent: fuelPercent,
        reservePercent: Math.max(0, 100 - fuelPercent),
        aircraftCount: 1,
        sortieCount: 1,
        routes: [routeObject(bestRoute.order, points, 0)],
        message: feasible ? "" : `Adding that stop would require ${{fuelPercent.toFixed(1)}}% fuel before reserve (${{distance.toFixed(1)}} km travel plus ${{indices.length * helicopterModel.hoverMinutesPerStop}} min hover). Limit before reserve is ${{usableFuelPercent.toFixed(1)}}%.`
      }};
    }}

    function exactFleetPlan(points, minAircraft = 1, maxAircraft = helicopterModel.maxFleetSize) {{
      const n = points.length;
      const fullMask = (1 << n) - 1;
      const subsetPlans = new Map();
      for (let mask = 1; mask <= fullMask; mask++) {{
        const indices = [];
        for (let i = 0; i < n; i++) if (mask & (1 << i)) indices.push(i);
        const route = bestClosedRouteForIndices(indices, points);
        if (routeFuelFeasible(route.distanceKm, indices.length)) subsetPlans.set(mask, route);
      }}
      const dp = Array(fullMask + 1).fill(null).map(() => Array(maxAircraft + 1).fill(null));
      dp[0][0] = {{ distanceKm: 0, routes: [] }};
      for (let mask = 0; mask <= fullMask; mask++) {{
        for (let count = 0; count < maxAircraft; count++) {{
          if (!dp[mask][count]) continue;
          const remaining = fullMask ^ mask;
          for (let subset = remaining; subset; subset = (subset - 1) & remaining) {{
            const route = subsetPlans.get(subset);
            if (!route) continue;
            const nextMask = mask | subset;
            const nextCount = count + 1;
            const candidate = {{
              distanceKm: dp[mask][count].distanceKm + route.distanceKm,
              routes: dp[mask][count].routes.concat(route)
            }};
            if (!dp[nextMask][nextCount] || candidate.distanceKm + 0.001 < dp[nextMask][nextCount].distanceKm) dp[nextMask][nextCount] = candidate;
          }}
        }}
      }}
      let best = null;
      for (let count = minAircraft; count <= maxAircraft; count++) {{
        const candidate = dp[fullMask][count];
        if (candidate && (!best || candidate.distanceKm + 0.001 < best.distanceKm)) best = candidate;
      }}
      if (!best) return {{ feasible: false, totalDistanceKm: 0, totalFuelPercent: 0, reservePercent: 0, aircraftCount: 0, routes: [] }};
      return finalizeFleetPlan(best.routes, points);
    }}

    function greedyFleetPlan(points, minAircraft = 1, maxAircraft = helicopterModel.maxFleetSize) {{
      const routeBuckets = [];
      const sorted = points.map((point, index) => ({{ point, index, distanceKm: haversineKm(currentArea.origin.lng, currentArea.origin.lat, point.lng, point.lat) }}))
        .sort((a, b) => b.distanceKm - a.distanceKm);
      for (const item of sorted) {{
        let bestBucket = null;
        let bestRoute = null;
        let bestIncrease = Infinity;
        const shouldSeedFleet = routeBuckets.length < minAircraft && routeBuckets.length < sorted.length;
        for (const bucket of shouldSeedFleet ? [] : routeBuckets) {{
          const candidateIndices = bucket.indices.concat(item.index);
          const candidateRoute = bestClosedRouteForIndices(candidateIndices, points);
          const increase = candidateRoute.distanceKm - bucket.route.distanceKm;
          if (routeFuelFeasible(candidateRoute.distanceKm, candidateIndices.length) && increase < bestIncrease) {{
            bestBucket = bucket;
            bestRoute = candidateRoute;
            bestIncrease = increase;
          }}
        }}
        if (bestBucket) {{
          bestBucket.indices.push(item.index);
          bestBucket.route = bestRoute;
        }} else {{
          const route = bestClosedRouteForIndices([item.index], points);
          if (!routeFuelFeasible(route.distanceKm, 1) || routeBuckets.length >= maxAircraft) {{
            return {{ feasible: false, totalDistanceKm: 0, totalFuelPercent: 0, reservePercent: 0, aircraftCount: 0, routes: [] }};
          }}
          routeBuckets.push({{ indices: [item.index], route }});
        }}
      }}
      return finalizeFleetPlan(routeBuckets.map(bucket => bucket.route), points);
    }}

    function finalizeFleetPlan(routes, points) {{
      const totalDistance = routes.reduce((sum, route) => sum + route.distanceKm, 0);
      const plannedRoutes = routes
        .sort((a, b) => a.distanceKm - b.distanceKm)
        .map((route, index) => routeObject(route.order, points, index));
      return {{
        feasible: true,
        totalDistanceKm: totalDistance,
        totalFuelPercent: plannedRoutes.reduce((sum, route) => sum + route.fuelPercent, 0),
        reservePercent: Math.min(...plannedRoutes.map(route => route.reservePercent)),
        aircraftCount: plannedRoutes.length,
        sortieCount: plannedRoutes.length,
        routes: plannedRoutes,
        message: plannedRoutes.length > 1 ? `Split into ${{plannedRoutes.length}} aircraft; each colored route returns to ${{currentArea.origin.label}} with reserve fuel preserved.` : ""
      }};
    }}

    function bestClosedRouteForIndices(indices, points) {{
      if (indices.length <= 1) {{
        const order = indices.slice();
        return {{ order, distanceKm: routeDistanceKm(routeForOrder(order, points)) }};
      }}
      if (indices.length <= 8) {{
        let bestOrder = indices.slice();
        let bestDistance = Infinity;
        function walk(prefix, unused) {{
          if (unused.length === 0) {{
            const distance = routeDistanceKm(routeForOrder(prefix, points));
            if (distance < bestDistance) {{
              bestDistance = distance;
              bestOrder = prefix.slice();
            }}
            return;
          }}
          for (let i = 0; i < unused.length; i++) {{
            const next = unused[i];
            walk(prefix.concat(next), unused.slice(0, i).concat(unused.slice(i + 1)));
          }}
        }}
        walk([], indices);
        return {{ order: bestOrder, distanceKm: bestDistance }};
      }}
      const order = twoOptImproveClosed(nearestNeighborRouteForIndices(indices, points), points);
      return {{ order, distanceKm: routeDistanceKm(routeForOrder(order, points)) }};
    }}

    function routeForOrder(order, points) {{
      const origin = [currentArea.origin.lat, currentArea.origin.lng];
      return [origin, ...order.map(index => [points[index].lat, points[index].lng]), origin];
    }}

    function routeObject(order, points, aircraftIndex, sortieIndex = 1) {{
      const latLngs = routeForOrder(order, points);
      const distanceKm = routeDistanceKm(latLngs);
      const hoverMinutes = order.length * helicopterModel.hoverMinutesPerStop;
      const travelFuelPercent = fuelPercentForDistance(distanceKm);
      const hoverFuelPercent = hoverFuelPercentForStops(order.length);
      const fuelPercent = travelFuelPercent + hoverFuelPercent;
      return {{
        aircraftIndex,
        sortieIndex,
        order,
        orderedStops: order.map(index => points[index]),
        distanceKm,
        hoverMinutes,
        travelFuelPercent,
        hoverFuelPercent,
        fuelPercent,
        reservePercent: Math.max(0, 100 - fuelPercent),
        latLngs
      }};
    }}

    function drawRoutePlan(plan) {{
      const labelCounters = {{}};
      for (const route of plan.routes) {{
        const colors = routePalette[route.aircraftIndex % routePalette.length];
        L.polyline(route.latLngs, {{ color: colors.halo, weight: 8, opacity: 0.92 }}).addTo(routeLayerGroup);
        L.polyline(route.latLngs, {{ color: colors.fill, weight: 4, opacity: 1 }}).addTo(routeLayerGroup);
        route.orderedStops.forEach((point, sequenceIndex) => {{
          const originalIndex = route.order[sequenceIndex];
          const aircraftLetter = String.fromCharCode(65 + route.aircraftIndex);
          labelCounters[route.aircraftIndex] = (labelCounters[route.aircraftIndex] || 0) + 1;
          const label = plan.routes.length > 1 ? `${{aircraftLetter}}${{labelCounters[route.aircraftIndex]}}` : `${{sequenceIndex + 1}}`;
          const marker = L.marker([point.lat, point.lng], {{
            icon: L.divIcon({{
              className: "",
              html: `<div class="stop-label" style="--stop-fill: ${{colors.fill}}; --stop-halo: ${{colors.halo}};">${{label}}</div>`,
              iconSize: [24, 24],
              iconAnchor: [12, 12]
            }})
          }}).addTo(map);
          marker.on("click", () => removeSelectedPoint(originalIndex));
          marker.bindTooltip("Click to remove stop", {{ direction: "right", className: "route-label" }});
          stopMarkers.push(marker);
        }});
      }}
    }}

    function renderRouteColorLegend(plan) {{
      const container = document.getElementById("routeColorLegend");
      if (!plan || !plan.routes.length) {{
        container.innerHTML = "";
        return;
      }}
      const groups = new Map();
      for (const route of plan.routes) {{
        const current = groups.get(route.aircraftIndex) || {{
          aircraftIndex: route.aircraftIndex,
          stops: 0,
          sorties: 0,
          distanceKm: 0,
          hoverMinutes: 0
        }};
        current.stops += route.orderedStops.length;
        current.sorties += 1;
        current.distanceKm += route.distanceKm;
        current.hoverMinutes += route.hoverMinutes || 0;
        groups.set(route.aircraftIndex, current);
      }}
      container.innerHTML = Array.from(groups.values())
        .sort((a, b) => a.aircraftIndex - b.aircraftIndex)
        .map(group => {{
          const colors = routePalette[group.aircraftIndex % routePalette.length];
          const helicopterName = `Helicopter ${{group.aircraftIndex + 1}}`;
          const sortieText = group.sorties === 1 ? "1 sortie" : `${{group.sorties}} sorties`;
          const stopText = group.stops === 1 ? "1 stop" : `${{group.stops}} stops`;
          return `
            <div class="route-color-row">
              <span class="route-color-swatch" style="--route-fill: ${{colors.fill}}; --route-halo: ${{colors.halo}};"></span>
              <span><strong>${{helicopterName}}</strong>${{stopText}}; ${{sortieText}}; ${{group.distanceKm.toFixed(1)}} km; ${{group.hoverMinutes}} min hover</span>
            </div>
          `;
        }})
        .join("");
    }}

    function routePlanText(plan) {{
      if (plan.routes.length === 0) return "No stops selected.";
      return plan.routes.map(route => {{
        const hasMultipleSorties = (plan.sortieCount || plan.routes.length) > plan.aircraftCount;
        const prefix = plan.routes.length > 1
          ? `H${{route.aircraftIndex + 1}}${{hasMultipleSorties ? ` S${{route.sortieIndex}}` : ""}} `
          : "";
        const displayLimit = plannerMode === "towerCoverage" ? 12 : 40;
        const stops = route.orderedStops.slice(0, displayLimit)
          .map((point, index) => `${{index + 1}}. ${{point.lat.toFixed(5)}}, ${{point.lng.toFixed(5)}}`)
          .join("\\n");
        const remaining = route.orderedStops.length - displayLimit;
        const tail = remaining > 0 ? `\\n... ${{remaining}} more stops` : "";
        return `${{prefix}}${{route.distanceKm.toFixed(1)}} km round trip; ${{route.orderedStops.length}} stop${{route.orderedStops.length === 1 ? "" : "s"}}; ${{route.hoverMinutes}} min hover\\n${{stops}}${{tail}}`;
      }}).join("\\n\\n");
    }}

    function renderFuelStats(plan) {{
      const list = document.getElementById("fuelStats");
      const efficiency = 100 / helicopterModel.rangeKm;
      const totalHoverMinutes = plan.routes.reduce((sum, route) => sum + (route.hoverMinutes || 0), 0);
      const totalTravelFuel = plan.routes.reduce((sum, route) => sum + (route.travelFuelPercent || 0), 0);
      const totalHoverFuel = plan.routes.reduce((sum, route) => sum + (route.hoverFuelPercent || 0), 0);
      const statusClass = plan.feasible ? "good" : "warn";
      const warning = lastFuelWarning || plan.message || (plan.feasible ? "Route preserves reserve fuel for return-to-base." : "Route is not feasible under this fuel model.");
      const aircraftText = plan.aircraftCount === 1 ? "1 helicopter" : `${{plan.aircraftCount}} helicopters`;
      const sortieText = plan.sortieCount && plan.sortieCount > plan.aircraftCount ? ` / ${{plan.sortieCount}} sorties` : "";
      list.innerHTML = `
        <li><strong>${{helicopterModel.name}} fuel model</strong>${{helicopterModel.rangeKm}} km standard-fuel range; ${{helicopterModel.reserveMinutes}} min reserve equals ${{reserveRangeKm.toFixed(1)}} km / ${{reserveFuelPercent.toFixed(1)}}% fuel. Travel efficiency proxy: ${{efficiency.toFixed(3)}}% of standard fuel per km.</li>
        <li><strong>Hover allowance</strong>${{helicopterModel.hoverMinutesPerStop}} minutes per stop at ${{helicopterModel.hoverFuelMultiplier.toFixed(2)}}x endurance burn; current plan includes ${{totalHoverMinutes}} hover minutes.</li>
        <li class="${{statusClass}}"><strong>${{plan.totalDistanceKm.toFixed(1)}} km including return home</strong>${{plan.feasible ? aircraftText + sortieText : "Blocked"}}; estimated fuel ${{plan.totalFuelPercent.toFixed(1)}}% tank-equivalent (${{totalTravelFuel.toFixed(1)}}% travel + ${{totalHoverFuel.toFixed(1)}}% hover). Smallest aircraft reserve left: ${{plan.reservePercent.toFixed(1)}}%.</li>
        <li><strong>${{plan.feasible ? "Fuel status" : "Selection blocked"}}</strong>${{warning}}</li>
      `;
    }}

    function fuelPercentForDistance(distanceKm) {{
      return distanceKm / helicopterModel.rangeKm * 100;
    }}

    function hoverFuelPercentForStops(stopCount) {{
      const hoverHours = stopCount * helicopterModel.hoverMinutesPerStop / 60;
      return hoverHours / helicopterModel.enduranceHours * 100 * helicopterModel.hoverFuelMultiplier;
    }}

    function missionFuelPercent(distanceKm, stopCount) {{
      return fuelPercentForDistance(distanceKm) + hoverFuelPercentForStops(stopCount);
    }}

    function routeFuelFeasible(distanceKm, stopCount) {{
      return missionFuelPercent(distanceKm, stopCount) <= usableFuelPercent;
    }}

    function updateAirspaceFlags(route) {{
      renderAirspaceFlags(currentArea.airspaceAreas.filter(area => routeIntersectsArea(route, area)));
    }}

    function renderAirspaceFlags(areas) {{
      const list = document.getElementById("airspaceFlags");
      if (areas.length === 0) {{
        list.innerHTML = "<li><strong>No reference intersections</strong>The current route does not cross the simplified DAH reference rings shown on this map.</li>";
        return;
      }}
      list.innerHTML = areas.map(area => `
        <li>
          <strong>${{area.name}}</strong>
          ${{area.classLabel}}; ${{area.altitude}}. ${{area.source}}. Reference only; verify exact sector geometry, altitude, ATC requirements, and NOTAMs before operational planning.
        </li>
      `).join("");
    }}

    function routeIntersectsArea(route, area) {{
      for (let i = 0; i < route.length - 1; i++) {{
        if (pointInsideArea(route[i], area) || pointInsideArea(route[i + 1], area)) return true;
        if (segmentDistanceToAreaCenterMeters(route[i], route[i + 1], area) <= area.radiusNm * 1852) return true;
      }}
      return false;
    }}

    function pointInsideArea(point, area) {{
      return haversineKm(point[1], point[0], area.center[1], area.center[0]) <= area.radiusNm * 1.852;
    }}

    function segmentDistanceToAreaCenterMeters(a, b, area) {{
      const ap = localMetersFromAreaCenter(a, area);
      const bp = localMetersFromAreaCenter(b, area);
      const dx = bp.x - ap.x;
      const dy = bp.y - ap.y;
      const denom = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, -(ap.x * dx + ap.y * dy) / denom));
      const x = ap.x + t * dx;
      const y = ap.y + t * dy;
      return Math.hypot(x, y);
    }}

    function localMetersFromAreaCenter(point, area) {{
      const latScale = 110540;
      const lngScale = 111320 * Math.cos(area.center[0] * Math.PI / 180);
      return {{ x: (point[1] - area.center[1]) * lngScale, y: (point[0] - area.center[0]) * latScale }};
    }}

    function optimizeStopOrder(points) {{
      if (points.length <= 1) return points.map((_, index) => index);
      if (points.length <= 8) return exactShortestOpenRoute(points);
      return twoOptImprove(nearestNeighborRoute(points), points);
    }}

    function exactShortestOpenRoute(points) {{
      const remaining = points.map((_, index) => index);
      let bestOrder = remaining.slice();
      let bestDistance = Infinity;
      function walk(prefix, unused) {{
        if (unused.length === 0) {{
          const distance = orderedDistanceKm(prefix, points);
          if (distance < bestDistance) {{
            bestDistance = distance;
            bestOrder = prefix.slice();
          }}
          return;
        }}
        for (let i = 0; i < unused.length; i++) {{
          const next = unused[i];
          walk(prefix.concat(next), unused.slice(0, i).concat(unused.slice(i + 1)));
        }}
      }}
      walk([], remaining);
      return bestOrder;
    }}

    function nearestNeighborRoute(points) {{
      const unused = new Set(points.map((_, index) => index));
      const order = [];
      let current = {{ lng: currentArea.origin.lng, lat: currentArea.origin.lat }};
      while (unused.size) {{
        let bestIndex = null;
        let bestDistance = Infinity;
        for (const index of unused) {{
          const candidate = points[index];
          const distance = haversineKm(current.lng, current.lat, candidate.lng, candidate.lat);
          if (distance < bestDistance) {{
            bestDistance = distance;
            bestIndex = index;
          }}
        }}
        order.push(bestIndex);
        unused.delete(bestIndex);
        current = points[bestIndex];
      }}
      return order;
    }}

    function nearestNeighborRouteForIndices(indices, points) {{
      const unused = new Set(indices);
      const order = [];
      let current = {{ lng: currentArea.origin.lng, lat: currentArea.origin.lat }};
      while (unused.size) {{
        let bestIndex = null;
        let bestDistance = Infinity;
        for (const index of unused) {{
          const candidate = points[index];
          const distance = haversineKm(current.lng, current.lat, candidate.lng, candidate.lat);
          if (distance < bestDistance) {{
            bestDistance = distance;
            bestIndex = index;
          }}
        }}
        order.push(bestIndex);
        unused.delete(bestIndex);
        current = points[bestIndex];
      }}
      return order;
    }}

    function twoOptImprove(order, points) {{
      let improved = true;
      let best = order.slice();
      let bestDistance = orderedDistanceKm(best, points);
      while (improved) {{
        improved = false;
        for (let i = 0; i < best.length - 1; i++) {{
          for (let k = i + 1; k < best.length; k++) {{
            const candidate = best.slice(0, i).concat(best.slice(i, k + 1).reverse(), best.slice(k + 1));
            const distance = orderedDistanceKm(candidate, points);
            if (distance + 0.001 < bestDistance) {{
              best = candidate;
              bestDistance = distance;
              improved = true;
            }}
          }}
        }}
      }}
      return best;
    }}

    function twoOptImproveClosed(order, points) {{
      let improved = true;
      let best = order.slice();
      let bestDistance = routeDistanceKm(routeForOrder(best, points));
      while (improved) {{
        improved = false;
        for (let i = 0; i < best.length - 1; i++) {{
          for (let k = i + 1; k < best.length; k++) {{
            const candidate = best.slice(0, i).concat(best.slice(i, k + 1).reverse(), best.slice(k + 1));
            const distance = routeDistanceKm(routeForOrder(candidate, points));
            if (distance + 0.001 < bestDistance) {{
              best = candidate;
              bestDistance = distance;
              improved = true;
            }}
          }}
        }}
      }}
      return best;
    }}

    function orderedDistanceKm(order, points) {{
      const origin = [currentArea.origin.lat, currentArea.origin.lng];
      return routeDistanceKm([origin, ...order.map(index => [points[index].lat, points[index].lng])]);
    }}

    function routeDistanceKm(route) {{
      let total = 0;
      for (let i = 0; i < route.length - 1; i++) {{
        total += haversineKm(route[i][1], route[i][0], route[i + 1][1], route[i + 1][0]);
      }}
      return total;
    }}

    function snapToNetwork(latlng) {{
      const p = mercator(latlng.lng, latlng.lat);
      let best = {{ distanceMeters: Infinity, lat: latlng.lat, lng: latlng.lng }};
      for (const feature of currentArea.lineGeojson.features) {{
        const coords = feature.geometry.coordinates;
        for (let i = 0; i < coords.length - 1; i++) {{
          const a = mercator(coords[i][0], coords[i][1]);
          const b = mercator(coords[i + 1][0], coords[i + 1][1]);
          const snapped = nearestOnSegment(p, a, b);
          if (snapped.distanceMeters < best.distanceMeters) {{
            const lonLat = inverseMercator(snapped.x, snapped.y);
            best = {{ distanceMeters: snapped.distanceMeters, lat: lonLat.lat, lng: lonLat.lng }};
          }}
        }}
      }}
      return best;
    }}

    function mercator(lng, lat) {{
      const radius = 6378137;
      const clampedLat = Math.max(Math.min(lat, 85.05112878), -85.05112878);
      return {{ x: radius * lng * Math.PI / 180, y: radius * Math.log(Math.tan(Math.PI / 4 + clampedLat * Math.PI / 360)) }};
    }}

    function inverseMercator(x, y) {{
      const radius = 6378137;
      const lng = x / radius * 180 / Math.PI;
      const lat = (2 * Math.atan(Math.exp(y / radius)) - Math.PI / 2) * 180 / Math.PI;
      return {{ lng, lat }};
    }}

    function nearestOnSegment(p, a, b) {{
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const denom = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / denom));
      const x = a.x + t * dx;
      const y = a.y + t * dy;
      return {{ x, y, distanceMeters: Math.hypot(p.x - x, p.y - y) }};
    }}

    function haversineKm(lon1, lat1, lon2, lat2) {{
      const toRad = degrees => degrees * Math.PI / 180;
      const dlon = toRad(lon2 - lon1);
      const dlat = toRad(lat2 - lat1);
      const phi1 = toRad(lat1);
      const phi2 = toRad(lat2);
      const a = Math.sin(dlat / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlon / 2) ** 2;
      return 6371.0088 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }}

    function initialBearing(lon1, lat1, lon2, lat2) {{
      const toRad = degrees => degrees * Math.PI / 180;
      const y = Math.sin(toRad(lon2 - lon1)) * Math.cos(toRad(lat2));
      const x = Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) - Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(toRad(lon2 - lon1));
      return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }}

    function compassLabel(bearing) {{
      const labels = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
      return labels[Math.floor((bearing + 11.25) / 22.5) % 16];
    }}
  </script>
</body>
</html>
"""
    INTERACTIVE_HTML_PATH.write_text(html, encoding="utf-8")


def main() -> None:
    ensure_data()
    with sqlite3.connect(GPKG_PATH) as conn:
        lines = read_layer(conn, LINE_TABLE, "cable")
        structures = read_layer(conn, STRUCTURE_TABLE, "structure")
        extent = read_extent(conn)
    metrics = render_map(lines, structures, extent)
    write_html(metrics)
    flight_metrics = render_flight_path_map(lines, structures, extent)
    write_flight_html(flight_metrics)
    write_interactive_picker_html(lines, structures)

    print(f"Wrote {PNG_PATH}")
    print(f"Wrote {HTML_PATH}")
    print(f"Wrote {FLIGHT_PNG_PATH}")
    print(f"Wrote {FLIGHT_HTML_PATH}")
    print(f"Wrote {INTERACTIVE_HTML_PATH}")
    print(
        "Metrics: "
        f"{metrics['line_features']:,.0f} cable features, "
        f"{metrics['structure_features']:,.0f} structures, "
        f"{metrics['cable_km']:,.1f} km of cable"
    )
    print(
        "Flight path: "
        f"{flight_metrics['route_km']:,.1f} km, "
        f"bearing {flight_metrics['bearing']:03.0f}° {flight_metrics['bearing_label']}, "
        f"destination {flight_metrics['destination_lat']:.5f}, {flight_metrics['destination_lon']:.5f}"
    )


if __name__ == "__main__":
    main()
