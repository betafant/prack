"""Region definitions.

A region is described by a small TOML file (see ``prack/regions/ch.toml``):
bounding box, time zone, map layers and the weather sample points that are
stored every day. Adding a new region means dropping another file next to it
(or into ``PRACK_REGIONS_DIR``) and listing its id in ``PRACK_REGIONS``.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

BUILTIN_DIR = Path(__file__).parent / "regions"

# Basemaps available everywhere. Regions can add their own (listed first).
GLOBAL_BASEMAPS: list[dict] = [
    {
        "id": "relief",
        "name": "Relief",
        "type": "relief",
        "attribution": "Terrain: Mapzen/AWS Terrain Tiles",
    },
    {
        "id": "topo",
        "name": "OpenTopoMap",
        "type": "raster",
        "tiles": [
            "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
            "https://b.tile.opentopomap.org/{z}/{x}/{y}.png",
            "https://c.tile.opentopomap.org/{z}/{x}/{y}.png",
        ],
        "max_zoom": 17,
        "attribution": "© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)",
        "paint": {"raster-brightness-max": 0.85, "raster-saturation": -0.25},
    },
    {
        "id": "osm",
        "name": "OpenStreetMap",
        "type": "raster",
        "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        "max_zoom": 19,
        "attribution": "© OpenStreetMap contributors",
        "paint": {"raster-brightness-max": 0.85, "raster-saturation": -0.3},
    },
    {
        "id": "satellite",
        "name": "Satellite",
        "type": "raster",
        "tiles": [
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ],
        "max_zoom": 18,
        "attribution": "Imagery © Esri, Maxar, Earthstar Geographics",
    },
]


@dataclass(frozen=True)
class WeatherPoint:
    id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Gradient:
    """Pressure difference ``a - b`` between two weather points (e.g. the föhn index)."""

    id: str
    name: str
    a: str
    b: str
    threshold: float = 4.0
    positive_label: str = ""
    negative_label: str = ""


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    timezone: str
    bbox: tuple[float, float, float, float]  # west, south, east, north
    center: tuple[float, float]  # lon, lat
    zoom: float = 7.0
    default_basemap: str = "relief"
    weather_model: str = ""
    basemaps: tuple[dict, ...] = ()
    weather_points: tuple[WeatherPoint, ...] = ()
    gradients: tuple[Gradient, ...] = ()
    tz: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tz", ZoneInfo(self.timezone))

    def contains(self, lat: float, lon: float, margin_km: float = 0.0) -> bool:
        west, south, east, north = self.expanded_bbox(margin_km)
        return south <= lat <= north and west <= lon <= east

    def expanded_bbox(self, margin_km: float) -> tuple[float, float, float, float]:
        west, south, east, north = self.bbox
        if margin_km <= 0:
            return self.bbox
        dlat = margin_km / 111.2
        dlon = margin_km / (111.2 * max(0.1, math.cos(math.radians((south + north) / 2))))
        return (west - dlon, south - dlat, east + dlon, north + dlat)

    def aprs_filter(self, margin_km: float = 0.0) -> str:
        """APRS-IS area filter ``a/latN/lonW/latS/lonE``."""
        west, south, east, north = self.expanded_bbox(margin_km)
        return f"a/{north:.3f}/{west:.3f}/{south:.3f}/{east:.3f}"

    def all_basemaps(self) -> list[dict]:
        own_ids = {b["id"] for b in self.basemaps}
        return list(self.basemaps) + [b for b in GLOBAL_BASEMAPS if b["id"] not in own_ids]

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "timezone": self.timezone,
            "bbox": list(self.bbox),
            "center": list(self.center),
            "zoom": self.zoom,
            "default_basemap": self.default_basemap,
            "basemaps": self.all_basemaps(),
            "weather_points": [p.__dict__ for p in self.weather_points],
        }


def _load_file(path: Path) -> Region:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Region(
        id=data["id"],
        name=data.get("name", data["id"]),
        timezone=data.get("timezone", "UTC"),
        bbox=tuple(float(v) for v in data["bbox"]),
        center=tuple(float(v) for v in data.get("center", [0, 0])),
        zoom=float(data.get("zoom", 7)),
        default_basemap=data.get("default_basemap", "relief"),
        weather_model=data.get("weather_model", ""),
        basemaps=tuple({"type": "raster", **b} for b in data.get("basemaps", [])),
        weather_points=tuple(WeatherPoint(**p) for p in data.get("weather_points", [])),
        gradients=tuple(Gradient(**g) for g in data.get("gradients", [])),
    )


def available_region_files(extra_dir: Path | None = None) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for directory in (BUILTIN_DIR, extra_dir):
        if directory and directory.is_dir():
            for path in sorted(directory.glob("*.toml")):
                files[path.stem] = path
    return files


def load_regions(ids: list[str], extra_dir: Path | None = None) -> list[Region]:
    files = available_region_files(extra_dir)
    regions = []
    for region_id in ids:
        if region_id not in files:
            raise ValueError(f"Unknown region '{region_id}'. Available: {', '.join(sorted(files))}")
        regions.append(_load_file(files[region_id]))
    if not regions:
        raise ValueError("At least one region must be configured (PRACK_REGIONS)")
    return regions


def region_for(regions: list[Region], lat: float, lon: float, margin_km: float = 0.0) -> Region | None:
    for region in regions:
        if region.contains(lat, lon, margin_km):
            return region
    return None
