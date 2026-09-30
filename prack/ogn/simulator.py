"""Synthetic OGN traffic for demos and tests.

Produces genuine APRS sentences (same format as the OGN servers), so the whole
pipeline (parser -> tracker -> database -> API) is exercised. Paragliders and
hang gliders launch from well known Swiss sites, thermal up to cloud base,
glide cross-country and land; gliders do the same from airfields, faster and
higher.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Iterator

from ..db import utcnow
from ..regions import Region

EARTH_R = 6371000.0


@dataclass(frozen=True)
class Site:
    name: str
    lat: float
    lon: float
    alt: float
    land_lat: float
    land_lon: float
    land_alt: float
    types: tuple[int, ...]
    base: float  # typical cloud base


SWISS_SITES = [
    Site("Niesen", 46.6453, 7.6511, 2330, 46.6370, 7.6900, 690, (7, 6), 3100),
    Site("Grindelwald First", 46.6595, 8.0545, 2150, 46.6236, 8.0400, 1030, (7,), 3400),
    Site("Fiesch", 46.4122, 8.1110, 2200, 46.4063, 8.1350, 1050, (7, 6), 3900),
    Site("Engelberg", 46.8385, 8.4150, 1850, 46.8200, 8.4050, 1000, (7,), 3100),
    Site("Amden", 47.1600, 9.1400, 1350, 47.1275, 9.1280, 430, (7, 6), 2500),
    Site("Monte Lema", 46.0416, 8.8370, 1600, 46.0340, 8.8850, 300, (7, 6), 2600),
    Site("Weissenstein", 47.2525, 7.5065, 1280, 47.2320, 7.5270, 450, (7, 6), 2200),
    Site("Schänis", 47.1717, 9.0380, 416, 47.1717, 9.0380, 416, (1,), 3300),
    Site("Bex", 46.2583, 6.9864, 400, 46.2583, 6.9864, 400, (1,), 3500),
    Site("Samedan", 46.5340, 9.8840, 1707, 46.5340, 9.8840, 1707, (1,), 4300),
    Site("Birrfeld", 47.4433, 8.2330, 395, 47.4433, 8.2330, 395, (1,), 2400),
]

RECEIVERS = ["Niesen", "Jungfrau", "Chasseral", "Uetliberg", "Rigi", "Weisshorn", "Monte Generoso", "Saentis"]
PILOT_NAMES = [
    "Alex M", "Chris K", "Sam R", "Jo B", "Robin S", "Kim W", "Luca F", "Noa G",
    "Andrea P", "Mika T", "Sasha L", "Dominique H", "Nico V", "Toni Z",
]

# (glide speed km/h, glide sink m/s, thermal speed km/h, turn deg/s, climb m/s)
PERFORMANCE = {
    7: (36.0, 1.15, 32.0, 18.0, 2.0),
    6: (48.0, 1.0, 40.0, 16.0, 2.2),
    1: (110.0, 0.95, 90.0, 14.0, 2.6),
    2: (140.0, -4.0, 120.0, 10.0, 0.0),
}


def _move(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / EARTH_R
    dlon = dist_m * math.sin(b) / (EARTH_R * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - math.sin(math.radians(lat1)) * math.cos(
        math.radians(lat2)
    ) * math.cos(math.radians(lon2 - lon1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _dist(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _aprs_coord(value: float, deg_digits: int, pos: str, neg: str) -> tuple[str, int]:
    hemi = pos if value >= 0 else neg
    value = abs(value)
    deg = int(value)
    thousandths = int(round((value - deg) * 60 * 1000))
    if thousandths >= 60000:
        deg += 1
        thousandths -= 60000
    minutes, extra = divmod(thousandths, 10)
    text = f"{deg:0{deg_digits}d}{minutes // 100:02d}.{minutes % 100:02d}{hemi}"
    return text, extra


@dataclass
class SimAircraft:
    address: str
    aircraft_type: int
    fanet: bool
    site: Site
    rng: random.Random
    name: str | None = None
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0
    heading: float = 0.0
    speed: float = 0.0  # km/h air speed
    climb: float = 0.0
    turn: float = 0.0
    phase: str = "pre"
    phase_until: float = 0.0
    elapsed: float = 0.0
    duration: float = 0.0
    target: tuple[float, float] = (0.0, 0.0)
    thermal: tuple[float, float] = (0.0, 0.0)
    thermal_top: float = 0.0
    thermal_strength: float = 0.0
    beacon_every: float = 2.0
    next_beacon: float = 0.0
    receiver: str = ""
    wind: tuple[float, float] = (270.0, 10.0)  # from, km/h
    last_track: float = 0.0
    last_gs: float = 0.0
    last_climb: float = 0.0
    history: list = field(default_factory=list)

    @property
    def callsign(self) -> str:
        return ("FNT" if self.fanet else "FLR") + self.address

    @property
    def done(self) -> bool:
        return self.phase == "off"

    def start(self) -> None:
        self.lat, self.lon, self.alt = self.site.lat, self.site.lon, self.site.alt
        self.phase = "pre"
        self.phase_until = self.rng.uniform(60, 180)
        self.duration = self.rng.uniform(1.2, 3.5) * 3600 if self.aircraft_type != 7 else self.rng.uniform(0.6, 2.8) * 3600
        self.beacon_every = 4.0 if self.fanet else self.rng.choice([1.0, 2.0, 2.0, 3.0])
        self.receiver = self.rng.choice(RECEIVERS)
        self.heading = self.rng.uniform(0, 360)

    def _perf(self) -> tuple[float, float, float, float, float]:
        return PERFORMANCE.get(self.aircraft_type, PERFORMANCE[7])

    def _new_thermal(self) -> None:
        dist = self.rng.uniform(300, 1500) if self.aircraft_type != 1 else self.rng.uniform(800, 4000)
        self.thermal = _move(self.lat, self.lon, self.heading + self.rng.uniform(-40, 40), dist)
        self.thermal_top = self.site.base + self.rng.uniform(-300, 250)
        self.thermal_strength = self._perf()[4] * self.rng.uniform(0.5, 1.5)
        self.phase = "to_thermal"

    def _new_target(self) -> None:
        range_km = {7: 18, 6: 30, 1: 80}.get(self.aircraft_type, 20)
        bearing = self.rng.uniform(0, 360)
        # stay roughly around the home site
        dist_home = _dist(self.lat, self.lon, self.site.lat, self.site.lon)
        if dist_home > range_km * 600:
            bearing = _bearing(self.lat, self.lon, self.site.lat, self.site.lon) + self.rng.uniform(-50, 50)
        self.target = _move(self.lat, self.lon, bearing, self.rng.uniform(0.3, 0.7) * range_km * 1000)
        self.phase = "glide"

    def step(self, dt: float) -> None:
        self.elapsed += dt
        glide_v, sink, therm_v, turn_rate, _ = self._perf()
        wind_from, wind_kmh = self.wind
        self.turn = 0.0

        if self.phase == "pre":
            self.speed, self.climb = 0.0, 0.0
            if self.elapsed >= self.phase_until:
                if self.aircraft_type == 1:
                    self.phase = "tow"
                    self.phase_until = self.elapsed + self.rng.uniform(420, 720)
                else:
                    self.phase = "launch"
                self.heading = _bearing(self.lat, self.lon, self.site.land_lat, self.site.land_lon) + self.rng.uniform(-60, 60)
        elif self.phase == "tow":
            self.speed, self.climb = 125.0, 3.0
            self.heading += self.rng.uniform(-2, 2)
            if self.elapsed >= self.phase_until:
                self._new_thermal()
        elif self.phase == "launch":
            self.speed, self.climb = glide_v, -sink
            if self.site.alt - self.alt > 60:
                self._new_thermal()
        elif self.phase == "to_thermal":
            self.heading = _bearing(self.lat, self.lon, *self.thermal)
            self.speed, self.climb = glide_v, -sink
            if _dist(self.lat, self.lon, *self.thermal) < 80:
                self.phase = "thermal"
        elif self.phase == "thermal":
            self.speed = therm_v
            self.turn = turn_rate
            self.heading = (self.heading + turn_rate * dt) % 360
            self.climb = self.thermal_strength * self.rng.uniform(0.6, 1.3)
            if self.alt >= self.thermal_top:
                if self.elapsed > self.duration:
                    self.phase = "final"
                else:
                    self._new_target()
        elif self.phase == "glide":
            self.heading = _bearing(self.lat, self.lon, *self.target)
            self.speed, self.climb = glide_v, -sink * self.rng.uniform(0.7, 1.4)
            low = self.site.land_alt + (900 if self.aircraft_type == 1 else 700)
            if self.elapsed > self.duration:
                self.phase = "final"
            elif self.alt < max(low, self.site.base - 1300) or _dist(self.lat, self.lon, *self.target) < 200:
                self._new_thermal()
        elif self.phase == "final":
            self.heading = _bearing(self.lat, self.lon, self.site.land_lat, self.site.land_lon)
            d = _dist(self.lat, self.lon, self.site.land_lat, self.site.land_lon)
            above = self.alt - self.site.land_alt
            self.speed = glide_v
            if d < 400:
                # spiral / circuit down over the landing field
                self.turn = turn_rate
                self.heading = (self.heading + 90) % 360
                self.climb = -max(1.0, min(4.0, above / 60))
            else:
                glide_ratio = (glide_v / 3.6) / sink
                needed = d / glide_ratio
                self.climb = -sink if above > needed + 150 else -sink * 0.6
            if above <= 3:
                self.alt = self.site.land_alt
                self.phase = "landed"
                self.phase_until = self.elapsed + self.rng.uniform(240, 600)
        elif self.phase == "landed":
            self.speed, self.climb = 0.0, 0.0
            if self.elapsed >= self.phase_until:
                self.phase = "off"
            return

        if self.phase in ("pre", "off"):
            return

        # integrate: air vector + wind drift
        air = self.speed / 3.6 * dt
        n = air * math.cos(math.radians(self.heading))
        e = air * math.sin(math.radians(self.heading))
        wind_to = math.radians(wind_from + 180)
        n += wind_kmh / 3.6 * dt * math.cos(wind_to)
        e += wind_kmh / 3.6 * dt * math.sin(wind_to)
        dist = math.hypot(n, e)
        track = (math.degrees(math.atan2(e, n)) + 360) % 360
        self.lat, self.lon = _move(self.lat, self.lon, track, dist)
        self.alt += self.climb * dt
        self.last_track, self.last_gs, self.last_climb = track, dist / dt * 3.6, self.climb

    def beacon(self, ts: datetime) -> str:
        lat, lat_extra = _aprs_coord(self.lat, 2, "N", "S")
        lon, lon_extra = _aprs_coord(self.lon, 3, "E", "W")
        on_ground = self.phase in ("pre", "landed")
        gs = 0.0 if on_ground else self.last_gs
        course = 0 if on_ground else (int(round(self.last_track)) % 360 or 360)
        climb = 0.0 if on_ground else self.last_climb
        flags = (self.aircraft_type << 2) | (3 if self.fanet else 2)
        tocall = "OGNFNT" if self.fanet else "OGFLR"
        symbol = "/g" if self.aircraft_type in (6, 7) else "/'"
        return (
            f"{self.callsign}>{tocall},qAS,{self.receiver}:/{ts:%H%M%S}h{lat}{symbol[0]}{lon}{symbol[1]}"
            f"{course:03d}/{int(round(gs / 1.852)):03d}/A={int(round(self.alt / 0.3048)):06d}"
            f" !W{lat_extra}{lon_extra}! id{flags:02X}{self.address}"
            f" {int(round(climb / 0.00508)):+04d}fpm {self.turn / 3:+.1f}rot"
            f" {self.rng.uniform(3, 25):.1f}dB {self.rng.choice([0, 0, 0, 1, 2])}e {self.rng.uniform(-10, 10):+.1f}kHz gps2x3"
        )

    def status(self, ts: datetime) -> str | None:
        if not (self.fanet and self.name):
            return None
        return f'{self.callsign}>OGNFNT,qAS,{self.receiver}:>{ts:%H%M%S}h Name="{self.name}" 12.0dB'


class Simulator:
    """Keeps a population of simulated aircraft alive and emits their beacons."""

    def __init__(self, region: Region, seed: int | None = None, pg: int = 14, hg: int = 3, gl: int = 6, other: int = 1):
        self.region = region
        self.rng = random.Random(seed)
        self.targets = {7: pg, 6: hg, 1: gl, 2: other}
        self.aircraft: list[SimAircraft] = []
        self.wind = (self.rng.choice([250.0, 270.0, 300.0, 40.0]), self.rng.uniform(5, 18))
        self._used: set[str] = set()
        self.sites = SWISS_SITES if region.id == "ch" else self._random_sites()
        self.registrations: dict[str, tuple[str, str, str]] = {}  # address -> (registration, cn, model)
        self.on_register: Callable[[str, str, str, str], None] | None = None
        # optional terrain lookup (lat, lon) -> metres, keeps launch/landing altitudes on the ground
        self.ground: Callable[[float, float], float | None] | None = None
        self._grounded: dict[str, Site] = {}

    def _random_sites(self) -> list[Site]:
        west, south, east, north = self.region.bbox
        sites = []
        for i in range(8):
            lat, lon = self.rng.uniform(south, north), self.rng.uniform(west, east)
            t = (1,) if i % 3 == 0 else (7, 6)
            sites.append(Site(f"Site {i + 1}", lat, lon, 1500 if t != (1,) else 500, lat - 0.02, lon + 0.02, 500, t, 2800))
        return sites

    def _on_ground(self, site: Site) -> Site:
        if self.ground is None:
            return site
        if site.name not in self._grounded:
            try:
                takeoff = self.ground(site.lat, site.lon)
                landing = self.ground(site.land_lat, site.land_lon)
            except Exception:  # noqa: BLE001 - demo only, fall back to the table values
                takeoff = landing = None
            self._grounded[site.name] = replace(
                site,
                alt=site.alt if takeoff is None else takeoff + 2,
                land_alt=site.land_alt if landing is None else landing + 1,
            )
        return self._grounded[site.name]

    def _address(self) -> str:
        while True:
            addr = f"{self.rng.randint(0, 0xFFFFFF):06X}"
            if addr not in self._used:
                self._used.add(addr)
                return addr

    def _spawn(self, aircraft_type: int) -> SimAircraft:
        sites = [s for s in self.sites if aircraft_type in s.types] or [s for s in self.sites if 1 in s.types]
        if aircraft_type == 2:
            sites = [s for s in self.sites if 1 in s.types]
        site = self._on_ground(self.rng.choice(sites))
        fanet = aircraft_type in (6, 7) and self.rng.random() < 0.4
        ac = SimAircraft(self._address(), aircraft_type, fanet, site, random.Random(self.rng.random()))
        ac.wind = self.wind
        if fanet:
            ac.name = self.rng.choice(PILOT_NAMES)
        if aircraft_type == 1:
            number = self.rng.randint(1000, 3999)
            model = self.rng.choice(["Discus 2", "Ventus 3", "LS8", "ASG 29", "Arcus", "DG-808"])
            self.registrations[ac.address] = (f"HB-{number}", f"{self.rng.choice('ABCDEFGHKLMNPRSTVWXZ')}{number % 100:02d}", model)
            if self.on_register:
                self.on_register(ac.address, *self.registrations[ac.address])
        ac.start()
        # spread start times so not everybody launches together
        ac.phase_until = self.rng.uniform(10, 900)
        return ac

    def fill(self, initial: bool = False) -> None:
        for aircraft_type, target in self.targets.items():
            alive = sum(1 for a in self.aircraft if a.aircraft_type == aircraft_type and not a.done)
            for _ in range(max(0, target - alive)):
                if initial or self.rng.random() < 0.02:
                    ac = self._spawn(aircraft_type)
                    if initial and aircraft_type != 2:
                        # start somewhere in the middle of a flight
                        warm = self.rng.uniform(0, ac.duration * 0.8)
                        t = 0.0
                        while t < warm and not ac.done:
                            ac.step(5.0)
                            t += 5.0
                    self.aircraft.append(ac)
        self.aircraft = [a for a in self.aircraft if not a.done]

    def tick(self, now: datetime, dt: float, spawn: bool = True) -> list[str]:
        if not self.aircraft and spawn:
            self.fill(initial=True)
        lines: list[str] = []
        for ac in self.aircraft:
            ac.step(dt)
            if ac.done:
                continue
            ac.next_beacon -= dt
            if ac.next_beacon <= 0:
                ac.next_beacon += ac.beacon_every
                if not (-0.5 < ac.next_beacon <= ac.beacon_every):
                    ac.next_beacon = ac.beacon_every
                lines.append(ac.beacon(now))
                if ac.fanet and self.rng.random() < 0.02:
                    status = ac.status(now)
                    if status:
                        lines.append(status)
        if spawn:
            self.fill()
        else:
            self.aircraft = [a for a in self.aircraft if not a.done]
        return lines

    def run_realtime(self, stop: threading.Event, on_line: Callable[[str], None]) -> None:
        self.fill(initial=True)
        last = time.monotonic()
        while not stop.wait(1.0):
            now_mono = time.monotonic()
            dt, last = now_mono - last, now_mono
            for line in self.tick(utcnow(), dt):
                on_line(line)

    def run_period(self, start: datetime, end: datetime, dt: float = 2.0) -> Iterator[tuple[datetime, str]]:
        """Simulate a past period as fast as possible (for seeding the archive)."""
        t = start
        for aircraft_type, target in self.targets.items():
            for _ in range(target):
                self.aircraft.append(self._spawn(aircraft_type))
        spawn_until = end - timedelta(hours=2)
        while t < end and (self.aircraft or t < spawn_until):
            for line in self.tick(t, dt, spawn=t < spawn_until):
                yield t, line
            t += timedelta(seconds=dt)
