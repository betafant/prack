"""Flight statistics computed from the stored fixes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .geo import distance_m

MAX_PLAUSIBLE_SPEED_KMH = 450.0
VARIO_WINDOW_S = 20.0  # max climb / sink are averaged over this window
AIRBORNE_MIN_AGL = 50.0


@dataclass
class FixPoint:
    ts: datetime
    lat: float
    lon: float
    alt: float
    ground: float | None = None
    speed: float | None = None
    climb: float | None = None


def _smooth(values: list[float], window: int = 5) -> list[float]:
    if len(values) < window:
        return list(values)
    half = window // 2
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def compute_stats(fixes: list[FixPoint], takeoff_speed: float = 15.0) -> dict:
    """Return the Flight columns derived from a chronologically sorted list of fixes."""
    if not fixes:
        return {"fix_count": 0}
    first, last = fixes[0], fixes[-1]
    alts = [f.alt for f in fixes]
    stats: dict = {
        "fix_count": len(fixes),
        "start_time": first.ts,
        "end_time": last.ts,
        "max_alt": max(alts),
        "min_alt": min(alts),
        "min_lat": min(f.lat for f in fixes),
        "max_lat": max(f.lat for f in fixes),
        "min_lon": min(f.lon for f in fixes),
        "max_lon": max(f.lon for f in fixes),
    }

    takeoff = next((f for f in fixes if (f.speed or 0) >= takeoff_speed), first)
    stats.update(
        takeoff_time=takeoff.ts,
        takeoff_lat=takeoff.lat,
        takeoff_lon=takeoff.lon,
        takeoff_alt=takeoff.alt,
        landing_lat=last.lat,
        landing_lon=last.lon,
        landing_alt=last.alt,
    )

    distance = 0.0
    max_from_start = 0.0
    for prev, cur in zip(fixes, fixes[1:]):
        d = distance_m(prev.lat, prev.lon, cur.lat, cur.lon)
        dt = (cur.ts - prev.ts).total_seconds()
        if dt > 0 and d / dt * 3.6 > MAX_PLAUSIBLE_SPEED_KMH:
            continue
        distance += d
        max_from_start = max(max_from_start, distance_m(takeoff.lat, takeoff.lon, cur.lat, cur.lon))
    stats["distance_km"] = round(distance / 1000.0, 3)
    stats["max_from_start_km"] = round(max_from_start / 1000.0, 3)
    stats["straight_km"] = round(distance_m(takeoff.lat, takeoff.lon, last.lat, last.lon) / 1000.0, 3)

    smoothed = _smooth(alts)
    stats["alt_gain"] = round(sum(max(0.0, b - a) for a, b in zip(smoothed, smoothed[1:])), 1)

    # best climb / sink averaged over ~20 s
    max_climb = 0.0
    max_sink = 0.0
    j = 0
    for i in range(len(fixes)):
        while j < i and (fixes[i].ts - fixes[j].ts).total_seconds() > VARIO_WINDOW_S:
            j += 1
        dt = (fixes[i].ts - fixes[j].ts).total_seconds()
        if dt >= VARIO_WINDOW_S * 0.5:
            rate = (fixes[i].alt - fixes[j].alt) / dt
            max_climb = max(max_climb, rate)
            max_sink = min(max_sink, rate)
    stats["max_climb"] = round(max_climb, 2)
    stats["max_sink"] = round(max_sink, 2)

    speeds = [f.speed for f in fixes if f.speed is not None and f.speed < MAX_PLAUSIBLE_SPEED_KMH]
    stats["max_speed"] = round(max(speeds), 1) if speeds else None

    agls = [f.alt - f.ground for f in fixes if f.ground is not None]
    stats["max_agl"] = round(max(agls), 1) if agls else None

    duration = (last.ts - first.ts).total_seconds()
    if agls:
        airborne = stats["max_agl"] >= AIRBORNE_MIN_AGL and duration >= 60
    else:
        airborne = duration >= 60 and ((stats["max_alt"] - stats["min_alt"]) > 100 or (stats["max_speed"] or 0) > 20)
    stats["airborne"] = airborne
    return stats


def simplify_track(points: list[tuple[float, float, float]], tolerance_m: float = 12.0, max_points: int = 500) -> list[list]:
    """Ramer-Douglas-Peucker simplification of (lon, lat, alt) points, rounded for compact storage."""
    n = len(points)
    if n <= 2:
        return [[round(p[0], 5), round(p[1], 5), round(p[2])] for p in points]
    lat0 = math.radians(sum(p[1] for p in points) / n)
    kx = 111_320.0 * math.cos(lat0)
    ky = 110_540.0
    xy = [(p[0] * kx, p[1] * ky) for p in points]

    def rdp(eps: float) -> list[int]:
        keep = [False] * n
        keep[0] = keep[-1] = True
        stack = [(0, n - 1)]
        while stack:
            a, b = stack.pop()
            (ax, ay), (bx, by) = xy[a], xy[b]
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy)
            best, best_i = -1.0, -1
            for i in range(a + 1, b):
                px, py = xy[i]
                if norm == 0:
                    d = math.hypot(px - ax, py - ay)
                else:
                    d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
                if d > best:
                    best, best_i = d, i
            if best > eps and best_i > 0:
                keep[best_i] = True
                stack.append((a, best_i))
                stack.append((best_i, b))
        return [i for i in range(n) if keep[i]]

    eps = tolerance_m
    idx = rdp(eps)
    while len(idx) > max_points:
        eps *= 1.6
        idx = rdp(eps)
    return [[round(points[i][0], 5), round(points[i][1], 5), round(points[i][2])] for i in idx]
