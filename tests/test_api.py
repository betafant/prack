import base64
import io
import zipfile
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prack.api import create_app
from prack.demo import synthetic_weather
from prack.tracking.tracker import Tracker

from .conftest import flight_lines

DAY = date(2026, 7, 15)
START = datetime(2026, 7, 15, 9, 0, 0)


@pytest.fixture
def client(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions, None, None, runtime.finalizer.enqueue)
    for address, atype, offset in (("DD0001", 7, 0), ("DD0002", 6, 5), ("DD0003", 1, 10)):
        for ts, line in flight_lines(START + timedelta(minutes=offset), address=address, aircraft_type=atype):
            tracker.process_line(line, ts)
    tracker.flush(force=True)
    runtime.finalizer.drain()
    region = runtime.regions[0]
    runtime.weather.store(region, DAY, synthetic_weather(region, DAY), "demo (synthetic)", "demo")
    app = create_app(runtime=runtime, start=False)
    with TestClient(app) as c:
        yield c


def test_config_and_status(client):
    cfg = client.get("/api/config").json()
    assert cfg["regions"][0]["id"] == "ch"
    assert {c["id"] for c in cfg["categories"]} == {"pg", "hg", "gl", "ot"}
    assert any(b["id"] == "swisstopo" for b in cfg["regions"][0]["basemaps"])
    assert client.get("/api/status").json()["flights_total"] == 3


def test_flights_track_and_hide(client):
    flights = client.get(f"/api/flights?date={DAY}").json()["flights"]
    assert [f["cat"] for f in flights] == ["pg", "hg", "gl"]
    fid = flights[0]["id"]
    detail = client.get(f"/api/flights/{fid}").json()
    assert detail["device"]["callsign"] == "FLRDD0001"
    assert detail["weather"]["point"]["id"] == "oberland"  # nearest weather point to Niesen
    track = client.get(f"/api/flights/{fid}/track").json()
    assert len(track["t"]) == detail["fixes"] and len(track["alt"]) == len(track["t"])
    assert client.patch(f"/api/flights/{fid}", json={"hidden": True}).json()["hidden"] is True
    assert client.get(f"/api/flights?date={DAY}&hidden=false").json()["flights"][0]["id"] != fid
    assert client.get("/api/flights/9999").status_code == 404


@pytest.mark.parametrize("fmt", ["igc", "gpx", "kml", "geojson", "csv"])
def test_exports(client, fmt):
    fid = client.get(f"/api/flights?date={DAY}").json()["flights"][0]["id"]
    r = client.get(f"/api/flights/{fid}/export.{fmt}")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and f".{fmt}" in r.headers["content-disposition"]


def test_days_tracks_zip_and_weather(client):
    days = client.get("/api/days?start=2026-07-01&end=2026-07-31").json()["days"]
    assert days == [
        {"date": "2026-07-15", "total": 3, "hidden": 0, "pg": 1, "hg": 1, "gl": 1, "ot": 0, "weather": True}
    ]
    tracks = client.get(f"/api/tracks?date={DAY}").json()["tracks"]
    assert len(tracks) == 3 and all(len(t["path"]) >= 2 for t in tracks)
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/days/{DAY}/export.zip").content))
    names = z.namelist()
    assert "flights.csv" in names and "weather.json" in names
    assert sum(n.endswith(".igc") for n in names) == 3
    wx = client.get(f"/api/weather/{DAY}").json()
    assert len(wx["points"]) == 11
    assert {g["id"] for g in wx["gradients"]} == {"foehn", "bise"}


def test_basic_auth(runtime):
    runtime.settings.auth_user, runtime.settings.auth_password = "pilot", "s3cret"
    app = create_app(runtime=runtime, start=False)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/config").status_code == 401
        token = base64.b64encode(b"pilot:s3cret").decode()
        assert c.get("/api/config", headers={"Authorization": f"Basic {token}"}).status_code == 200
