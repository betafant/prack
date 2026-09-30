"""Daily weather archive (Open-Meteo, free, no API key).

For every region the weather at a handful of representative points is stored
per day: hourly values (surface, clouds, boundary layer, CAPE, winds and
temperatures at 850/700/500 hPa, ...) plus a daily summary. Pressure
differences between points (e.g. the Lugano-Zürich föhn index) are derived on
the fly. This makes it possible to compare flights against the weather of the
day later on.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select

from .db import Database, utcnow
from .models import Flight, WeatherDay
from .regions import Region
from .tracking.geo import distance_m

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_START = date(2022, 1, 1)

BASIC_HOURLY = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "weather_code",
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "pressure_msl",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "shortwave_radiation",
    "sunshine_duration", "boundary_layer_height",
]
FULL_HOURLY = BASIC_HOURLY + [
    "cape", "lifted_index", "convective_inhibition", "freezing_level_height",
    "temperature_850hPa", "temperature_700hPa", "temperature_500hPa",
    "wind_speed_850hPa", "wind_direction_850hPa", "wind_speed_700hPa", "wind_direction_700hPa",
    "wind_speed_500hPa", "wind_direction_500hPa",
    "geopotential_height_850hPa", "geopotential_height_700hPa", "geopotential_height_500hPa",
    "relative_humidity_850hPa", "relative_humidity_700hPa",
]
DAILY = [
    "weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum", "sunshine_duration",
    "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant", "shortwave_radiation_sum",
    "sunrise", "sunset",
]

DAYTIME_HOURS = range(10, 18)
REFERENCE_HOUR = 13


def _hour_of(time_str: str) -> int:
    return int(time_str[11:13])


def _values(hourly: dict, key: str, hours) -> list[float]:
    series = hourly.get(key) or []
    times = hourly.get("time") or []
    return [v for t, v in zip(times, series) if v is not None and _hour_of(t) in hours]


def _at(hourly: dict, key: str, hour: int):
    series = hourly.get(key) or []
    for t, v in zip(hourly.get("time") or [], series):
        if _hour_of(t) == hour:
            return v
    return None


def _first(daily: dict, key: str):
    series = daily.get(key) or []
    return series[0] if series else None


def _r(value, digits: int = 1):
    return None if value is None else round(value, digits)


def summarize(hourly: dict, daily: dict, elevation: float | None) -> dict:
    """Key figures of a flying day at one point."""
    day = DAYTIME_HOURS
    h = REFERENCE_HOUR

    def mx(key):
        vals = _values(hourly, key, day)
        return max(vals) if vals else None

    def mn(key):
        vals = _values(hourly, key, day)
        return min(vals) if vals else None

    def mean(key):
        vals = _values(hourly, key, day)
        return sum(vals) / len(vals) if vals else None

    t14, td14 = _at(hourly, "temperature_2m", 14), _at(hourly, "dew_point_2m", 14)
    cloudbase = None
    if t14 is not None and td14 is not None:
        cloudbase = 125.0 * (t14 - td14) + (elevation or 0.0)  # Espy's approximation

    t850, t500 = _at(hourly, "temperature_850hPa", h), _at(hourly, "temperature_500hPa", h)
    z850, z500 = _at(hourly, "geopotential_height_850hPa", h), _at(hourly, "geopotential_height_500hPa", h)
    lapse = None
    if None not in (t850, t500, z850, z500) and z500 != z850:
        lapse = (t850 - t500) / ((z500 - z850) / 1000.0)

    def wind(level):
        speed = _at(hourly, f"wind_speed_{level}", h)
        direction = _at(hourly, f"wind_direction_{level}", h)
        return None if speed is None else {"speed": _r(speed), "dir": _r(direction, 0)}

    sunshine = _first(daily, "sunshine_duration")
    return {
        "weather_code": _first(daily, "weather_code"),
        "t_max": _r(_first(daily, "temperature_2m_max")),
        "t_min": _r(_first(daily, "temperature_2m_min")),
        "precip_mm": _r(_first(daily, "precipitation_sum")),
        "sunshine_h": _r(sunshine / 3600.0) if sunshine is not None else None,
        "wind_max": _r(_first(daily, "wind_speed_10m_max")),
        "gust_max": _r(_first(daily, "wind_gusts_10m_max")),
        "wind_dir": _r(_first(daily, "wind_direction_10m_dominant"), 0),
        "radiation_mj": _r(_first(daily, "shortwave_radiation_sum")),
        "cloud_mean": _r(mean("cloud_cover"), 0),
        "cloud_low_mean": _r(mean("cloud_cover_low"), 0),
        "blh_max": _r(mx("boundary_layer_height"), 0),
        "cape_max": _r(mx("cape"), 0),
        "lifted_index_min": _r(mn("lifted_index")),
        "cloudbase_14": _r(cloudbase, 0),
        "freezing_level": _r(_at(hourly, "freezing_level_height", h), 0),
        "t850": _r(t850),
        "t700": _r(_at(hourly, "temperature_700hPa", h)),
        "t500": _r(t500),
        "lapse_850_500": _r(lapse, 2),
        "wind_850": wind("850hPa"),
        "wind_700": wind("700hPa"),
        "wind_500": wind("500hPa"),
        "pressure_msl": _r(_at(hourly, "pressure_msl", h)),
        "sunrise": _first(daily, "sunrise"),
        "sunset": _first(daily, "sunset"),
    }


class WeatherService:
    def __init__(self, db: Database, regions: list[Region], interval_hours: float = 3.0, backfill_days: int = 30):
        self.db = db
        self.regions = {r.id: r for r in regions}
        self.interval = timedelta(hours=interval_hours)
        self.backfill_days = backfill_days
        self.last_run: str | None = None
        self.last_error: str | None = None

    # -- fetching -------------------------------------------------------------------

    def _request(self, region: Region, day: date, hourly: list[str]) -> list[dict]:
        today = datetime.now(region.tz).date()
        if (today - day).days <= 80:
            url = FORECAST_URL
        elif day >= HISTORICAL_FORECAST_START:
            url = HISTORICAL_FORECAST_URL
        else:
            url = ARCHIVE_URL
            hourly = BASIC_HOURLY
        points = region.weather_points
        params = {
            "latitude": ",".join(f"{p.lat:.4f}" for p in points),
            "longitude": ",".join(f"{p.lon:.4f}" for p in points),
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "hourly": ",".join(hourly),
            "daily": ",".join(DAILY),
            "timezone": region.timezone,
            "wind_speed_unit": "kmh",
        }
        if region.weather_model:
            params["models"] = region.weather_model
        response = httpx.get(url, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else [data]

    def fetch_day(self, region: Region, day: date) -> int:
        """Download and store the weather of ``day`` for all points of ``region``."""
        if not region.weather_points:
            return 0
        try:
            results = self._request(region, day, FULL_HOURLY)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            log.info("Full variable set rejected (%s), retrying with basic set", exc.response.text[:200])
            results = self._request(region, day, BASIC_HOURLY)
        return self.store(region, day, results, "open-meteo", region.weather_model or "best_match")

    def store(self, region: Region, day: date, results: list[dict], source: str, model: str) -> int:
        """Store Open-Meteo shaped results (one per weather point, same order)."""
        final = day < datetime.now(region.tz).date()
        now = utcnow()
        rows = []
        for point, result in zip(region.weather_points, results):
            hourly = result.get("hourly") or {}
            daily = result.get("daily") or {}
            elevation = result.get("elevation")
            rows.append(
                WeatherDay(
                    date=day,
                    region=region.id,
                    point_id=point.id,
                    point_name=point.name,
                    lat=point.lat,
                    lon=point.lon,
                    elevation=elevation,
                    source=source,
                    model=model,
                    final=final,
                    fetched_at=now,
                    summary=summarize(hourly, daily, elevation),
                    daily=daily,
                    hourly=hourly,
                )
            )
        with self.db.session() as session, session.begin():
            session.execute(delete(WeatherDay).where(WeatherDay.date == day, WeatherDay.region == region.id))
            session.add_all(rows)
        log.info("Weather stored for %s %s (%d points, %s)", region.id, day, len(rows), source)
        return len(rows)

    def _is_final(self, region: Region, day: date) -> bool:
        with self.db.session() as session:
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(WeatherDay)
                    .where(WeatherDay.date == day, WeatherDay.region == region.id, WeatherDay.final.is_(True))
                )
            )

    def _has_weather(self, region: Region, day: date) -> bool:
        with self.db.session() as session:
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(WeatherDay)
                    .where(WeatherDay.date == day, WeatherDay.region == region.id)
                )
            )

    def update(self) -> None:
        """Refresh today, finalise yesterday and backfill recent days that have flights."""
        for region in self.regions.values():
            today = datetime.now(region.tz).date()
            try:
                self.fetch_day(region, today)
                yesterday = today - timedelta(days=1)
                if not self._is_final(region, yesterday):
                    self.fetch_day(region, yesterday)
                since = today - timedelta(days=self.backfill_days)
                with self.db.session() as session:
                    flight_days = session.scalars(
                        select(Flight.date)
                        .where(Flight.region == region.id, Flight.date >= since, Flight.date < yesterday)
                        .distinct()
                    ).all()
                missing = [d for d in sorted(flight_days, reverse=True) if not self._has_weather(region, d)]
                for day in missing[:5]:
                    self.fetch_day(region, day)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Weather update for %s failed: %s", region.id, exc)
        self.last_run = utcnow().isoformat() + "Z"

    def run(self, stop: threading.Event) -> None:
        if stop.wait(15):
            return
        while not stop.is_set():
            self.update()
            if stop.wait(self.interval.total_seconds()):
                return

    # -- reading ----------------------------------------------------------------------

    def day_report(self, region: Region, day: date, hourly: bool = True) -> dict:
        with self.db.session() as session:
            rows = session.scalars(
                select(WeatherDay).where(WeatherDay.date == day, WeatherDay.region == region.id)
            ).all()
        order = {p.id: i for i, p in enumerate(region.weather_points)}
        rows = sorted(rows, key=lambda r: order.get(r.point_id, 99))
        by_id = {r.point_id: r for r in rows}
        gradients = []
        for g in region.gradients:
            a, b = by_id.get(g.a), by_id.get(g.b)
            if not a or not b:
                continue
            times = a.hourly.get("time") or []
            pa, pb = a.hourly.get("pressure_msl") or [], b.hourly.get("pressure_msl") or []
            series = [
                (t, round(x - y, 1)) for t, x, y in zip(times, pa, pb) if x is not None and y is not None
            ]
            day_values = [v for t, v in series if _hour_of(t) in range(6, 21)]
            ref = next((v for t, v in series if _hour_of(t) == REFERENCE_HOUR), None)
            label = ""
            if ref is not None and abs(ref) >= g.threshold:
                label = g.positive_label if ref > 0 else g.negative_label
            gradients.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "value": ref,
                    "min": min(day_values) if day_values else None,
                    "max": max(day_values) if day_values else None,
                    "threshold": g.threshold,
                    "label": label,
                    "series": series if hourly else None,
                }
            )
        return {
            "region": region.id,
            "date": day.isoformat(),
            "fetched_at": max((r.fetched_at for r in rows), default=None),
            "final": all(r.final for r in rows) if rows else False,
            "source": rows[0].source if rows else None,
            "points": [
                {
                    "id": r.point_id,
                    "name": r.point_name,
                    "lat": r.lat,
                    "lon": r.lon,
                    "elevation": r.elevation,
                    "model": r.model,
                    "summary": r.summary,
                    "hourly": r.hourly if hourly else None,
                }
                for r in rows
            ],
            "gradients": gradients,
        }

    def at_location(self, region: Region, day: date, lat: float, lon: float, when: datetime) -> dict | None:
        """Hourly weather of the nearest sample point at the given (UTC) time."""
        with self.db.session() as session:
            rows = session.scalars(
                select(WeatherDay).where(WeatherDay.date == day, WeatherDay.region == region.id)
            ).all()
        if not rows:
            return None
        row = min(rows, key=lambda r: distance_m(lat, lon, r.lat, r.lon))
        local = when.replace(tzinfo=timezone.utc).astimezone(region.tz)
        key = local.strftime("%Y-%m-%dT%H:00")
        times = row.hourly.get("time") or []
        if key not in times:
            return None
        i = times.index(key)
        values = {k: v[i] for k, v in row.hourly.items() if k != "time" and isinstance(v, list) and i < len(v)}
        return {
            "point": {"id": row.point_id, "name": row.point_name, "lat": row.lat, "lon": row.lon},
            "distance_km": round(distance_m(lat, lon, row.lat, row.lon) / 1000, 1),
            "time": key,
            "values": values,
            "summary": row.summary,
        }
