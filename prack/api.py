"""HTTP API (FastAPI) and static web app."""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import mimetypes
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from . import __version__
from .config import Settings
from .export import FORMATS, FlightMeta, TrackPoint, filename, render, to_igc
from .models import Device, Fix, Flight, WeatherDay
from .ogn.constants import AIRCRAFT_TYPES, CATEGORIES, category_for
from .regions import Region
from .runtime import Runtime
from .tracking.stats import simplify_track

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
router = APIRouter(prefix="/api")

STATIC_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".json": "application/json",
}


def pin_static_types() -> None:
    """Python takes content types from the Windows registry, where ".js" is often "text/plain";
    browsers refuse to run ES modules served like that. Pin the types the web app needs."""
    for ext, content_type in STATIC_TYPES.items():
        mimetypes.add_type(content_type, ext)

# simplified tracks of active flights, recomputed at most every PREVIEW_TTL seconds
PREVIEW_TTL = 60.0
_preview_cache: dict[int, tuple[float, list]] = {}


def _rt(request: Request) -> Runtime:
    return request.app.state.runtime


def _epoch(ts: datetime | None) -> int | None:
    return None if ts is None else int(ts.replace(tzinfo=timezone.utc).timestamp())


def _region(rt: Runtime, region_id: str | None) -> Region:
    if region_id is None:
        return rt.regions[0]
    region = rt.regions_by_id.get(region_id)
    if region is None:
        raise HTTPException(404, f"Unknown region {region_id}")
    return region


def _parse_date(value: str | None, region: Region) -> date:
    if not value:
        return datetime.now(region.tz).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "date must be YYYY-MM-DD") from exc


def _display_name(device: Device) -> str:
    if device.pilot_name:
        return device.pilot_name
    reg = device.registration if device.identified else None
    cn = device.competition_id if device.identified else None
    if cn and reg:
        return f"{cn} {reg}"
    return reg or cn or device.callsign


def _flight_summary(f: Flight, d: Device) -> dict:
    start = f.takeoff_time or f.start_time
    end = f.landing_time or f.end_time
    return {
        "id": f.id,
        "device_id": d.id,
        "callsign": d.callsign,
        "address": d.address,
        "name": _display_name(d),
        "reg": d.registration if d.identified else None,
        "cn": d.competition_id if d.identified else None,
        "pilot": d.pilot_name,
        "model": d.model,
        "type": f.aircraft_type,
        "type_name": AIRCRAFT_TYPES.get(f.aircraft_type, ("", "Unknown"))[1],
        "cat": category_for(f.aircraft_type),
        "src": f.source,
        "region": f.region,
        "date": f.date.isoformat(),
        "status": f.status,
        "close_reason": f.close_reason,
        "airborne": f.airborne,
        "hidden": f.hidden,
        "notes": f.notes,
        "start": _epoch(f.start_time),
        "end": _epoch(f.end_time),
        "takeoff": _epoch(start),
        "landing": _epoch(f.landing_time),
        "duration_s": int((end - start).total_seconds()) if start and end else None,
        "fixes": f.fix_count,
        "max_alt": f.max_alt,
        "min_alt": f.min_alt,
        "max_agl": f.max_agl,
        "gain": f.alt_gain,
        "max_climb": f.max_climb,
        "max_sink": f.max_sink,
        "max_speed": f.max_speed,
        "distance_km": f.distance_km,
        "straight_km": f.straight_km,
        "max_from_start_km": f.max_from_start_km,
        "takeoff_pos": [f.takeoff_lon, f.takeoff_lat, f.takeoff_alt] if f.takeoff_lat is not None else None,
        "landing_pos": [f.landing_lon, f.landing_lat, f.landing_alt] if f.landing_lat is not None else None,
        "bbox": [f.min_lon, f.min_lat, f.max_lon, f.max_lat] if f.min_lat is not None else None,
    }


def _get_flight(session, flight_id: int) -> tuple[Flight, Device]:
    row = session.execute(
        select(Flight, Device).join(Device, Flight.device_id == Device.id).where(Flight.id == flight_id)
    ).first()
    if row is None:
        raise HTTPException(404, "Flight not found")
    return row[0], row[1]


def _track_points(session, flight_id: int) -> list[TrackPoint]:
    rows = session.execute(
        select(Fix.ts, Fix.lat, Fix.lon, Fix.alt, Fix.ground, Fix.speed, Fix.track, Fix.climb, Fix.receiver)
        .where(Fix.flight_id == flight_id)
        .order_by(Fix.ts)
    ).all()
    return [TrackPoint(*r) for r in rows]


def _meta(f: Flight, d: Device) -> FlightMeta:
    return FlightMeta(
        flight_id=f.id,
        callsign=d.callsign,
        address=d.address,
        aircraft_type=f.aircraft_type,
        source=f.source,
        registration=d.registration if d.identified else None,
        competition_id=d.competition_id if d.identified else None,
        model=d.model,
        pilot=d.pilot_name,
    )


# -- meta ---------------------------------------------------------------------------------


@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/config")
def config(request: Request) -> dict:
    rt = _rt(request)
    s = rt.settings
    return {
        "version": __version__,
        "demo": s.demo,
        "regions": [r.public() for r in rt.regions],
        "categories": CATEGORIES,
        "aircraft_types": {k: v[1] for k, v in AIRCRAFT_TYPES.items()},
        "tracked_types": list(s.tracked_types),
        "live_window_minutes": s.live_window_minutes,
        "terrain": {"tiles": ["/api/dem/{z}/{x}/{y}.png"], "encoding": "terrarium", "max_zoom": 14},
    }


@router.get("/status")
def status(request: Request) -> dict:
    rt = _rt(request)
    data = rt.status()
    with rt.db.session() as session:
        data["flights_total"] = session.scalar(select(func.count()).select_from(Flight))
        data["days_total"] = session.scalar(select(func.count(func.distinct(Flight.date))))
    return data


# -- live ---------------------------------------------------------------------------------


@router.get("/live")
def live(request: Request, since: int = 0, trail: bool = True) -> dict:
    data = _rt(request).tracker.live(since, trail)
    data["now"] = int(time.time())
    return data


@router.get("/live/stream")
async def live_stream(request: Request) -> StreamingResponse:
    tracker = _rt(request).tracker

    async def events():
        since = 0
        last_full = 0.0
        last_sent = time.monotonic()
        while not await request.is_disconnected():
            now = time.monotonic()
            full = now - last_full >= 60.0
            data = await asyncio.to_thread(tracker.live, 0 if full else since, full)
            if full:
                last_full = now
            if full or data["aircraft"] or data["removed"]:
                since = data["seq"]
                data["now"] = int(time.time())
                last_sent = now
                yield f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
            elif now - last_sent > 15:
                last_sent = now
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.5)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


# -- flights --------------------------------------------------------------------------------


@router.get("/flights")
def list_flights(
    request: Request,
    date_: str | None = Query(None, alias="date"),
    region: str | None = None,
    types: str | None = None,
    hidden: bool | None = None,
    airborne: bool | None = None,
) -> dict:
    rt = _rt(request)
    reg = _region(rt, region)
    day = _parse_date(date_, reg)
    stmt = (
        select(Flight, Device)
        .join(Device, Flight.device_id == Device.id)
        .where(Flight.date == day, Flight.region == reg.id)
        .order_by(Flight.start_time)
    )
    if types:
        stmt = stmt.where(Flight.aircraft_type.in_([int(t) for t in types.split(",") if t.strip()]))
    if hidden is not None:
        stmt = stmt.where(Flight.hidden.is_(hidden))
    if airborne is not None:
        stmt = stmt.where(Flight.airborne.is_(airborne))
    with rt.db.session() as session:
        rows = session.execute(stmt).all()
    return {"date": day.isoformat(), "region": reg.id, "flights": [_flight_summary(f, d) for f, d in rows]}


@router.get("/flights/{flight_id}")
def get_flight(request: Request, flight_id: int) -> dict:
    rt = _rt(request)
    with rt.db.session() as session:
        f, d = _get_flight(session, flight_id)
    data = _flight_summary(f, d)
    data["device"] = {
        "callsign": d.callsign,
        "address": d.address,
        "address_type": d.address_type,
        "source": d.source,
        "software": d.software_version,
        "hardware": d.hardware_version,
        "first_seen": _epoch(d.first_seen),
        "last_seen": _epoch(d.last_seen),
        "identified": d.identified,
    }
    region = rt.regions_by_id.get(f.region)
    weather = None
    if region and f.takeoff_lat is not None:
        weather = rt.weather.at_location(region, f.date, f.takeoff_lat, f.takeoff_lon, f.takeoff_time or f.start_time)
    data["weather"] = weather
    return data


@router.get("/flights/{flight_id}/track")
def get_track(request: Request, flight_id: int, fill: bool = True) -> dict:
    rt = _rt(request)
    with rt.db.session() as session, session.begin():
        f, _d = _get_flight(session, flight_id)
        if fill and not f.ground_filled:
            missing = rt.finalizer.fill_ground(session, flight_id)
            if missing == 0 and f.status == "closed":
                f.ground_filled = True
        points = _track_points(session, flight_id)
    t, lat, lon, alt, gnd, spd, vs, hdg = [], [], [], [], [], [], [], []
    prev = None
    for p in points:
        t.append(_epoch(p.ts))
        lat.append(round(p.lat, 6))
        lon.append(round(p.lon, 6))
        alt.append(round(p.alt, 1))
        gnd.append(p.ground)
        spd.append(p.speed)
        climb = p.climb
        if climb is None and prev is not None:
            dt = (p.ts - prev.ts).total_seconds()
            climb = round((p.alt - prev.alt) / dt, 2) if dt > 0 else None
        vs.append(climb)
        hdg.append(p.track)
        prev = p
    return {"id": flight_id, "t": t, "lat": lat, "lon": lon, "alt": alt, "gnd": gnd, "spd": spd, "vs": vs, "hdg": hdg}


class FlightPatch(BaseModel):
    hidden: bool | None = None
    notes: str | None = None


@router.patch("/flights/{flight_id}")
def patch_flight(request: Request, flight_id: int, patch: FlightPatch) -> dict:
    rt = _rt(request)
    with rt.db.session() as session, session.begin():
        f, d = _get_flight(session, flight_id)
        if patch.hidden is not None:
            f.hidden = patch.hidden
        if patch.notes is not None:
            f.notes = patch.notes[:4000] or None
        summary = _flight_summary(f, d)
    if patch.hidden is not None:
        rt.tracker.set_hidden(flight_id, patch.hidden)
    return summary


@router.get("/flights/{flight_id}/export.{fmt}")
def export_flight(request: Request, flight_id: int, fmt: str) -> Response:
    if fmt not in FORMATS:
        raise HTTPException(400, f"Unknown format, use one of {', '.join(FORMATS)}")
    rt = _rt(request)
    with rt.db.session() as session:
        f, d = _get_flight(session, flight_id)
        points = _track_points(session, flight_id)
    if not points:
        raise HTTPException(404, "Flight has no fixes")
    meta = _meta(f, d)
    media_type, _ext = FORMATS[fmt]
    return Response(
        render(fmt, meta, points),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename(meta, points, fmt)}"'},
    )


@router.get("/tracks")
def day_tracks(
    request: Request, date_: str | None = Query(None, alias="date"), region: str | None = None
) -> dict:
    """Simplified tracks of all flights of a day (for the overview map)."""
    rt = _rt(request)
    reg = _region(rt, region)
    day = _parse_date(date_, reg)
    with rt.db.session() as session:
        flights = session.execute(
            select(Flight.id, Flight.aircraft_type, Flight.hidden, Flight.airborne, Flight.status, Flight.preview).where(
                Flight.date == day, Flight.region == reg.id
            )
        ).all()
        tracks = []
        now = time.monotonic()
        for fid, atype, hidden, airborne, status_, preview in flights:
            if preview is None or status_ == "active":
                cached = _preview_cache.get(fid)
                if cached and cached[0] > now:
                    preview = cached[1]
                else:
                    rows = session.execute(
                        select(Fix.lon, Fix.lat, Fix.alt).where(Fix.flight_id == fid).order_by(Fix.ts)
                    ).all()
                    preview = simplify_track([tuple(r) for r in rows], tolerance_m=20, max_points=300)
                    _preview_cache[fid] = (now + PREVIEW_TTL, preview)
            tracks.append(
                {"id": fid, "type": atype, "cat": category_for(atype), "hidden": hidden, "airborne": airborne, "path": preview}
            )
    for fid in [k for k, (expires, _) in _preview_cache.items() if expires < now]:
        _preview_cache.pop(fid, None)
    return {"date": day.isoformat(), "region": reg.id, "tracks": tracks}


# -- calendar -------------------------------------------------------------------------------


@router.get("/days")
def days(request: Request, start: str | None = None, end: str | None = None, region: str | None = None) -> dict:
    rt = _rt(request)
    reg = _region(rt, region)
    today = datetime.now(reg.tz).date()
    end_d = date.fromisoformat(end) if end else today
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=41)
    with rt.db.session() as session:
        rows = session.execute(
            select(Flight.date, Flight.aircraft_type, Flight.hidden, func.count())
            .where(Flight.region == reg.id, Flight.date >= start_d, Flight.date <= end_d, Flight.airborne.is_(True))
            .group_by(Flight.date, Flight.aircraft_type, Flight.hidden)
        ).all()
        weather_days = set(
            session.scalars(
                select(WeatherDay.date)
                .where(WeatherDay.region == reg.id, WeatherDay.date >= start_d, WeatherDay.date <= end_d)
                .distinct()
            ).all()
        )
    result: dict[date, dict] = {}
    for day, atype, hidden, count in rows:
        entry = result.setdefault(day, {"date": day.isoformat(), "total": 0, "hidden": 0, "pg": 0, "hg": 0, "gl": 0, "ot": 0})
        entry["total"] += count
        entry[category_for(atype)] += count
        if hidden:
            entry["hidden"] += count
    for day in weather_days:
        result.setdefault(day, {"date": day.isoformat(), "total": 0, "hidden": 0, "pg": 0, "hg": 0, "gl": 0, "ot": 0})
    for day, entry in result.items():
        entry["weather"] = day in weather_days
    return {
        "region": reg.id,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "today": today.isoformat(),
        "days": [result[d] for d in sorted(result)],
    }


@router.get("/days/{day}/export.zip")
def export_day(request: Request, day: str, region: str | None = None, include_hidden: bool = False) -> Response:
    rt = _rt(request)
    reg = _region(rt, region)
    the_day = _parse_date(day, reg)
    buffer = io.BytesIO()
    with rt.db.session() as session:
        stmt = (
            select(Flight, Device)
            .join(Device, Flight.device_id == Device.id)
            .where(Flight.date == the_day, Flight.region == reg.id, Flight.airborne.is_(True))
            .order_by(Flight.start_time)
        )
        if not include_hidden:
            stmt = stmt.where(Flight.hidden.is_(False))
        rows = session.execute(stmt).all()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            index = io.StringIO()
            writer = csv.writer(index)
            writer.writerow(["flight_id", "callsign", "name", "type", "source", "takeoff_utc", "landing_utc",
                             "duration_min", "max_alt_m", "distance_km", "file"])
            for f, d in rows:
                points = _track_points(session, f.id)
                if not points:
                    continue
                meta = _meta(f, d)
                name = filename(meta, points, "igc")
                zf.writestr(f"igc/{name}", to_igc(meta, points))
                summary = _flight_summary(f, d)
                writer.writerow([
                    f.id, d.callsign, summary["name"], summary["type_name"], f.source,
                    (f.takeoff_time or f.start_time).isoformat() + "Z", (f.landing_time or f.end_time).isoformat() + "Z",
                    round((summary["duration_s"] or 0) / 60), f.max_alt, f.distance_km, f"igc/{name}",
                ])
            zf.writestr("flights.csv", index.getvalue())
            zf.writestr("weather.json", json.dumps(rt.weather.day_report(reg, the_day), default=str, indent=1))
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="prack-{reg.id}-{the_day.isoformat()}.zip"'},
    )


# -- weather --------------------------------------------------------------------------------


@router.get("/weather/{day}")
def weather_day(request: Request, day: str, region: str | None = None, hourly: bool = True) -> dict:
    rt = _rt(request)
    reg = _region(rt, region)
    return rt.weather.day_report(reg, _parse_date(day, reg), hourly)


@router.post("/weather/{day}/fetch")
def weather_fetch(request: Request, day: str, region: str | None = None) -> dict:
    rt = _rt(request)
    reg = _region(rt, region)
    the_day = _parse_date(day, reg)
    try:
        rt.weather.fetch_day(reg, the_day)
    except Exception as exc:  # noqa: BLE001
        if not rt.settings.demo:
            raise HTTPException(502, f"Weather provider unavailable: {exc}") from exc
        from .demo import synthetic_weather

        rt.weather.store(reg, the_day, synthetic_weather(reg, the_day), "demo (synthetic)", "demo")
    return rt.weather.day_report(reg, the_day)


# -- terrain tiles ---------------------------------------------------------------------------


@router.get("/dem/{z}/{x}/{y}.png")
def dem_tile(request: Request, z: int, x: int, y: int) -> Response:
    if not (0 <= z <= 15 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(404)
    data = _rt(request).elevation.tile_png(z, x, y)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=2592000"})


# -- app ------------------------------------------------------------------------------------


class BasicAuthMiddleware:
    """Protects everything except the health check with HTTP basic auth."""

    def __init__(self, app, username: str, password: str) -> None:
        self.app = app
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.expected = f"Basic {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/api/health":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        if secrets.compare_digest(header, self.expected):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"detail": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="prack"'}
        )
        await response(scope, receive, send)


def create_app(settings: Settings | None = None, runtime: Runtime | None = None, start: bool = True) -> FastAPI:
    settings = settings or (runtime.settings if runtime else Settings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime(settings)
        app.state.runtime = rt
        if start:
            rt.start()
        try:
            yield
        finally:
            if start:
                rt.stop()

    app = FastAPI(title="prack", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    if runtime is not None:
        app.state.runtime = runtime
    app.include_router(router)
    if settings.auth_enabled:
        app.add_middleware(BasicAuthMiddleware, username=settings.auth_user, password=settings.auth_password)

    pin_static_types()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
