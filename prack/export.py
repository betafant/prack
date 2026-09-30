"""Track export: IGC, GPX, KML, GeoJSON and CSV."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from . import __version__
from .ogn.constants import AIRCRAFT_TYPES

FORMATS = {
    "igc": ("application/vnd.fai.igc", "igc"),
    "gpx": ("application/gpx+xml", "gpx"),
    "kml": ("application/vnd.google-earth.kml+xml", "kml"),
    "geojson": ("application/geo+json", "geojson"),
    "csv": ("text/csv", "csv"),
}


@dataclass
class TrackPoint:
    ts: datetime  # naive UTC
    lat: float
    lon: float
    alt: float
    ground: float | None = None
    speed: float | None = None
    track: float | None = None
    climb: float | None = None
    receiver: str | None = None


@dataclass
class FlightMeta:
    flight_id: int
    callsign: str
    address: str
    aircraft_type: int
    source: str
    registration: str | None = None
    competition_id: str | None = None
    model: str | None = None
    pilot: str | None = None

    @property
    def type_name(self) -> str:
        return AIRCRAFT_TYPES.get(self.aircraft_type, ("", "Unknown"))[1]

    @property
    def title(self) -> str:
        return self.pilot or self.registration or self.competition_id or self.callsign


def filename(meta: FlightMeta, points: list[TrackPoint], fmt: str) -> str:
    day = points[0].ts.strftime("%Y-%m-%d") if points else "flight"
    return f"{day}-{meta.callsign}-{meta.flight_id}.{FORMATS[fmt][1]}"


def _iso(ts: datetime) -> str:
    return ts.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _igc_coord(value: float, deg_digits: int, pos: str, neg: str) -> str:
    hemi = pos if value >= 0 else neg
    value = abs(value)
    degrees = int(value)
    milli_minutes = int(round((value - degrees) * 60000))
    if milli_minutes >= 60000:
        degrees += 1
        milli_minutes -= 60000
    return f"{degrees:0{deg_digits}d}{milli_minutes:05d}{hemi}"


def _igc_alt(alt: float) -> str:
    value = int(round(alt))
    value = max(-9999, min(99999, value))
    return f"{value:05d}" if value >= 0 else f"-{abs(value):04d}"


def _clean(text: str | None) -> str:
    return (text or "").replace("\r", " ").replace("\n", " ")


def to_igc(meta: FlightMeta, points: list[TrackPoint]) -> str:
    first = points[0].ts if points else datetime.now(timezone.utc)
    lines = [
        f"AXPR{meta.address[-3:] if meta.address else 'OGN'} prack OGN track",
        f"HFDTEDATE:{first:%d%m%y},01",
        "HFFXA035",
        f"HFPLTPILOTINCHARGE:{_clean(meta.pilot)}",
        "HFCM2CREW2:NIL",
        f"HFGTYGLIDERTYPE:{_clean(meta.model or meta.type_name)}",
        f"HFGIDGLIDERID:{_clean(meta.registration)}",
        "HFDTMGPSDATUM:WGS84",
        f"HFRFWFIRMWAREVERSION:prack {__version__}",
        "HFRHWHARDWAREVERSION:OGN",
        f"HFFTYFRTYPE:OGN {_clean(meta.source)} via prack",
        "HFGPSRECEIVER:unknown",
        "HFPRSPRESSALTSENSOR:NIL",
        f"HFCIDCOMPETITIONID:{_clean(meta.competition_id)}",
        f"HFCCLCOMPETITIONCLASS:{meta.type_name}",
        f"LPRKSOURCE OGN callsign {meta.callsign}, device {meta.address}",
        "LPRKNOTE Reconstructed from OGN ground station reports; no pressure altitude, GNSS altitude repeated",
    ]
    for p in points:
        alt = _igc_alt(p.alt)
        lines.append(
            f"B{p.ts:%H%M%S}{_igc_coord(p.lat, 2, 'N', 'S')}{_igc_coord(p.lon, 3, 'E', 'W')}A{alt}{alt}"
        )
    return "\r\n".join(lines) + "\r\n"


def to_gpx(meta: FlightMeta, points: list[TrackPoint]) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gpx version="1.1" creator="prack {__version__}" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{escape(meta.title)}</name>",
        f"    <desc>{escape(meta.type_name)} {escape(meta.callsign)} ({escape(meta.source)})</desc>",
        "  </metadata>",
        "  <trk>",
        f"    <name>{escape(meta.title)}</name>",
        f"    <type>{escape(meta.type_name)}</type>",
        "    <trkseg>",
    ]
    for p in points:
        out.append(
            f'      <trkpt lat="{p.lat:.6f}" lon="{p.lon:.6f}"><ele>{p.alt:.1f}</ele><time>{_iso(p.ts)}</time></trkpt>'
        )
    out += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(out)


def to_kml(meta: FlightMeta, points: list[TrackPoint]) -> str:
    coords = " ".join(f"{p.lon:.6f},{p.lat:.6f},{p.alt:.1f}" for p in points)
    when = "\n".join(f"        <when>{_iso(p.ts)}</when>" for p in points)
    gx = "\n".join(f"        <gx:coord>{p.lon:.6f} {p.lat:.6f} {p.alt:.1f}</gx:coord>" for p in points)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
  <Document>
    <name>{escape(meta.title)} - {escape(meta.type_name)}</name>
    <Style id="track"><LineStyle><color>ffc93eff</color><width>3</width></LineStyle><PolyStyle><color>40c93eff</color></PolyStyle></Style>
    <Placemark>
      <name>Track (curtain)</name>
      <styleUrl>#track</styleUrl>
      <LineString>
        <extrude>1</extrude>
        <tessellate>0</tessellate>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>{coords}</coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>Timed track</name>
      <styleUrl>#track</styleUrl>
      <gx:Track>
        <altitudeMode>absolute</altitudeMode>
{when}
{gx}
      </gx:Track>
    </Placemark>
  </Document>
</kml>
"""


def to_geojson(meta: FlightMeta, points: list[TrackPoint]) -> str:
    feature = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[p.lon, p.lat, p.alt] for p in points]},
        "properties": {
            "flight_id": meta.flight_id,
            "callsign": meta.callsign,
            "aircraft_type": meta.type_name,
            "source": meta.source,
            "registration": meta.registration,
            "competition_id": meta.competition_id,
            "pilot": meta.pilot,
            "coordTimes": [_iso(p.ts) for p in points],
            "ground": [p.ground for p in points],
            "speed_kmh": [p.speed for p in points],
            "climb_ms": [p.climb for p in points],
        },
    }
    return json.dumps({"type": "FeatureCollection", "features": [feature]}, separators=(",", ":"))


def to_csv(meta: FlightMeta, points: list[TrackPoint]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time_utc", "lat", "lon", "alt_m", "ground_m", "agl_m", "speed_kmh", "track_deg", "climb_ms", "receiver"])
    for p in points:
        agl = round(p.alt - p.ground, 1) if p.ground is not None else ""
        writer.writerow(
            [_iso(p.ts), f"{p.lat:.6f}", f"{p.lon:.6f}", f"{p.alt:.1f}",
             "" if p.ground is None else p.ground, agl,
             "" if p.speed is None else p.speed, "" if p.track is None else p.track,
             "" if p.climb is None else p.climb, p.receiver or ""]
        )
    return buffer.getvalue()


RENDERERS = {"igc": to_igc, "gpx": to_gpx, "kml": to_kml, "geojson": to_geojson, "csv": to_csv}


def render(fmt: str, meta: FlightMeta, points: list[TrackPoint]) -> str:
    return RENDERERS[fmt](meta, points)
