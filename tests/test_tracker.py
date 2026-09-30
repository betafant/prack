from datetime import datetime, timedelta

import pytest

from sqlalchemy import func, select

from prack.models import Device, Fix, Flight
from prack.ogn.ddb import DdbInfo
from prack.tracking.tracker import Finalizer, Tracker

from .conftest import aprs, flight_lines

START = datetime(2026, 7, 15, 9, 0, 0)


def _run(tracker: Tracker, lines):
    for ts, line in lines:
        tracker.process_line(line, ts)
    tracker.flush(force=True)


def _flights(rt):
    with rt.db.session() as s:
        return s.scalars(select(Flight).order_by(Flight.id)).all()


def test_takeoff_landing_and_fixes(runtime):
    finalizer = Finalizer(runtime.db, None)
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions, None, None, finalizer.enqueue)
    _run(tracker, flight_lines(START))
    flights = _flights(runtime)
    assert len(flights) == 1
    f = flights[0]
    assert f.status == "closed" and f.close_reason == "landed"
    assert f.region == "ch" and f.aircraft_type == 7
    assert f.date.isoformat() == "2026-07-15"
    finalizer.drain()
    with runtime.db.session() as s:
        f = s.get(Flight, f.id)
        fixes = s.scalar(select(func.count()).select_from(Fix).where(Fix.flight_id == f.id))
    assert fixes == f.fix_count
    # the pre-takeoff buffer (last minute on the launch) is stored, the long ground wait before is not
    assert f.start_time >= START + timedelta(seconds=0)
    assert f.takeoff_time >= START + timedelta(seconds=60)
    assert f.max_alt >= 2300 and f.min_alt < 1100
    assert f.distance_km == pytest.approx(13.7, abs=0.2)  # 900 steps x 0.0002 deg lon at 46.6 N
    assert f.airborne
    assert f.preview and len(f.preview) >= 2


def test_min_fix_interval(runtime):
    runtime.settings.min_fix_interval = 10
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    _run(tracker, flight_lines(START, step=2))
    with runtime.db.session() as s:
        ts = s.scalars(select(Fix.ts).order_by(Fix.ts)).all()
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    assert gaps and min(gaps) >= 10


def test_gap_and_resume_same_flight(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = flight_lines(START, minutes=20)
    airborne = [(t, l) for t, l in lines if "'090/" in l]
    first, second = airborne[: len(airborne) // 2], airborne[len(airborne) // 2:]
    _run(tracker, lines[:30] + first)
    # 30 minutes of silence (coverage hole) -> flight closed by the sweeper
    last = first[-1][0]
    tracker.sweep(last + timedelta(minutes=30))
    tracker.flush(force=True)
    assert _flights(runtime)[0].close_reason == "gap"
    # the aircraft shows up again, still flying -> the same flight continues
    shift = timedelta(minutes=30)
    _run(tracker, [(t + shift, l.replace(f"/{t:%H%M%S}h", f"/{t + shift:%H%M%S}h")) for t, l in second])
    flights = _flights(runtime)
    assert len(flights) == 1
    assert flights[0].status == "active"


def test_privacy_and_type_filters(runtime):
    runtime.ddb.add_local("AAAAAA", DdbInfo(None, None, None, tracked=False, identified=True))
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions, runtime.ddb)
    t = START
    lines = []
    for i in range(10):
        ts = t + timedelta(seconds=2 * i)
        lines.append((ts, aprs(ts, 46.6, 7.6 + i * 0.001, 2000, 40, 90, address="111111", no_tracking=True)))
        lines.append((ts, aprs(ts, 46.6, 7.6 + i * 0.001, 2000, 40, 90, address="222222", stealth=True)))
        lines.append((ts, aprs(ts, 46.6, 7.6 + i * 0.001, 2000, 40, 90, address="AAAAAA")))  # DDB: do not track
        lines.append((ts, aprs(ts, 46.6, 7.6 + i * 0.001, 2000, 150, 90, address="333333", aircraft_type=8)))
        lines.append((ts, aprs(ts, 52.0, 13.0 + i * 0.001, 2000, 40, 90, address="444444")))  # outside region
    _run(tracker, lines)
    with runtime.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Device)) == 0
    assert tracker.counters["ignored_privacy"] == 30
    assert tracker.counters["ignored_type"] == 10
    assert tracker.counters["ignored_region"] == 10


def test_duplicates_and_live_state(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = flight_lines(START, minutes=5)
    _run(tracker, lines[:60] + lines[:60])  # the same beacons again (e.g. via another receiver)
    assert tracker.counters["duplicates"] == 60
    live = tracker.live(0)
    assert live["full"] and len(live["aircraft"]) == 1
    a = live["aircraft"][0]
    assert a["id"] == "FLRDD1234" and a["cat"] == "pg" and a["flying"]
    assert tracker.live(live["seq"])["aircraft"] == []


def test_fanet_name_updates_device(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    ts = START
    tracker.process_line(aprs(ts, 46.6, 7.6, 2000, 40, 90, address="1103CE", prefix="FNT", tocall="OGNFNT"), ts)
    tracker.process_line('FNT1103CE>OGNFNT,qAS,Rx:>090001h Name="Test Pilot" 45.0dB', ts)
    with runtime.db.session() as s:
        assert s.scalar(select(Device.pilot_name)) == "Test Pilot"
    assert tracker.live(0)["aircraft"][0]["name"] == "Test Pilot"


def test_restore_closes_active_flights(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = flight_lines(START, minutes=10)
    _run(tracker, lines[:200])
    assert _flights(runtime)[0].status == "active"
    fresh = Tracker(runtime.db, runtime.settings, runtime.regions)
    assert fresh.restore() == [_flights(runtime)[0].id]
    f = _flights(runtime)[0]
    assert f.status == "closed" and f.close_reason == "gap"
    assert "FLRDD1234" in fresh.states


def test_diagnostic_counters(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    ts = START
    tracker.process_line(aprs(ts, 46.6, 7.6, 11000, 800, 90, address="4B1234", aircraft_type=9, addr_type=1, prefix="ICA", tocall="OGADSB"), ts)
    tracker.process_line(aprs(ts, 46.6, 7.7, 2000, 30, 90, address="07220E"), ts)
    tracker.process_line("XYZ123456>OGNEW,qAS,Somewhere:/090000h4636.00N/00736.00E'000/000/A=001000 idWEIRD!", ts)
    assert tracker.sources == {"ADS-B": 1, "FLARM": 1}
    assert tracker.types == {9: 1, 7: 1}
    assert tracker.counters["ignored_type"] == 1
    # a position message with a malformed id is reported as undecodable, with a sample
    assert tracker.counters["unparsed"] == 1
    assert tracker.unparsed_samples[0].startswith("XYZ123456>OGNEW")
    # an unknown id format is still an aircraft, just without a type
    tracker.process_line("XYZ123456>OGNEW,qAS,Somewhere:/090000h4636.00N/00736.00E'000/000/A=001000 idWEIRD", ts)
    assert tracker.sources["OGNEW"] == 1 and tracker.types[0] == 1
