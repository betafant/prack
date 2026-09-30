"""Runtime configuration, read from environment variables (prefix ``PRACK_``).

A ``.env`` file in the working directory is loaded first (existing environment
variables win), so the same configuration works on a laptop, in Docker and on
a VPS.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TRACKED_TYPES = (1, 6, 7)  # glider, hang glider, paraglider


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"PRACK_{name}")
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _list(name: str, default: list[str]) -> list[str]:
    value = _env(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
    data_dir: Path = Path("data")
    database_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # Regions to track (ids of region files) and an optional folder with extra region files.
    regions: list[str] = field(default_factory=lambda: ["ch"])
    regions_dir: Path | None = None

    # OGN APRS feed
    ogn_enabled: bool = True
    ogn_host: str = "aprs.glidernet.org"
    ogn_port: int = 14580
    ogn_callsign: str = ""
    ogn_filter_margin_km: float = 30.0

    # Demo mode replaces the OGN feed with simulated aircraft over the region.
    demo: bool = False

    # What is tracked and how flights are cut
    tracked_types: tuple[int, ...] = DEFAULT_TRACKED_TYPES
    respect_stealth: bool = True
    min_fix_interval: float = 2.0  # seconds between stored fixes per aircraft
    flight_gap_minutes: float = 20.0  # silence that ends a flight
    flight_resume_minutes: float = 90.0  # airborne again within this after a gap: same flight
    landing_minutes: float = 4.0  # stationary on ground this long: landed
    live_window_minutes: float = 10.0
    store_raw: bool = False  # keep the raw APRS sentence per fix

    # Services
    ddb_enabled: bool = True
    ddb_url: str = "https://ddb.glidernet.org/download/?j=1&t=1"
    elevation_enabled: bool = True
    elevation_url: str = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    elevation_zoom: int = 12
    weather_enabled: bool = True
    weather_interval_hours: float = 3.0
    weather_backfill_days: int = 30

    # Optional HTTP basic auth (recommended when exposed on the internet)
    auth_user: str = ""
    auth_password: str = ""

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'prack.db').as_posix()}"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        s = cls()
        s.data_dir = Path(_env("DATA_DIR", str(s.data_dir)))
        s.database_url = _env("DATABASE_URL", "") or ""
        s.host = _env("HOST", s.host)
        s.port = _int("PORT", s.port)
        s.log_level = (_env("LOG_LEVEL", s.log_level) or "INFO").upper()
        s.regions = _list("REGIONS", s.regions)
        regions_dir = _env("REGIONS_DIR")
        s.regions_dir = Path(regions_dir) if regions_dir else None
        s.ogn_enabled = _bool("OGN_ENABLED", s.ogn_enabled)
        s.ogn_host = _env("OGN_HOST", s.ogn_host)
        s.ogn_port = _int("OGN_PORT", s.ogn_port)
        s.ogn_callsign = _env("OGN_CALLSIGN", "") or ""
        s.ogn_filter_margin_km = _float("OGN_FILTER_MARGIN_KM", s.ogn_filter_margin_km)
        s.demo = _bool("DEMO", s.demo)
        s.tracked_types = tuple(int(t) for t in _list("TRACKED_TYPES", [str(t) for t in s.tracked_types]))
        s.respect_stealth = _bool("RESPECT_STEALTH", s.respect_stealth)
        s.min_fix_interval = _float("MIN_FIX_INTERVAL", s.min_fix_interval)
        s.flight_gap_minutes = _float("FLIGHT_GAP_MINUTES", s.flight_gap_minutes)
        s.flight_resume_minutes = _float("FLIGHT_RESUME_MINUTES", s.flight_resume_minutes)
        s.landing_minutes = _float("LANDING_MINUTES", s.landing_minutes)
        s.live_window_minutes = _float("LIVE_WINDOW_MINUTES", s.live_window_minutes)
        s.store_raw = _bool("STORE_RAW", s.store_raw)
        s.ddb_enabled = _bool("DDB_ENABLED", s.ddb_enabled)
        s.ddb_url = _env("DDB_URL", s.ddb_url)
        s.elevation_enabled = _bool("ELEVATION_ENABLED", s.elevation_enabled)
        s.elevation_url = _env("ELEVATION_URL", s.elevation_url)
        s.elevation_zoom = _int("ELEVATION_ZOOM", s.elevation_zoom)
        s.weather_enabled = _bool("WEATHER_ENABLED", s.weather_enabled)
        s.weather_interval_hours = _float("WEATHER_INTERVAL_HOURS", s.weather_interval_hours)
        s.weather_backfill_days = _int("WEATHER_BACKFILL_DAYS", s.weather_backfill_days)
        s.auth_user = _env("AUTH_USER", "") or ""
        s.auth_password = _env("AUTH_PASSWORD", "") or ""
        return s

    def resolved_callsign(self) -> str:
        """Read-only APRS login name. Must be unique on the OGN servers."""
        if self.ogn_callsign:
            return self.ogn_callsign
        return f"PRACK{random.randint(1000, 9999)}"
