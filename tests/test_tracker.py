from datetime import datetime, timedelta

import pytest

from sqlalchemy import func, select

from prack.models import Device, Fix, Flight
from prack.ogn.ddb import DdbInfo
from prack.ogn.parser import parse_line
from prack.tracking.tracker import Finalizer, Tracker, epoch

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


def _fast_flight(start, address="3FF19F", speed=306.0, n=30, aircraft_type=7):
    """A "paraglider" crossing the Bernese Oberland at microlight speed."""
    lines, t, lon = [], start, 7.60
    for _ in range(n):
        lon += speed / 3.6 * 2 / 76000  # ~76 km per degree of longitude at 46.7 N
        lines.append((t, aprs(t, 46.70, lon, 3000, speed, 90, 0, address, aircraft_type)))
        t += timedelta(seconds=2)
    return lines


def test_too_fast_paraglider_is_dropped(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    _run(tracker, _fast_flight(START))
    assert _flights(runtime) == []
    assert tracker.live(0)["aircraft"] == []
    assert tracker.counters["ignored_implausible"] == 29  # every fix after the second fast one
    # a glider at the same speed is fine
    _run(tracker, _fast_flight(START, address="3FF1A0", speed=200, aircraft_type=1))
    assert len(_flights(runtime)) == 1


def test_single_speed_spike_is_ignored(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = flight_lines(START, minutes=10)
    i = 200  # in flight: one fix with a bogus 400 km/h ground speed
    t, line = lines[i]
    lines[i] = (t, line.replace("'090/019/", "'090/216/"))
    assert "'090/216/" in lines[i][1]
    _run(tracker, lines)
    flights = _flights(runtime)
    assert len(flights) == 1 and flights[0].close_reason == "landed"
    assert tracker.counters["ignored_implausible"] == 0


def test_same_device_on_two_protocols_is_one_aircraft(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = []
    for t, line in flight_lines(START, address="2027D5", minutes=10):
        lines.append((t, line))
        # the same device also heard via ADS-L / OGN tracker, one second later
        t2 = t + timedelta(seconds=1)
        dup = line.replace("FLR2027D5>OGFLR", "ICA2027D5>OGADSL").replace(f"/{t:%H%M%S}h", f"/{t2:%H%M%S}h")
        lines.append((t2, dup))
    _run(tracker, lines)
    assert len(_flights(runtime)) == 1
    assert len(tracker.live(0)["aircraft"]) == 1
    assert tracker.counters["merged"] > 0


def test_purge_implausible_flights(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions, None, None)
    _run(tracker, flight_lines(START, address="000001"))  # a genuine paraglider flight
    with runtime.db.session() as s, s.begin():  # an "ADS-B paraglider" stored by an older version
        good = s.scalars(select(Flight)).one()
        bad = Flight(device_id=good.device_id, region="ch", date=good.date, aircraft_type=7, source="ADS-B",
                     status="closed", start_time=good.start_time, end_time=good.end_time,
                     created_at=good.created_at, updated_at=good.updated_at, max_speed=306)
        s.add(bad)
    assert tracker.purge_implausible() == 1
    assert [f.source for f in _flights(runtime)] == ["FLARM"]


def _two_protocols(start, minutes=10, adsl_offset_deg=0.0, fanet_from=0):
    """One vario sending ADS-L (ICA...) and FANET (FNT...); FANET heard from fix ``fanet_from`` on.
    ``adsl_offset_deg`` shifts the ADS-L positions north during the first 120 fixes."""
    lines = []
    for i, (t, line) in enumerate(flight_lines(start, address="202B09", minutes=minutes)):
        b = parse_line(line, t)
        lat = b.lat + (adsl_offset_deg if i < 120 else 0.0)
        speed, course = b.speed or 0.0, int(b.track or 0)
        lines.append((t, aprs(t, lat, b.lon, b.alt, speed, course, b.climb or 0.0, "202B09", 7,
                              addr_type=1, prefix="ICA", tocall="OGADSL")))
        if i >= fanet_from:
            t2 = t + timedelta(seconds=1)
            lines.append((t2, aprs(t2, b.lat, b.lon, b.alt, speed, course, b.climb or 0.0, "202B09", 7,
                                   addr_type=3, prefix="FNT", tocall="OGNFNT")))
    return lines


def test_fanet_identity_wins_when_heard_later(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = _two_protocols(START, fanet_from=90)  # ADS-L only at take-off, FANET joins in flight
    lines.insert(200, (lines[199][0], 'FNT202B09>OGNFNT,qAS,Rx:>090501h Name="FluK"'))
    _run(tracker, lines)
    flights = _flights(runtime)
    assert len(flights) == 1
    with runtime.db.session() as s:
        device = s.get(Device, flights[0].device_id)
    assert device.callsign == "FNT202B09" and flights[0].source == "FANET"
    live = tracker.live(0)["aircraft"]
    assert [a["id"] for a in live] == ["FNT202B09"] and live[0]["name"] == "FluK"


def test_two_existing_aircraft_are_merged_into_one_flight(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    # the ADS-L positions are 6 km off for the first 4 minutes: two aircraft, two flights ...
    lines = _two_protocols(START, adsl_offset_deg=0.05)
    _run(tracker, lines[:200])
    assert len(_flights(runtime)) == 2
    # ... until they coincide: one aircraft, one flight, FANET identity
    _run(tracker, lines[200:])
    flights = _flights(runtime)
    assert len(flights) == 1 and flights[0].source == "FANET"
    assert [a["id"] for a in tracker.live(0)["aircraft"]] == ["FNT202B09"]
    with runtime.db.session() as s:
        stored = s.scalar(select(func.count()).select_from(Fix).where(Fix.flight_id == flights[0].id))
    assert stored == flights[0].fix_count


def test_stored_duplicates_are_merged_on_start(runtime):
    # an older version recorded the same flight twice (FANET + ADS-L)
    old = Tracker(runtime.db, runtime.settings, runtime.regions)
    old._peer = lambda b: None  # no merging back then
    _run(old, _two_protocols(START))
    assert sorted(f.source for f in _flights(runtime)) == ["FANET", "OGN tracker (ADS-L)"]
    finalizer = Finalizer(runtime.db, None)
    fresh = Tracker(runtime.db, runtime.settings, runtime.regions, None, None, finalizer.enqueue)
    assert fresh.merge_duplicate_flights() == 1
    finalizer.drain()
    (flight,) = _flights(runtime)
    assert flight.source == "FANET" and flight.airborne


FLARM_LAT = 46.6453  # flight_lines() flies due east on this latitude; the FANET positions are 11 m north


def _flarm_and_fanet(start, minutes=10, fanet_every=4, flarm_silent=()):
    """One paraglider carrying a FLARM (FLR112880, every second) and a FANET vario (FNT112880, every
    ``fanet_every`` seconds, slightly different positions). FLARM is not heard for the fix indices in
    ``flarm_silent``."""
    lines = []
    for i, (t, line) in enumerate(flight_lines(start, address="112880", minutes=minutes, step=1)):
        if i not in flarm_silent:
            lines.append((t, line))
        if i % fanet_every == 0:
            b = parse_line(line, t)
            lines.append((t, aprs(t, b.lat + 0.0001, b.lon, b.alt + 5, b.speed or 0.0, int(b.track or 0),
                                  b.climb or 0.0, "112880", 7, addr_type=3, prefix="FNT", tocall="OGNFNT")))
    return lines


def _stored_fixes(rt, flight_id):
    with rt.db.session() as s:
        return s.scalars(select(Fix).where(Fix.flight_id == flight_id).order_by(Fix.ts)).all()


def test_flarm_preferred_over_fanet(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = _flarm_and_fanet(START, flarm_silent=range(10))  # FANET heard first, FLARM a few seconds later
    lines.insert(30, (lines[29][0], 'FNT112880>OGNFNT,qAS,Rx:>090020h Name="Mia"'))
    _run(tracker, lines)
    (flight,) = _flights(runtime)
    with runtime.db.session() as s:
        device = s.get(Device, flight.device_id)
    assert device.callsign == "FLR112880" and flight.source == "FLARM"
    assert device.pilot_name == "Mia"  # the FANET name is kept
    live = tracker.live(0)["aircraft"]
    assert [a["id"] for a in live] == ["FLR112880"] and live[0]["name"] == "Mia"
    assert tracker.counters["secondary"] > 0
    fixes = _stored_fixes(runtime, flight.id)
    assert len(fixes) == flight.fix_count
    # FANET positions only until FLARM is heard, then every FLARM position (1/s)
    flarm_from = START + timedelta(seconds=10)
    assert all(f.ts < flarm_from for f in fixes if f.lat > FLARM_LAT + 0.00005)
    after = [f for f in fixes if f.ts >= flarm_from]
    assert len(after) == (flight.end_time - flarm_from).total_seconds() + 1


def test_fanet_fills_flarm_gaps(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    silent = range(300, 400)  # FLARM out of reception for 100 s in flight
    _run(tracker, _flarm_and_fanet(START, flarm_silent=silent))
    (flight,) = _flights(runtime)
    assert flight.source == "FLARM"
    fixes = _stored_fixes(runtime, flight.id)
    fanet = [f for f in fixes if f.lat > FLARM_LAT + 0.00005]
    gap_start, gap_end = START + timedelta(seconds=300), START + timedelta(seconds=400)
    assert len(fanet) >= 15  # FANET positions after the 30 s hold, every 4 s
    assert all(gap_start + timedelta(seconds=30) <= f.ts < gap_end for f in fanet)
    assert len({f.ts for f in fixes}) == len(fixes)


def test_restart_with_fanet_heard_first_resumes_flarm_flight(runtime):
    lines = _flarm_and_fanet(START)
    split = next(i for i, (t, _) in enumerate(lines) if t >= START + timedelta(minutes=5))
    first = Tracker(runtime.db, runtime.settings, runtime.regions)
    _run(first, lines[:split])
    # prack restarts; FLARM is heard again only a few seconds after FANET
    second = Tracker(runtime.db, runtime.settings, runtime.regions)
    second.restore()
    rest = [(t, line) for t, line in lines[split:]
            if not (line.startswith("FLR") and t < START + timedelta(minutes=5, seconds=8))]
    _run(second, rest)
    (flight,) = _flights(runtime)
    with runtime.db.session() as s:
        device = s.get(Device, flight.device_id)
    assert device.callsign == "FLR112880" and flight.close_reason == "landed"
    assert [a["id"] for a in second.live(0)["aircraft"]] == ["FLR112880"]


def test_live_updates_carry_every_new_position(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = _flarm_and_fanet(START)
    _run(tracker, lines[:100])
    seq = tracker.live(0)["seq"]
    _run(tracker, lines[100:110])
    (entry,) = tracker.live(seq, trail=False)["aircraft"]
    # every FLARM position since the last update, none of the FANET duplicates
    flarm_times = sorted({epoch(t) for t, line in lines[100:110] if line.startswith("FLR")})
    assert [p[0] for p in entry["pts"]] == flarm_times
    assert entry["pts"][-1][0] == entry["t"] and "trail" not in entry


def test_full_snapshot_carries_new_positions_too(runtime):
    tracker = Tracker(runtime.db, runtime.settings, runtime.regions)
    lines = _flarm_and_fanet(START)
    _run(tracker, lines[:100])
    seq = tracker.live(0)["seq"]
    _run(tracker, lines[100:110])
    (entry,) = tracker.live(0, True, seq)["aircraft"]
    assert entry["trail"] and len(entry["pts"]) == len([1 for _, line in lines[100:110] if line.startswith("FLR")])
    assert "pts" not in tracker.live(0)["aircraft"][0]
