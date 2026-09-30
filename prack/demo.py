"""Demo data: simulated past flying days and (if Open-Meteo is unreachable) synthetic weather."""

from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from sqlalchemy import func, select

from .models import Flight
from .ogn.simulator import Simulator
from .regions import Region
from .tracking.tracker import Tracker

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)


def synthetic_weather(region: Region, day: date) -> list[dict]:
    """Plausible, clearly synthetic weather in Open-Meteo's response shape (demo only)."""
    rng = random.Random(f"{region.id}-{day.isoformat()}")
    base_temp = 14 + 10 * math.sin((day.timetuple().tm_yday - 105) / 365 * 2 * math.pi)
    wind_dir = rng.choice([240, 260, 280, 30, 50, 190])
    foehn = rng.uniform(-3, 6)
    results = []
    for p in region.weather_points:
        elevation = 400 + 900 * abs(math.sin(p.lat * 7 + p.lon * 3))
        t_offset = -elevation / 150.0
        times, temp, dew, cloud, cloud_low, pressure, wind, wdir, gust, blh, cape = ([] for _ in range(11))
        rad, sun, rh, precip, code = [], [], [], [], []
        t850, t700, t500, w850, w700, w500, d850, d700, d500, z850, z700, z500, fl = ([] for _ in range(13))
        for h in range(24):
            diurnal = math.sin((h - 9) / 24 * 2 * math.pi)
            t = base_temp + t_offset + 6 * diurnal + rng.uniform(-0.5, 0.5)
            td = t - (6 + 5 * max(0, diurnal)) + rng.uniform(-1, 1)
            times.append(f"{day.isoformat()}T{h:02d}:00")
            temp.append(round(t, 1))
            dew.append(round(td, 1))
            rh.append(round(max(20, min(100, 100 - 5 * (t - td))), 0))
            c = max(0, min(100, 20 + 40 * max(0, diurnal) + rng.uniform(-15, 25)))
            cloud.append(round(c))
            cloud_low.append(round(c * 0.5))
            pressure.append(round(1016 + (foehn / 2 if p.lat < 46.3 else -foehn / 2) + rng.uniform(-0.3, 0.3), 1))
            ws = 6 + 8 * max(0, diurnal) + rng.uniform(0, 4)
            wind.append(round(ws, 1))
            wdir.append(wind_dir + rng.randint(-20, 20))
            gust.append(round(ws * 1.8, 1))
            blh.append(round(max(100, 300 + 1800 * max(0, diurnal) + rng.uniform(-100, 100))))
            cape.append(round(max(0, 400 * max(0, diurnal) + rng.uniform(-100, 200))))
            radiation = max(0, 850 * math.sin((h - 6) / 14 * math.pi)) if 6 <= h <= 20 else 0
            rad.append(round(radiation))
            sun.append(3600 if radiation > 150 and c < 70 else 0)
            precip.append(0.0)
            code.append(2 if c > 50 else 1)
            t850.append(round(t - 8 - elevation / 200, 1))
            t700.append(round(t - 18 - elevation / 200, 1))
            t500.append(round(t - 33 - elevation / 200, 1))
            w850.append(round(ws * 1.3, 1))
            w700.append(round(ws * 1.8, 1))
            w500.append(round(ws * 2.6, 1))
            d850.append(wind_dir)
            d700.append(wind_dir + 10)
            d500.append(wind_dir + 20)
            z850.append(1480)
            z700.append(3080)
            z500.append(5700)
            fl.append(round(3200 + 60 * t, 0))
        results.append(
            {
                "elevation": round(elevation),
                "hourly": {
                    "time": times, "temperature_2m": temp, "dew_point_2m": dew, "relative_humidity_2m": rh,
                    "precipitation": precip, "weather_code": code, "cloud_cover": cloud, "cloud_cover_low": cloud_low,
                    "cloud_cover_mid": cloud_low, "cloud_cover_high": cloud_low, "pressure_msl": pressure,
                    "wind_speed_10m": wind, "wind_direction_10m": wdir, "wind_gusts_10m": gust,
                    "shortwave_radiation": rad, "sunshine_duration": sun, "boundary_layer_height": blh, "cape": cape,
                    "freezing_level_height": fl, "temperature_850hPa": t850, "temperature_700hPa": t700,
                    "temperature_500hPa": t500, "wind_speed_850hPa": w850, "wind_direction_850hPa": d850,
                    "wind_speed_700hPa": w700, "wind_direction_700hPa": d700, "wind_speed_500hPa": w500,
                    "wind_direction_500hPa": d500, "geopotential_height_850hPa": z850,
                    "geopotential_height_700hPa": z700, "geopotential_height_500hPa": z500,
                },
                "daily": {
                    "time": [day.isoformat()], "weather_code": [2], "temperature_2m_max": [max(temp)],
                    "temperature_2m_min": [min(temp)], "precipitation_sum": [0.0], "sunshine_duration": [sum(sun)],
                    "wind_speed_10m_max": [max(wind)], "wind_gusts_10m_max": [max(gust)],
                    "wind_direction_10m_dominant": [wind_dir], "shortwave_radiation_sum": [round(sum(rad) * 0.0036, 1)],
                    "sunrise": [f"{day.isoformat()}T06:30"], "sunset": [f"{day.isoformat()}T19:30"],
                },
            }
        )
    return results


def ensure_weather(runtime: "Runtime", region: Region, day: date) -> None:
    try:
        runtime.weather.fetch_day(region, day)
    except Exception as exc:  # noqa: BLE001
        log.info("Open-Meteo unavailable (%s); storing synthetic demo weather for %s", exc, day)
        runtime.weather.store(region, day, synthetic_weather(region, day), "demo (synthetic)", "demo")


def seed_history(runtime: "Runtime", days: int = 2, progress: Callable[[str], None] | None = None) -> int:
    """Simulate ``days`` past flying days into the archive. Returns the number of flights created."""
    region = runtime.regions[0]
    today = datetime.now(region.tz).date()
    created = 0
    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        with runtime.db.session() as session:
            exists = session.scalar(select(func.count()).select_from(Flight).where(Flight.date == day))
        if exists:
            continue
        if progress:
            progress(f"simulating {day.isoformat()}")
        log.info("Seeding demo flights for %s", day)
        sim = Simulator(region, seed=day.toordinal())
        sim.on_register = runtime._register_demo
        if runtime.settings.elevation_enabled:
            sim.ground = runtime.elevation.get
        tracker = Tracker(runtime.db, runtime.settings, runtime.regions, runtime.ddb, None, runtime.finalizer.enqueue)
        start = datetime.combine(day, time(9, 30), region.tz).astimezone(timezone.utc).replace(tzinfo=None)
        end = start + timedelta(hours=9)
        last_flush = start
        for ts, line in sim.run_period(start, end, dt=2.0):
            if runtime.stop_event.is_set():
                return created
            tracker.process_line(line, ts)
            if (ts - last_flush).total_seconds() >= 10:
                tracker.flush()
                tracker.sweep(ts)
                last_flush = ts
        tracker.sweep(end + timedelta(hours=2))
        tracker.flush(force=True)
        created += tracker.counters["flights_opened"]
        ensure_weather(runtime, region, day)
    ensure_weather(runtime, region, today)
    if progress:
        progress("finalizing")
    return created
