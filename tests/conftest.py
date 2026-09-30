from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from prack.config import Settings
from prack.runtime import Runtime


def aprs(
    ts: datetime,
    lat: float,
    lon: float,
    alt_m: float,
    speed_kmh: float = 0.0,
    course: int = 0,
    climb_ms: float = 0.0,
    address: str = "DD1234",
    aircraft_type: int = 7,
    addr_type: int = 2,
    stealth: bool = False,
    no_tracking: bool = False,
    prefix: str = "FLR",
    tocall: str = "OGFLR",
) -> str:
    """Build an OGN aircraft beacon the way the OGN servers send it."""

    def coord(value: float, digits: int, pos: str, neg: str) -> tuple[str, int]:
        hemi = pos if value >= 0 else neg
        value = abs(value)
        deg = int(value)
        thousandths = int(round((value - deg) * 60000))
        minutes, extra = divmod(thousandths, 10)
        return f"{deg:0{digits}d}{minutes // 100:02d}.{minutes % 100:02d}{hemi}", extra

    la, lae = coord(lat, 2, "N", "S")
    lo, loe = coord(lon, 3, "E", "W")
    flags = (0x80 if stealth else 0) | (0x40 if no_tracking else 0) | (aircraft_type << 2) | addr_type
    return (
        f"{prefix}{address}>{tocall},qAS,TestRx:/{ts:%H%M%S}h{la}/{lo}'{course:03d}/{int(round(speed_kmh / 1.852)):03d}"
        f"/A={int(round(alt_m / 0.3048)):06d} !W{lae}{loe}! id{flags:02X}{address} {int(round(climb_ms / 0.00508)):+04d}fpm"
        f" +0.0rot 12.5dB 0e +1.2kHz gps2x3"
    )


def flight_lines(start: datetime, address: str = "DD1234", aircraft_type: int = 7, minutes: int = 30, step: int = 2):
    """A small synthetic flight near Niesen: ground, take-off, soaring, landing, ground."""
    lines = []
    t = start
    lat, lon, alt = 46.6453, 7.6511, 2330.0
    for _ in range(30):  # 1 minute on the ground at launch
        lines.append((t, aprs(t, lat, lon, alt, 0, 0, 0, address, aircraft_type)))
        t += timedelta(seconds=step)
    n = minutes * 60 // step
    for _ in range(n):  # fly east, descending to the valley
        lon += 0.0002
        alt -= 1300.0 / n
        lines.append((t, aprs(t, lat, lon, alt, 36, 90, -1.2, address, aircraft_type)))
        t += timedelta(seconds=step)
    for _ in range(int(8 * 60 / step)):  # 8 minutes standing in the landing field
        lines.append((t, aprs(t, lat, lon, alt, 0, 0, 0, address, aircraft_type)))
        t += timedelta(seconds=step)
    return lines


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path
    s.database_url = f"sqlite:///{tmp_path / 'test.db'}"
    s.ogn_enabled = False
    s.ddb_enabled = False
    s.weather_enabled = False
    s.elevation_enabled = False
    s.demo = False
    return s


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    rt = Runtime(settings)
    yield rt
    rt.db.dispose()
