"""Turns the stream of OGN beacons into flights.

Per aircraft a small state machine runs:

* **on ground / not flying**: fixes are only kept in a short pre-takeoff buffer
* **takeoff**: two consecutive fixes above the type specific takeoff speed open
  a flight inside a configured region (the buffered fixes of the last minute
  are stored as well)
* **flying**: fixes are stored (at most one per ``min_fix_interval``)
* **landing**: stationary on the ground for ``landing_minutes`` closes the flight
* **gap**: no data for ``flight_gap_minutes`` closes the flight; if the aircraft
  shows up again airborne within ``flight_resume_minutes`` the same flight is
  continued (typical for coverage holes in the Alps)

Closed flights are handed to the :class:`Finalizer` which fills in terrain
elevation below every fix and computes the final statistics.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import delete, func, select, update

from ..config import Settings
from ..db import Database, utcnow
from ..elevation import ElevationService
from ..models import Device, Fix, Flight
from ..ogn.constants import MAX_TYPE_SPEED_KMH, category_for
from ..ogn.ddb import DeviceDatabase
from ..ogn.parser import AircraftBeacon, StatusBeacon, parse_line, unparsed_aircraft_candidate
from ..regions import Region, region_for
from .geo import distance_m
from .stats import FixPoint, compute_stats, simplify_track

log = logging.getLogger(__name__)

TAKEOFF_SPEED_KMH = {7: 15.0, 6: 18.0, 1: 35.0}
DEFAULT_TAKEOFF_SPEED_KMH = 30.0
STATIONARY_SPEED_KMH = 5.0
STATIONARY_RADIUS_M = 150.0
LANDED_MAX_AGL_M = 80.0
MAX_SPEED_KMH = 500.0
PRE_TAKEOFF_BUFFER_S = 60.0
FLIGHT_UPDATE_SECONDS = 10.0
TRAIL_POINTS = 360
SPEED_CHECK_WINDOW = 10  # recent fixes considered by the speed plausibility check
SPEED_CHECK_LIMIT = 2  # implausibly fast fixes within the window that disqualify an aircraft
ALIAS_MAX_DISTANCE_M = 3000.0  # same address, different protocol (FLARM / ADS-L / ...): same aircraft
ALIAS_MAX_AGE = timedelta(minutes=10)


def takeoff_speed(aircraft_type: int) -> float:
    return TAKEOFF_SPEED_KMH.get(aircraft_type, DEFAULT_TAKEOFF_SPEED_KMH)


def local_date(region: Region, ts: datetime):
    return ts.replace(tzinfo=timezone.utc).astimezone(region.tz).date()


def epoch(ts: datetime) -> int:
    return int(ts.replace(tzinfo=timezone.utc).timestamp())


@dataclass
class FlightAcc:
    """Incrementally maintained statistics of an open flight (for the live list)."""

    flight_id: int
    region: str
    start_time: datetime
    end_time: datetime
    fix_count: int = 0
    max_alt: float = -1e9
    min_alt: float = 1e9
    max_climb: float = 0.0
    max_sink: float = 0.0
    max_speed: float = 0.0
    distance_m: float = 0.0
    max_agl: float | None = None
    alt_gain: float = 0.0
    last_alt: float | None = None
    takeoff: tuple[float, float, float] | None = None
    last_pos: tuple[float, float] | None = None
    bbox: list[float] = field(default_factory=lambda: [90.0, -90.0, 180.0, -180.0])  # minlat maxlat minlon maxlon
    dirty: bool = True

    def add(self, b: AircraftBeacon, ground: float | None = None) -> None:
        self.fix_count += 1
        if ground is not None:
            agl = b.alt - ground
            self.max_agl = agl if self.max_agl is None else max(self.max_agl, agl)
        if self.last_alt is not None and b.alt > self.last_alt:
            self.alt_gain += b.alt - self.last_alt
        self.last_alt = b.alt
        self.end_time = b.timestamp
        self.max_alt = max(self.max_alt, b.alt)
        self.min_alt = min(self.min_alt, b.alt)
        if b.climb is not None and abs(b.climb) < 30:
            self.max_climb = max(self.max_climb, b.climb)
            self.max_sink = min(self.max_sink, b.climb)
        if b.speed is not None and b.speed < MAX_SPEED_KMH:
            self.max_speed = max(self.max_speed, b.speed)
        if self.takeoff is None:
            self.takeoff = (b.lat, b.lon, b.alt)
        if self.last_pos is not None:
            self.distance_m += distance_m(self.last_pos[0], self.last_pos[1], b.lat, b.lon)
        self.last_pos = (b.lat, b.lon)
        self.bbox = [
            min(self.bbox[0], b.lat), max(self.bbox[1], b.lat),
            min(self.bbox[2], b.lon), max(self.bbox[3], b.lon),
        ]
        self.dirty = True

    def row_values(self) -> dict:
        values = {
            "end_time": self.end_time,
            "fix_count": self.fix_count,
            "max_climb": round(self.max_climb, 2),
            "max_sink": round(self.max_sink, 2),
            "max_speed": round(self.max_speed, 1),
            "distance_km": round(self.distance_m / 1000, 3),
            "alt_gain": round(self.alt_gain, 1),
            "updated_at": utcnow(),
        }
        if self.max_agl is not None:
            values["max_agl"] = round(self.max_agl, 1)
        if self.fix_count:
            values.update(
                max_alt=self.max_alt,
                min_alt=self.min_alt,
                min_lat=self.bbox[0], max_lat=self.bbox[1], min_lon=self.bbox[2], max_lon=self.bbox[3],
            )
        if self.takeoff:
            values.update(takeoff_lat=self.takeoff[0], takeoff_lon=self.takeoff[1], takeoff_alt=self.takeoff[2])
        if self.last_pos:
            values.update(landing_lat=self.last_pos[0], landing_lon=self.last_pos[1])
        return values


@dataclass
class DeviceState:
    callsign: str
    device_id: int
    address: str
    aircraft_type: int
    source: str
    registration: str | None = None
    competition_id: str | None = None
    model: str | None = None
    pilot_name: str | None = None
    last: AircraftBeacon | None = None
    ground: float | None = None
    flight: FlightAcc | None = None
    hidden: bool = False
    last_stored: datetime | None = None
    pre_buffer: deque = field(default_factory=lambda: deque(maxlen=120))
    moving_count: int = 0
    stationary_since: datetime | None = None
    stationary_anchor: tuple[float, float] | None = None
    rejected: int = 0
    speed_flags: deque = field(default_factory=lambda: deque(maxlen=SPEED_CHECK_WINDOW))
    misclassified: bool = False  # flies too fast for its declared aircraft type
    # last closed flight (to resume after a coverage gap)
    prev_flight_id: int | None = None
    prev_reason: str | None = None
    prev_end: datetime | None = None
    trail: deque = field(default_factory=lambda: deque(maxlen=TRAIL_POINTS))
    seq: int = 0
    live: bool = True

    def display_name(self) -> str:
        if self.pilot_name:
            return self.pilot_name
        if self.competition_id and self.registration:
            return f"{self.competition_id} {self.registration}"
        return self.registration or self.competition_id or self.callsign


class Tracker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        regions: list[Region],
        ddb: DeviceDatabase | None = None,
        elevation: ElevationService | None = None,
        on_flight_closed: Callable[[int], None] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.regions = regions
        self.regions_by_id = {r.id: r for r in regions}
        self.ddb = ddb
        self.elevation = elevation
        self.on_flight_closed = on_flight_closed
        self.states: dict[str, DeviceState] = {}
        self.lock = threading.RLock()
        self.pending: list[dict] = []
        self.seq = 0
        self.removed: deque[tuple[int, str]] = deque(maxlen=5000)
        self._last_flight_update = utcnow()
        self.counters = {
            "lines": 0,
            "beacons": 0,
            "fixes_stored": 0,
            "flights_opened": 0,
            "flights_closed": 0,
            "ignored_type": 0,
            "ignored_privacy": 0,
            "ignored_region": 0,
            "ignored_time": 0,
            "duplicates": 0,
            "glitches": 0,
            "unparsed": 0,
            "ignored_implausible": 0,
            "merged": 0,
        }
        self.by_address: dict[str, str] = {}  # 24 bit address -> callsign of the state
        # what arrives, before any filtering (diagnostics)
        self.sources: Counter[str] = Counter()
        self.types: Counter[int] = Counter()
        self.unparsed_samples: deque[str] = deque(maxlen=10)
        self.gap = timedelta(minutes=settings.flight_gap_minutes)
        self.resume = timedelta(minutes=settings.flight_resume_minutes)
        self.landing = timedelta(minutes=settings.landing_minutes)
        self.live_window = timedelta(minutes=settings.live_window_minutes)
        self.min_interval = timedelta(seconds=settings.min_fix_interval)
        self.tracked_types = set(settings.tracked_types)

    # -- input -----------------------------------------------------------------------

    def process_line(self, line: str, received: datetime | None = None) -> None:
        received = received or utcnow()
        self.counters["lines"] += 1
        parsed = parse_line(line, received)
        if isinstance(parsed, AircraftBeacon):
            self.process_beacon(parsed, received)
        elif isinstance(parsed, StatusBeacon):
            self.process_status(parsed)
        elif unparsed_aircraft_candidate(line):
            self.counters["unparsed"] += 1
            self.unparsed_samples.append(line.strip()[:200])

    def process_status(self, status: StatusBeacon) -> None:
        if not status.name:
            return
        with self.lock:
            state = self.states.get(status.callsign)
            if state is None or state.pilot_name == status.name:
                return
            state.pilot_name = status.name[:64]
            self._touch(state)
        with self.db.session() as session, session.begin():
            session.execute(update(Device).where(Device.id == state.device_id).values(pilot_name=state.pilot_name))

    def process_beacon(self, b: AircraftBeacon, received: datetime | None = None) -> None:
        received = received or b.timestamp
        self.counters["beacons"] += 1
        self.sources[b.source] += 1
        self.types[b.aircraft_type] += 1
        if b.aircraft_type not in self.tracked_types:
            self.counters["ignored_type"] += 1
            return
        if b.no_tracking or (b.stealth and self.settings.respect_stealth):
            self.counters["ignored_privacy"] += 1
            return
        info = self.ddb.lookup(b.address) if self.ddb else None
        if info is not None and not info.tracked:
            self.counters["ignored_privacy"] += 1
            return
        if b.timestamp - received > timedelta(minutes=2) or received - b.timestamp > timedelta(minutes=30):
            self.counters["ignored_time"] += 1
            return

        with self.lock:
            state = self.states.get(b.callsign) or self._same_aircraft(b)
            if state is None:
                if region_for(self.regions, b.lat, b.lon) is None:
                    self.counters["ignored_region"] += 1
                    return
                state = self._create_state(b, info)
                self.by_address[b.address] = b.callsign
            if state.misclassified:
                self.counters["ignored_implausible"] += 1
                return
            self._handle(state, b)

    def _same_aircraft(self, b: AircraftBeacon) -> DeviceState | None:
        """A device heard under another callsign with the same address (e.g. FLARM and ADS-L) nearby."""
        callsign = self.by_address.get(b.address)
        other = self.states.get(callsign) if callsign else None
        if other is None or other.last is None:
            return None
        if abs(b.timestamp - other.last.timestamp) > ALIAS_MAX_AGE:
            return None
        if distance_m(other.last.lat, other.last.lon, b.lat, b.lon) > ALIAS_MAX_DISTANCE_M:
            return None
        self.counters["merged"] += 1
        return other

    # -- state machine -----------------------------------------------------------------

    def _create_state(self, b: AircraftBeacon, info) -> DeviceState:
        now = utcnow()
        registration = competition_id = model = None
        identified = True
        if info is not None:
            identified = info.identified
            model = info.model
            if identified:
                registration, competition_id = info.registration, info.competition_id
        with self.db.session() as session, session.begin():
            device = session.execute(select(Device).where(Device.callsign == b.callsign)).scalar_one_or_none()
            if device is None:
                device = Device(callsign=b.callsign, address=b.address, first_seen=now, last_seen=now)
                session.add(device)
            device.address_type = b.address_type
            device.aircraft_type = b.aircraft_type
            device.source = b.source
            device.last_seen = now
            device.identified = identified
            if info is not None:
                device.registration, device.competition_id, device.model = registration, competition_id, model
            if b.software:
                device.software_version = b.software
            if b.hardware:
                device.hardware_version = b.hardware
            session.flush()
            device_id = device.id
            pilot_name = device.pilot_name
        state = DeviceState(
            callsign=b.callsign,
            device_id=device_id,
            address=b.address,
            aircraft_type=b.aircraft_type,
            source=b.source,
            registration=registration,
            competition_id=competition_id,
            model=model,
            pilot_name=pilot_name,
        )
        self.states[b.callsign] = state
        return state

    def _touch(self, state: DeviceState) -> None:
        self.seq += 1
        state.seq = self.seq

    def _handle(self, state: DeviceState, b: AircraftBeacon) -> None:
        last = state.last
        if last is not None:
            if b.timestamp <= last.timestamp:
                self.counters["duplicates"] += 1
                return
            dt = (b.timestamp - last.timestamp).total_seconds()
            d = distance_m(last.lat, last.lon, b.lat, b.lon)
            if d > 2000 and d / dt * 3.6 > MAX_SPEED_KMH and state.rejected < 3:
                state.rejected += 1
                self.counters["glitches"] += 1
                return
        state.rejected = 0
        if b.speed is None and last is not None:
            dt = (b.timestamp - last.timestamp).total_seconds()
            b.speed = distance_m(last.lat, last.lon, b.lat, b.lon) / dt * 3.6 if dt > 0 else 0.0
        limit = MAX_TYPE_SPEED_KMH.get(b.aircraft_type)
        if limit is not None and b.speed is not None:
            too_fast = b.speed > limit
            state.speed_flags.append(too_fast)
            if too_fast:
                if sum(state.speed_flags) >= SPEED_CHECK_LIMIT:
                    self._disqualify(state, b)
                else:
                    self.counters["glitches"] += 1
                return
        state.last = b
        state.aircraft_type = b.aircraft_type
        state.live = True
        if self.elevation is not None:
            ground = self.elevation.get_cached(b.lat, b.lon)
            state.ground = ground
        state.trail.append((b.timestamp, b.lat, b.lon, b.alt))
        self._touch(state)

        if state.flight is not None and b.timestamp - state.flight.end_time > self.gap:
            self._close(state, "gap", state.flight.end_time)

        if state.flight is None:
            self._maybe_takeoff(state, b)
        else:
            self._store(state, b)
            self._check_landing(state, b)

    def _disqualify(self, state: DeviceState, b: AircraftBeacon) -> None:
        """The aircraft is much faster than its declared type allows: drop it and its open flight."""
        log.info("%s reports aircraft type %s but flies %.0f km/h: ignored", state.callsign, b.aircraft_type, b.speed)
        state.misclassified = True
        self.counters["ignored_implausible"] += 1
        if state.flight is not None:
            self._discard_flight(state.flight.flight_id)
            state.flight = None
        state.prev_flight_id = None
        if state.live:
            state.live = False
            self.seq += 1
            self.removed.append((self.seq, state.callsign))

    def _discard_flight(self, flight_id: int) -> None:
        self.flush(force=True)
        with self.db.session() as session, session.begin():
            session.execute(delete(Fix).where(Fix.flight_id == flight_id))
            session.execute(delete(Flight).where(Flight.id == flight_id))

    def purge_implausible(self) -> int:
        """Delete stored flights that cannot be what their aircraft type says (ADS-B "paragliders",
        "paragliders" at 300 km/h). Returns the number of deleted flights."""
        doomed: list[int] = []
        with self.db.session() as session:
            for aircraft_type, limit in MAX_TYPE_SPEED_KMH.items():
                rows = session.execute(
                    select(Flight.id, Flight.source).where(
                        Flight.aircraft_type == aircraft_type,
                        (Flight.source == "ADS-B") | (Flight.max_speed > limit),
                    )
                ).all()
                for flight_id, source in rows:
                    fast = session.scalar(
                        select(func.count()).select_from(Fix).where(Fix.flight_id == flight_id, Fix.speed > limit)
                    )
                    if source == "ADS-B" or fast >= SPEED_CHECK_LIMIT:
                        doomed.append(flight_id)
        for flight_id in doomed:
            self._discard_flight(flight_id)
        if doomed:
            log.info("Deleted %d stored flights with an implausible aircraft type", len(doomed))
        return len(doomed)

    def _maybe_takeoff(self, state: DeviceState, b: AircraftBeacon) -> None:
        state.pre_buffer.append(b)
        if (b.speed or 0.0) >= takeoff_speed(b.aircraft_type):
            state.moving_count += 1
        else:
            state.moving_count = 0
            return
        if state.moving_count < 2:
            return
        # airborne again shortly after a coverage gap: continue that flight
        if (
            state.prev_flight_id is not None
            and state.prev_reason == "gap"
            and state.prev_end is not None
            and b.timestamp - state.prev_end <= self.resume
        ):
            self._resume(state, b)
            return
        region = region_for(self.regions, b.lat, b.lon)
        if region is None:
            return
        self._open(state, region, b)

    def _open(self, state: DeviceState, region: Region, b: AircraftBeacon) -> None:
        cutoff = b.timestamp - timedelta(seconds=PRE_TAKEOFF_BUFFER_S)
        buffered = [f for f in state.pre_buffer if f.timestamp >= cutoff]
        start = buffered[0].timestamp if buffered else b.timestamp
        now = utcnow()
        with self.db.session() as session, session.begin():
            flight = Flight(
                device_id=state.device_id,
                region=region.id,
                date=local_date(region, start),
                aircraft_type=b.aircraft_type,
                source=b.source,
                status="active",
                start_time=start,
                end_time=start,
                takeoff_time=b.timestamp,
                created_at=now,
                updated_at=now,
            )
            session.add(flight)
            session.flush()
            flight_id = flight.id
        state.flight = FlightAcc(flight_id=flight_id, region=region.id, start_time=start, end_time=start)
        state.hidden = False
        state.last_stored = None
        state.stationary_since = None
        state.pre_buffer.clear()
        self.counters["flights_opened"] += 1
        for f in buffered:
            self._store(state, f)
        log.debug("Flight %s opened for %s", flight_id, state.callsign)

    def _resume(self, state: DeviceState, b: AircraftBeacon) -> None:
        flight_id = state.prev_flight_id
        self.flush(force=True)
        with self.db.session() as session, session.begin():
            flight = session.get(Flight, flight_id)
            if flight is None:
                state.prev_flight_id = None
                return
            flight.status = "active"
            flight.close_reason = None
            flight.landing_time = None
            flight.updated_at = utcnow()
            acc = FlightAcc(
                flight_id=flight.id,
                region=flight.region,
                start_time=flight.start_time,
                end_time=flight.end_time,
                fix_count=flight.fix_count or 0,
                max_alt=flight.max_alt if flight.max_alt is not None else -1e9,
                min_alt=flight.min_alt if flight.min_alt is not None else 1e9,
                max_climb=flight.max_climb or 0.0,
                max_sink=flight.max_sink or 0.0,
                max_speed=flight.max_speed or 0.0,
                distance_m=(flight.distance_km or 0.0) * 1000,
                max_agl=flight.max_agl,
                alt_gain=flight.alt_gain or 0.0,
                takeoff=(flight.takeoff_lat, flight.takeoff_lon, flight.takeoff_alt)
                if flight.takeoff_lat is not None
                else None,
                last_pos=(flight.landing_lat, flight.landing_lon) if flight.landing_lat is not None else None,
            )
            if flight.min_lat is not None:
                acc.bbox = [flight.min_lat, flight.max_lat, flight.min_lon, flight.max_lon]
            hidden = flight.hidden
        state.flight = acc
        state.hidden = hidden
        state.prev_flight_id = None
        state.last_stored = None
        state.stationary_since = None
        state.pre_buffer.clear()
        self._store(state, b)
        log.debug("Flight %s resumed for %s", flight_id, state.callsign)

    def _store(self, state: DeviceState, b: AircraftBeacon) -> None:
        acc = state.flight
        if acc is None:
            return
        if state.last_stored is not None and b.timestamp - state.last_stored < self.min_interval:
            acc.end_time = max(acc.end_time, b.timestamp)
            return
        state.last_stored = b.timestamp
        ground = state.ground if b is state.last else None
        acc.add(b, ground)
        self.pending.append(
            {
                "flight_id": acc.flight_id,
                "ts": b.timestamp,
                "lat": round(b.lat, 6),
                "lon": round(b.lon, 6),
                "alt": round(b.alt, 1),
                "ground": ground,
                "speed": round(b.speed, 1) if b.speed is not None else None,
                "track": b.track,
                "climb": round(b.climb, 2) if b.climb is not None else None,
                "turn": round(b.turn, 1) if b.turn is not None else None,
                "receiver": (b.receiver or None) and b.receiver[:16],
                "signal": b.signal,
                "errors": b.errors,
                "freq_offset": b.freq_offset,
                "gps": b.gps,
                "raw": b.raw if self.settings.store_raw else None,
            }
        )

    def _check_landing(self, state: DeviceState, b: AircraftBeacon) -> None:
        if (b.speed or 0.0) >= STATIONARY_SPEED_KMH:
            state.stationary_since = None
            return
        if state.stationary_since is None or state.stationary_anchor is None or distance_m(
            state.stationary_anchor[0], state.stationary_anchor[1], b.lat, b.lon
        ) > STATIONARY_RADIUS_M:
            state.stationary_since = b.timestamp
            state.stationary_anchor = (b.lat, b.lon)
            return
        if b.timestamp - state.stationary_since < self.landing:
            return
        if state.ground is not None and b.alt - state.ground > LANDED_MAX_AGL_M:
            return  # hovering in strong wind, not landed
        if state.ground is None and b.climb is not None and abs(b.climb) > 0.5:
            return
        self._close(state, "landed", state.stationary_since)

    def _close(self, state: DeviceState, reason: str, end: datetime) -> None:
        acc = state.flight
        if acc is None:
            return
        self.flush(force=True)
        with self.db.session() as session, session.begin():
            values = acc.row_values()
            values.update(status="closed", close_reason=reason, landing_time=end)
            session.execute(update(Flight).where(Flight.id == acc.flight_id).values(**values))
        state.prev_flight_id = acc.flight_id
        state.prev_reason = reason
        state.prev_end = acc.end_time
        state.flight = None
        state.moving_count = 0
        state.stationary_since = None
        state.pre_buffer.clear()
        self.counters["flights_closed"] += 1
        self._touch(state)
        log.debug("Flight %s closed (%s)", acc.flight_id, reason)
        if self.on_flight_closed:
            self.on_flight_closed(acc.flight_id)

    # -- persistence -----------------------------------------------------------------

    def flush(self, force: bool = False) -> None:
        """Write buffered fixes (and, every few seconds, flight summaries) to the database."""
        with self.lock:
            fixes, self.pending = self.pending, []
            now = utcnow()
            flights = []
            if force or (now - self._last_flight_update).total_seconds() >= FLIGHT_UPDATE_SECONDS:
                self._last_flight_update = now
                for state in self.states.values():
                    if state.flight is not None and state.flight.dirty:
                        flights.append((state.flight.flight_id, state.flight.row_values()))
                        state.flight.dirty = False
        if not fixes and not flights:
            return
        with self.db.session() as session, session.begin():
            if fixes:
                session.execute(self.db.insert_ignore(Fix), fixes)
            for flight_id, values in flights:
                session.execute(update(Flight).where(Flight.id == flight_id).values(**values))
        self.counters["fixes_stored"] += len(fixes)

    def sweep(self, now: datetime | None = None) -> None:
        """Close silent flights and drop aircraft that left the live window."""
        now = now or utcnow()
        with self.lock:
            for callsign, state in list(self.states.items()):
                if state.flight is not None and now - state.flight.end_time > self.gap:
                    self._close(state, "gap", state.flight.end_time)
                last_ts = state.last.timestamp if state.last else None
                if state.live and (last_ts is None or now - last_ts > self.live_window):
                    state.live = False
                    self.seq += 1
                    self.removed.append((self.seq, callsign))
                idle_since = last_ts or state.prev_end
                if state.flight is None and (idle_since is None or now - idle_since > self.resume + timedelta(hours=1)):
                    del self.states[callsign]

    def restore(self) -> list[int]:
        """Close flights left open by a previous run; they resume if the aircraft is still airborne."""
        closed = []
        with self.db.session() as session, session.begin():
            rows = session.execute(
                select(Flight, Device).join(Device, Flight.device_id == Device.id).where(Flight.status == "active")
            ).all()
            for flight, device in rows:
                flight.status = "closed"
                flight.close_reason = "gap"
                flight.landing_time = flight.end_time
                closed.append(flight.id)
                self.states[device.callsign] = DeviceState(
                    callsign=device.callsign,
                    device_id=device.id,
                    address=device.address,
                    aircraft_type=device.aircraft_type,
                    source=device.source,
                    registration=device.registration if device.identified else None,
                    competition_id=device.competition_id if device.identified else None,
                    model=device.model,
                    pilot_name=device.pilot_name,
                    prev_flight_id=flight.id,
                    prev_reason="gap",
                    prev_end=flight.end_time,
                    live=False,
                )
                self.by_address[device.address] = device.callsign
        for flight_id in closed:
            if self.on_flight_closed:
                self.on_flight_closed(flight_id)
        return closed

    def close_all(self) -> None:
        with self.lock:
            for state in list(self.states.values()):
                if state.flight is not None:
                    self._close(state, "gap", state.flight.end_time)
        self.flush(force=True)

    # -- API helpers ---------------------------------------------------------------------

    def set_hidden(self, flight_id: int, hidden: bool) -> None:
        with self.lock:
            for state in self.states.values():
                if state.flight is not None and state.flight.flight_id == flight_id:
                    state.hidden = hidden
                    self._touch(state)

    def _live_entry(self, state: DeviceState, trail: bool) -> dict:
        b = state.last
        assert b is not None
        entry = {
            "id": state.callsign,
            "device_id": state.device_id,
            "flight_id": state.flight.flight_id if state.flight else None,
            "type": state.aircraft_type,
            "cat": category_for(state.aircraft_type),
            "src": state.source,
            "name": state.display_name(),
            "reg": state.registration,
            "cn": state.competition_id,
            "pilot": state.pilot_name,
            "model": state.model,
            "t": epoch(b.timestamp),
            "lat": round(b.lat, 6),
            "lon": round(b.lon, 6),
            "alt": round(b.alt),
            "gnd": round(state.ground) if state.ground is not None else None,
            "spd": round(b.speed, 1) if b.speed is not None else None,
            "vs": round(b.climb, 1) if b.climb is not None else None,
            "hdg": b.track,
            "rx": b.receiver,
            "flying": state.flight is not None,
            "hidden": state.hidden,
            "takeoff": epoch(state.flight.start_time) if state.flight else None,
        }
        if trail:
            entry["trail"] = [
                [round(lon, 6), round(lat, 6), round(alt)] for (_ts, lat, lon, alt) in list(state.trail)[::2]
            ] + [[round(b.lon, 6), round(b.lat, 6), round(b.alt)]]
        return entry

    def live(self, since: int = 0, trail: bool = True) -> dict:
        """Aircraft changed since ``since`` (sequence number) plus removals."""
        with self.lock:
            aircraft = [
                self._live_entry(s, trail)
                for s in self.states.values()
                if s.live and s.last is not None and s.seq > since
            ]
            removed = [cs for (seq, cs) in self.removed if seq > since] if since else []
            return {"seq": self.seq, "aircraft": aircraft, "removed": removed, "full": since == 0}


class Finalizer:
    """Fills terrain elevation and final statistics for closed flights (background thread)."""

    def __init__(self, db: Database, elevation: ElevationService | None) -> None:
        self.db = db
        self.elevation = elevation
        self.queue: queue.Queue[int] = queue.Queue()

    def enqueue(self, flight_id: int) -> None:
        self.queue.put(flight_id)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                flight_id = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.finalize(flight_id)
            except Exception:  # noqa: BLE001
                log.exception("Finalizing flight %s failed", flight_id)

    def drain(self) -> None:
        while True:
            try:
                flight_id = self.queue.get_nowait()
            except queue.Empty:
                return
            self.finalize(flight_id)

    def fill_ground(self, session, flight_id: int) -> int:
        """Look up terrain elevation for fixes that have none. Returns the number still missing."""
        if self.elevation is None:
            return -1
        rows = session.execute(
            select(Fix.ts, Fix.lat, Fix.lon).where(Fix.flight_id == flight_id, Fix.ground.is_(None))
        ).all()
        if not rows:
            return 0
        grounds = self.elevation.get_many([(r.lat, r.lon) for r in rows])
        updates = [{"flight_id": flight_id, "ts": r.ts, "ground": g} for r, g in zip(rows, grounds) if g is not None]
        if updates:
            session.execute(update(Fix), updates)
        return len(rows) - len(updates)

    def finalize(self, flight_id: int) -> None:
        with self.db.session() as session, session.begin():
            flight = session.get(Flight, flight_id)
            if flight is None:
                return
            missing = self.fill_ground(session, flight_id)
            rows = session.execute(
                select(Fix.ts, Fix.lat, Fix.lon, Fix.alt, Fix.ground, Fix.speed, Fix.climb)
                .where(Fix.flight_id == flight_id)
                .order_by(Fix.ts)
            ).all()
            if not rows:
                session.delete(flight)
                return
            stats = compute_stats(
                [FixPoint(r.ts, r.lat, r.lon, r.alt, r.ground, r.speed, r.climb) for r in rows],
                takeoff_speed(flight.aircraft_type),
            )
            if flight.status == "closed" and flight.close_reason == "landed" and flight.landing_time:
                stats["landing_time"] = flight.landing_time
            elif flight.status == "closed":
                stats["landing_time"] = stats["end_time"]
            for key, value in stats.items():
                setattr(flight, key, value)
            flight.preview = simplify_track([(r.lon, r.lat, r.alt) for r in rows])
            flight.ground_filled = missing == 0
            flight.updated_at = utcnow()
