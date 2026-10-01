"""Wires the services together and runs the background threads."""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

from sqlalchemy import func, select

from . import __version__
from .config import Settings
from .db import Database, utcnow
from .elevation import ElevationService
from .models import Flight
from .ogn.aprs import AprsClient, LinkStatus
from .ogn.constants import AIRCRAFT_TYPES
from .ogn.ddb import DdbInfo, DeviceDatabase
from .ogn.simulator import Simulator
from .regions import load_regions
from .tracking.tracker import Finalizer, Tracker
from .weather import WeatherService

log = logging.getLogger(__name__)

DDB_REFRESH_SECONDS = 24 * 3600


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.regions = load_regions(settings.regions, settings.regions_dir)
        self.regions_by_id = {r.id: r for r in self.regions}
        self.db = Database(settings.db_url)
        self.db.create_all()
        self.ddb = DeviceDatabase(self.db, settings.ddb_url)
        self.elevation = ElevationService(
            settings.elevation_url, settings.elevation_zoom, settings.data_dir / "dem", settings.elevation_enabled
        )
        self.finalizer = Finalizer(self.db, self.elevation)
        self.tracker = Tracker(
            self.db, settings, self.regions, self.ddb, self.elevation, on_flight_closed=self.finalizer.enqueue
        )
        self.weather = WeatherService(
            self.db, self.regions, settings.weather_interval_hours, settings.weather_backfill_days
        )
        self.link_status = LinkStatus()
        self.client: AprsClient | None = None
        self.simulator: Simulator | None = None
        self.lines: queue.Queue[tuple[datetime, str]] = queue.Queue(maxsize=200_000)
        self.dropped_lines = 0
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.started_at = utcnow()
        self.seeding: str | None = None

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        self.ddb.load_from_db()
        self.tracker.purge_implausible()
        self.tracker.merge_duplicate_flights()
        reopened = self.tracker.restore()
        if reopened:
            log.info("%d flights from the previous run closed (resume if still airborne)", len(reopened))
        self._thread("finalizer", self.finalizer.run, self.stop_event)
        self._thread("processor", self._process_loop)
        if self.settings.demo:
            self._start_demo()
        elif self.settings.ogn_enabled:
            margin = self.settings.ogn_filter_margin_km
            filter_expr = " ".join(r.aprs_filter(margin) for r in self.regions)
            self.client = AprsClient(
                self.settings.ogn_host,
                self.settings.ogn_port,
                self.settings.resolved_callsign(),
                filter_expr,
                self._enqueue,
            )
            self.link_status = self.client.status
            self._thread("ogn", self.client.run, self.stop_event)
        if self.settings.ddb_enabled and not self.settings.demo:
            self._thread("ddb", self._ddb_loop)
        if self.settings.weather_enabled:
            self._thread("weather", self.weather.run, self.stop_event)
        log.info("prack %s started (regions: %s)", __version__, ", ".join(r.id for r in self.regions))

    def stop(self) -> None:
        self.stop_event.set()
        if self.client:
            self.client.stop()
        for thread in self.threads:
            thread.join(timeout=5)
        try:
            self.tracker.flush(force=True)
        except Exception:  # noqa: BLE001
            log.exception("Final flush failed")
        self.db.dispose()

    def _thread(self, name: str, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    # -- ingestion ------------------------------------------------------------------------

    def _enqueue(self, line: str) -> None:
        try:
            self.lines.put_nowait((utcnow(), line))
        except queue.Full:
            self.dropped_lines += 1

    def _process_loop(self) -> None:
        last_flush = last_sweep = time.monotonic()
        while not self.stop_event.is_set():
            try:
                received, line = self.lines.get(timeout=0.5)
                self.tracker.process_line(line, received)
            except queue.Empty:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Processing failed")
            now = time.monotonic()
            try:
                if now - last_flush >= 1.0:
                    last_flush = now
                    self.tracker.flush()
                if now - last_sweep >= 30.0:
                    last_sweep = now
                    self.tracker.sweep()
            except Exception:  # noqa: BLE001
                log.exception("Flush/sweep failed")

    def _ddb_loop(self) -> None:
        if len(self.ddb) and self.stop_event.wait(60):
            return
        while not self.stop_event.is_set():
            try:
                self.ddb.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("DDB refresh failed: %s", exc)
                if self.stop_event.wait(3600):
                    return
                continue
            if self.stop_event.wait(DDB_REFRESH_SECONDS):
                return

    # -- demo -------------------------------------------------------------------------------

    def _register_demo(self, address: str, registration: str, cn: str, model: str) -> None:
        self.ddb.add_local(address, DdbInfo(registration, cn, model, True, True))

    def _start_demo(self) -> None:
        from .demo import seed_history

        self.link_status.state = "demo"
        self.link_status.server = "simulator"
        self.simulator = Simulator(self.regions[0])
        self.simulator.on_register = self._register_demo
        if self.settings.elevation_enabled:
            self.simulator.ground = self.elevation.get

        def on_line(line: str) -> None:
            self.link_status.lines += 1
            self.link_status.last_line_at = utcnow().isoformat() + "Z"
            self._enqueue(line)

        self._thread("simulator", self.simulator.run_realtime, self.stop_event, on_line)

        with self.db.session() as session:
            has_flights = bool(session.scalar(select(func.count()).select_from(Flight)))
        if not has_flights:

            def seed() -> None:
                try:
                    seed_history(self, days=2, progress=self._seed_progress)
                finally:
                    self.seeding = None

            self._thread("demo-seed", seed)

    def _seed_progress(self, text: str) -> None:
        self.seeding = text

    # -- status -----------------------------------------------------------------------------

    def status(self) -> dict:
        s = self.link_status
        return {
            "version": __version__,
            "started_at": self.started_at.isoformat() + "Z",
            "demo": self.settings.demo,
            "seeding": self.seeding,
            "link": {
                "state": s.state if (self.client or self.settings.demo) else "disabled",
                "server": s.server,
                "connected_since": s.connected_since,
                "lines": s.lines,
                "last_line_at": s.last_line_at,
                "last_error": s.last_error,
                "reconnects": s.reconnects,
            },
            "queue": self.lines.qsize(),
            "dropped_lines": self.dropped_lines,
            "tracker": dict(self.tracker.counters),
            "tracked_types": list(self.settings.tracked_types),
            "received_sources": dict(self.tracker.sources.most_common()),
            "received_types": {
                AIRCRAFT_TYPES.get(t, ("", str(t)))[1]: n for t, n in self.tracker.types.most_common()
            },
            "received_types_untracked": sum(
                n for t, n in self.tracker.types.items() if t not in self.settings.tracked_types
            ),
            "unparsed_samples": list(self.tracker.unparsed_samples),
            "live_aircraft": sum(1 for st in self.tracker.states.values() if st.live and st.last is not None),
            "ddb_devices": len(self.ddb),
            "ddb_updated": self.ddb.last_update,
            "weather": {"last_run": self.weather.last_run, "last_error": self.weather.last_error},
        }
