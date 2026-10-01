"""Command line interface: ``prack run``, ``prack demo-seed``, ``prack weather``, ..."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from .config import Settings


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "demo", False):
        settings.demo = True
    if getattr(args, "host", None):
        settings.host = args.host
    if getattr(args, "port", None):
        settings.port = args.port
    return settings


def cmd_run(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    settings = _settings(args)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(), proxy_headers=True)


def _runtime(args: argparse.Namespace):
    from .runtime import Runtime

    return Runtime(_settings(args))


def cmd_init_db(args: argparse.Namespace) -> None:
    rt = _runtime(args)
    print(f"Database ready: {rt.settings.db_url}")


def cmd_demo_seed(args: argparse.Namespace) -> None:
    from .demo import seed_history

    rt = _runtime(args)
    created = seed_history(rt, days=args.days, progress=lambda text: print(text, flush=True))
    rt.finalizer.drain()
    print(f"{created} demo flights created")


def cmd_weather(args: argparse.Namespace) -> None:
    rt = _runtime(args)
    region = rt.regions_by_id[args.region] if args.region else rt.regions[0]
    day = date.fromisoformat(args.date) if args.date else datetime.now(region.tz).date()
    days = [day - timedelta(days=i) for i in range(args.days)]
    for d in days:
        n = rt.weather.fetch_day(region, d)
        print(f"{region.id} {d}: {n} points stored")


def cmd_ddb(args: argparse.Namespace) -> None:
    rt = _runtime(args)
    print(f"{rt.ddb.refresh()} devices in the OGN device database")


def cmd_replay(args: argparse.Namespace) -> None:
    """Feed a file of raw APRS lines (e.g. captured from the OGN feed) through the tracker."""
    from .ogn.parser import parse_line

    rt = _runtime(args)
    tracker = rt.tracker
    reference = datetime.combine(date.fromisoformat(args.date), datetime.min.time()).replace(hour=12)
    count = 0
    with open(args.file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parsed = parse_line(line, reference)
            if parsed is not None and getattr(parsed, "timestamp", None):
                reference = parsed.timestamp
            tracker.process_line(line, reference)
            count += 1
            if count % 5000 == 0:
                tracker.flush()
                tracker.sweep(reference)
    tracker.sweep(reference + timedelta(hours=3))
    tracker.flush(force=True)
    rt.finalizer.drain()
    print(f"{count} lines replayed: {tracker.counters}")


def cmd_diagnose(args: argparse.Namespace) -> None:
    """Listen to the OGN feed for a while and report what arrives and why aircraft are dropped.

    Uses a throw-away database, so nothing is written to the real one.
    """
    import queue
    import tempfile
    import threading
    import time
    from collections import Counter

    from .db import Database, utcnow
    from .ogn.aprs import AprsClient
    from .ogn.constants import AIRCRAFT_TYPES
    from .ogn.parser import AircraftBeacon, parse_line
    from .regions import load_regions, region_for
    from .tracking.tracker import Tracker

    settings = _settings(args)
    regions = load_regions(settings.regions, settings.regions_dir)
    db = Database(f"sqlite:///{Path(tempfile.mkdtemp()) / 'diagnose.db'}")
    db.create_all()
    tracker = Tracker(db, settings, regions)
    filter_expr = " ".join(r.aprs_filter(settings.ogn_filter_margin_km) for r in regions)
    lines: queue.Queue[str] = queue.Queue()
    client = AprsClient(settings.ogn_host, settings.ogn_port, settings.resolved_callsign(), filter_expr, lines.put)
    stop = threading.Event()
    threading.Thread(target=client.run, args=(stop,), daemon=True).start()
    print(f"Listening to {settings.ogn_host}:{settings.ogn_port} ({filter_expr}) for {args.seconds} s ...", flush=True)

    seen: dict[str, AircraftBeacon] = {}
    start = time.monotonic()
    next_report = start + 10
    while time.monotonic() - start < args.seconds:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            pass
        else:
            now = utcnow()
            beacon = parse_line(line, now)
            if isinstance(beacon, AircraftBeacon):
                seen[beacon.callsign] = beacon
            tracker.process_line(line, now)
        if time.monotonic() >= next_report:
            next_report += 10
            st = client.status
            print(f"  {int(time.monotonic() - start):4d} s  link={st.state}  lines={st.lines}  aircraft={len(seen)}", flush=True)
    stop.set()
    client.stop()

    def type_name(t: int) -> str:
        return AIRCRAFT_TYPES.get(t, ("", str(t)))[1]

    st = client.status
    print("\n=== OGN link")
    print(f"state {st.state}, server {st.server or '-'}, lines {st.lines}, error {st.last_error or '-'}")
    print("\n=== Aircraft received (all types, before filters)")
    for t, n in Counter(b.aircraft_type for b in seen.values()).most_common():
        mark = "tracked" if t in settings.tracked_types else "not tracked (PRACK_TRACKED_TYPES)"
        print(f"  {n:4d} x {type_name(t):18s} {mark}")
    print("  by source: " + ", ".join(f"{s} {n}" for s, n in Counter(b.source for b in seen.values()).most_common()))

    print("\n=== Paragliders / hang gliders / gliders (tracked types)")
    rows = [b for b in seen.values() if b.aircraft_type in settings.tracked_types]
    if not rows:
        print("  none received")
    now = utcnow()
    for b in sorted(rows, key=lambda b: (b.aircraft_type, b.callsign)):
        if b.no_tracking:
            status = "dropped: no-tracking flag"
        elif b.stealth and settings.respect_stealth:
            status = "dropped: stealth (PRACK_RESPECT_STEALTH)"
        elif (state := tracker.states.get(b.callsign)) is not None and state.misclassified:
            status = "dropped: far too fast for its declared aircraft type"
        elif (primary := tracker.by_address.get(b.address)) not in (None, b.callsign) and primary in tracker.states:
            status = f"merged with {primary} (same device, other protocol)"
        elif b.callsign in tracker.states:
            status = "shown (in flight)" if tracker.states[b.callsign].flight else "shown (on ground: GND key)"
        elif region_for(regions, b.lat, b.lon) is None:
            status = "dropped: outside region box"
        elif abs((now - b.timestamp).total_seconds()) > 1800:
            status = f"dropped: time stamp {b.timestamp:%H:%M:%S} too far from now"
        else:
            status = "dropped"
        speed = f"{b.speed:.0f} km/h" if b.speed is not None else "- km/h"
        print(f"  {b.callsign:10s} {type_name(b.aircraft_type):12s} {b.source:12s} {b.alt:6.0f} m {speed:>9s}"
              f"  {b.lat:.4f},{b.lon:.4f}  {status}")

    print("\n=== Tracker counters")
    print("  " + ", ".join(f"{k} {v}" for k, v in tracker.counters.items()))
    if tracker.unparsed_samples:
        print("\n=== Aircraft messages that could not be decoded (please report)")
        for sample in tracker.unparsed_samples:
            print("  " + sample)


def cmd_export(args: argparse.Namespace) -> None:
    from sqlalchemy import select

    from .export import FlightMeta, TrackPoint, filename, render
    from .models import Device, Fix, Flight

    rt = _runtime(args)
    with rt.db.session() as session:
        flight, device = session.execute(
            select(Flight, Device).join(Device, Flight.device_id == Device.id).where(Flight.id == args.flight)
        ).one()
        rows = session.execute(
            select(Fix.ts, Fix.lat, Fix.lon, Fix.alt, Fix.ground, Fix.speed, Fix.track, Fix.climb, Fix.receiver)
            .where(Fix.flight_id == flight.id)
            .order_by(Fix.ts)
        ).all()
    points = [TrackPoint(*r) for r in rows]
    meta = FlightMeta(flight.id, device.callsign, device.address, flight.aircraft_type, flight.source,
                      device.registration if device.identified else None,
                      device.competition_id if device.identified else None, device.model, device.pilot_name)
    out = Path(args.output or filename(meta, points, args.format))
    out.write_text(render(args.format, meta, points), encoding="utf-8")
    print(f"Written {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prack", description="prack - paragliding & track (OGN)")
    parser.add_argument("--version", action="version", version=f"prack {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("run", help="start the web app and the OGN receiver")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--demo", action="store_true", help="simulated traffic instead of the OGN feed")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("init-db", help="create the database tables")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("demo-seed", help="simulate past flying days into the archive")
    p.add_argument("--days", type=int, default=3)
    p.set_defaults(func=cmd_demo_seed)

    p = sub.add_parser("weather", help="download and store the weather of a day")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.add_argument("--days", type=int, default=1, help="number of days back from --date")
    p.add_argument("--region")
    p.set_defaults(func=cmd_weather)

    p = sub.add_parser("ddb", help="refresh the OGN device database")
    p.set_defaults(func=cmd_ddb)

    p = sub.add_parser("replay", help="replay a file of raw APRS lines")
    p.add_argument("file")
    p.add_argument("--date", required=True, help="UTC date of the capture (YYYY-MM-DD)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("diagnose", help="listen to OGN for a while and report what is received")
    p.add_argument("--seconds", type=int, default=60)
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("export", help="export one flight")
    p.add_argument("flight", type=int)
    p.add_argument("--format", default="igc", choices=["igc", "gpx", "kml", "geojson", "csv"])
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["run", *(argv or sys.argv[1:])])
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
