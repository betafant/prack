import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytest

from prack.export import FlightMeta, TrackPoint, render, to_igc
from prack.tracking.stats import FixPoint, compute_stats, simplify_track
from prack.weather import summarize

META = FlightMeta(7, "FLRDDA5BA", "DDA5BA", 1, "FLARM", "HB-1234", "X4", "Discus 2", None)


def points(n=5):
    t0 = datetime(2026, 7, 15, 10, 0, 0)
    return [TrackPoint(t0 + timedelta(seconds=4 * i), 46.5 + i * 0.001, 7.25 - i * 0.001, 1500 + 10 * i, 1200.0) for i in range(n)]


def test_igc_records():
    igc = to_igc(META, points())
    lines = igc.split("\r\n")
    assert lines[0].startswith("AXPR")
    assert "HFDTEDATE:150726,01" in lines
    assert "HFGIDGLIDERID:HB-1234" in lines
    b = [line for line in lines if line.startswith("B")]
    assert len(b) == 5
    # B HHMMSS DDMMmmmN DDDMMmmmE A PPPPP GGGGG
    assert b[0] == "B1000004630000N00715000EA0150001500"
    assert all(len(line) == 35 for line in b)


def test_igc_southern_western_negative():
    p = [TrackPoint(datetime(2026, 1, 2, 3, 4, 5), -33.999999, -70.5, -12.0)]
    b = [line for line in to_igc(META, p).split("\r\n") if line.startswith("B")][0]
    assert b == "B0304053400000S07030000WA-0012-0012"


@pytest.mark.parametrize("fmt", ["gpx", "kml"])
def test_xml_exports_are_well_formed(fmt):
    root = ET.fromstring(render(fmt, META, points()).encode())
    assert root.tag.endswith("gpx" if fmt == "gpx" else "kml")


def test_geojson_and_csv():
    data = json.loads(render("geojson", META, points()))
    coords = data["features"][0]["geometry"]["coordinates"]
    assert len(coords) == 5 and len(coords[0]) == 3
    csv_text = render("csv", META, points())
    assert csv_text.splitlines()[0].startswith("time_utc,lat,lon,alt_m,ground_m,agl_m")
    assert len(csv_text.splitlines()) == 6


def test_compute_stats():
    t0 = datetime(2026, 7, 15, 10, 0, 0)
    fixes = [FixPoint(t0, 46.0, 7.0, 1000, 900, 0, 0)]
    for i in range(1, 61):  # climb 2 m/s for 2 minutes, moving north at ~40 km/h
        fixes.append(FixPoint(t0 + timedelta(seconds=2 * i), 46.0 + i * 0.0002, 7.0, 1000 + 4 * i, 900, 40, 2))
    s = compute_stats(fixes)
    assert s["fix_count"] == 61
    assert s["max_alt"] == 1240 and s["min_alt"] == 1000
    assert s["max_climb"] == pytest.approx(2.0, abs=0.01)
    assert s["max_agl"] == 340
    assert s["distance_km"] == pytest.approx(1.33, abs=0.02)
    assert s["airborne"]
    assert s["takeoff_time"] == t0 + timedelta(seconds=2)


def test_simplify_track_keeps_shape():
    straight = [(7.0 + i * 0.0001, 46.0, 1000 + i) for i in range(1000)]
    out = simplify_track(straight)
    assert len(out) == 2 and out[0][:2] == [7.0, 46.0]
    zigzag = [(7.0 + i * 0.001, 46.0 + (0.01 if i % 2 else 0), 1000) for i in range(2000)]
    assert len(simplify_track(zigzag, max_points=300)) <= 300


def test_weather_summary():
    hourly = {
        "time": [f"2026-07-15T{h:02d}:00" for h in range(24)],
        "temperature_2m": [15 + (h if h < 15 else 15) * 0.5 for h in range(24)],
        "dew_point_2m": [8.0] * 24,
        "boundary_layer_height": [100 * h for h in range(24)],
        "cape": [50.0] * 24,
        "wind_speed_700hPa": [30.0] * 24,
        "wind_direction_700hPa": [250.0] * 24,
        "temperature_850hPa": [10.0] * 24,
        "temperature_500hPa": [-15.0] * 24,
        "geopotential_height_850hPa": [1500.0] * 24,
        "geopotential_height_500hPa": [5700.0] * 24,
    }
    daily = {"temperature_2m_max": [22.5], "sunshine_duration": [36000.0]}
    s = summarize(hourly, daily, 500.0)
    assert s["t_max"] == 22.5
    assert s["sunshine_h"] == 10.0
    assert s["blh_max"] == 1700  # max over 10..17 LT
    assert s["cloudbase_14"] == pytest.approx(125 * (22 - 8) + 500)
    assert s["wind_700"] == {"speed": 30.0, "dir": 250.0}
    assert s["lapse_850_500"] == pytest.approx(25 / 4.2, abs=0.01)
