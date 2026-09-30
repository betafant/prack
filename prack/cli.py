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
