"""Parser for OGN APRS sentences.

Example aircraft beacon (FLARM)::

    FLRDDA5BA>OGFLR,qAS,LFMX:/160829h4415.41N/00600.03E'342/049/A=005524 !W52! id0ADDA5BA -454fpm -1.1rot 8.8dB 0e +51.2kHz gps4x5

Only what prack needs is decoded: aircraft positions (with the OGN ``id``
field that carries aircraft type and privacy flags) and status messages
(FANET pilot names). Receiver beacons are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .constants import RECEIVER_TOCALLS, source_for

KNOTS_TO_KMH = 1.852
FEET_TO_M = 0.3048
FPM_TO_MS = 0.00508
ROT_TO_DEGS = 3.0  # 1 "rot" = standard rate turn = 3 deg/s

HEADER_RE = re.compile(r"^(?P<callsign>[^>\s]{1,12})>(?P<tocall>[^,:]+)(?:,(?P<path>[^:]*))?:(?P<payload>.*)$")

POSITION_RE = re.compile(
    r"^(?:[/@](?P<time>\d{6})(?P<tfmt>[hz])|[!=])"
    r"(?P<lat>\d{4}\.\d{2})(?P<ns>[NS])(?P<symtab>.)"
    r"(?P<lon>\d{5}\.\d{2})(?P<ew>[EW])(?P<sym>.)"
    r"(?:(?P<course>\d{3})/(?P<speed>\d{3}))?"
    r"(?:/A=(?P<alt>-?\d{5,6}))?"
    r"(?P<rest>.*)$"
)

STATUS_RE = re.compile(r"^>(?:(?P<time>\d{6})(?P<tfmt>[hz]))?(?P<text>.*)$")
NAME_RE = re.compile(r'Name="([^"]*)"')

_TOKENS: list[tuple[str, re.Pattern]] = [
    ("precision", re.compile(r"^!W(\d)(\d)!$")),
    ("id", re.compile(r"^id([0-9A-Fa-f]{2})([0-9A-Fa-f]{6})$")),
    ("climb", re.compile(r"^([+-]?\d+)fpm$")),
    ("turn", re.compile(r"^([+-]?\d+(?:\.\d+)?)rot$")),
    ("flight_level", re.compile(r"^FL(\d+(?:\.\d+)?)$")),
    ("signal", re.compile(r"^([+-]?\d+(?:\.\d+)?)dB$")),
    ("errors", re.compile(r"^(\d+)e$")),
    ("freq", re.compile(r"^([+-]?\d+(?:\.\d+)?)kHz$")),
    ("gps", re.compile(r"^gps(\d+x\d+)$")),
    ("software", re.compile(r"^s(\d+(?:\.\d+)*)$")),
    ("hardware", re.compile(r"^h([0-9A-Fa-f]{2,4})$")),
    ("real_address", re.compile(r"^r([0-9A-Fa-f]{6})$")),
    ("power", re.compile(r"^([+-]?\d+(?:\.\d+)?)dBm$")),
]


@dataclass
class AircraftBeacon:
    callsign: str
    tocall: str
    source: str
    receiver: str | None
    timestamp: datetime  # naive UTC
    lat: float
    lon: float
    alt: float  # m
    address: str
    address_type: int
    aircraft_type: int
    stealth: bool = False
    no_tracking: bool = False
    track: float | None = None  # deg
    speed: float | None = None  # km/h
    climb: float | None = None  # m/s
    turn: float | None = None  # deg/s
    signal: float | None = None
    errors: int | None = None
    freq_offset: float | None = None
    gps: str | None = None
    software: str | None = None
    hardware: str | None = None
    real_address: str | None = None
    flight_level: float | None = None
    raw: str = field(default="", repr=False)


@dataclass
class StatusBeacon:
    callsign: str
    tocall: str
    source: str
    timestamp: datetime | None
    text: str
    name: str | None = None


def decode_time(value: str, fmt: str, reference: datetime) -> datetime | None:
    """Combine an APRS time stamp with the reception time (handles midnight roll-over)."""
    try:
        if fmt == "h":
            hour, minute, second = int(value[0:2]), int(value[2:4]), int(value[4:6])
            candidate = reference.replace(hour=hour, minute=minute, second=second, microsecond=0)
            best = min(
                (candidate + timedelta(days=d) for d in (-1, 0, 1)),
                key=lambda c: abs((c - reference).total_seconds()),
            )
            return best
        # "z": DDHHMM
        day, hour, minute = int(value[0:2]), int(value[2:4]), int(value[4:6])
        candidate = reference.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate - reference > timedelta(days=1):
            month_start = reference.replace(day=1)
            prev = month_start - timedelta(days=1)
            candidate = candidate.replace(year=prev.year, month=prev.month)
        return candidate
    except ValueError:
        return None


def _coord(value: str, deg_digits: int, hemisphere: str, extra: int | None) -> float:
    degrees = int(value[:deg_digits])
    minutes = float(value[deg_digits:])
    if extra is not None:
        minutes += extra / 1000.0
    result = degrees + minutes / 60.0
    return -result if hemisphere in ("S", "W") else result


def _receiver(path: str | None) -> str | None:
    if not path:
        return None
    parts = path.split(",")
    for i, part in enumerate(parts):
        if part.startswith("qA") and i + 1 < len(parts):
            return parts[i + 1].rstrip("*")
    return None


def parse_line(line: str, reference: datetime) -> AircraftBeacon | StatusBeacon | None:
    """Parse one APRS line. ``reference`` is the (naive UTC) reception time."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    header = HEADER_RE.match(line)
    if not header:
        return None
    callsign = header["callsign"]
    tocall = header["tocall"]
    path = header["path"]
    payload = header["payload"]
    if tocall in RECEIVER_TOCALLS or (path and "TCPIP" in path):
        return None
    source = source_for(tocall)

    if payload.startswith(">"):
        m = STATUS_RE.match(payload)
        if not m:
            return None
        ts = decode_time(m["time"], m["tfmt"], reference) if m["time"] else None
        text = m["text"].strip()
        name_match = NAME_RE.search(text)
        return StatusBeacon(callsign, tocall, source, ts, text, name_match.group(1).strip() if name_match else None)

    m = POSITION_RE.match(payload)
    if not m:
        return None

    fields: dict = {}
    for token in m["rest"].split():
        for key, pattern in _TOKENS:
            tm = pattern.match(token)
            if tm:
                fields[key] = tm.groups()
                break
    if "id" not in fields:
        return None  # not an aircraft (receivers, weather stations, ...)

    flags = int(fields["id"][0], 16)
    address = fields["id"][1].upper()
    lat_extra = lon_extra = None
    if "precision" in fields:
        lat_extra, lon_extra = int(fields["precision"][0]), int(fields["precision"][1])

    if m["time"]:
        ts = decode_time(m["time"], m["tfmt"], reference)
        if ts is None:
            return None
    else:
        ts = reference.replace(microsecond=0)

    lat = _coord(m["lat"], 2, m["ns"], lat_extra)
    lon = _coord(m["lon"], 3, m["ew"], lon_extra)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    beacon = AircraftBeacon(
        callsign=callsign,
        tocall=tocall,
        source=source,
        receiver=_receiver(path),
        timestamp=ts,
        lat=lat,
        lon=lon,
        alt=int(m["alt"]) * FEET_TO_M if m["alt"] else 0.0,
        address=address,
        address_type=flags & 0x03,
        aircraft_type=(flags >> 2) & 0x0F,
        stealth=bool(flags & 0x80),
        no_tracking=bool(flags & 0x40),
        raw=line,
    )
    if m["course"] is not None:
        course = int(m["course"])
        beacon.track = float(course % 360) if course else (0.0 if int(m["speed"]) else None)
        beacon.speed = int(m["speed"]) * KNOTS_TO_KMH
    if "climb" in fields:
        beacon.climb = int(fields["climb"][0]) * FPM_TO_MS
    if "turn" in fields:
        beacon.turn = float(fields["turn"][0]) * ROT_TO_DEGS
    if "signal" in fields:
        beacon.signal = float(fields["signal"][0])
    if "errors" in fields:
        beacon.errors = int(fields["errors"][0])
    if "freq" in fields:
        beacon.freq_offset = float(fields["freq"][0])
    if "gps" in fields:
        beacon.gps = fields["gps"][0]
    if "software" in fields:
        beacon.software = fields["software"][0]
    if "hardware" in fields:
        beacon.hardware = fields["hardware"][0].upper()
    if "real_address" in fields:
        beacon.real_address = fields["real_address"][0].upper()
    if "flight_level" in fields:
        beacon.flight_level = float(fields["flight_level"][0])
    return beacon
