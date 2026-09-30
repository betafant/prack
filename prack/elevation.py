"""Ground elevation from Terrarium encoded DEM tiles (global, no API key).

Tiles are downloaded on demand, cached on disk (``<data>/dem``) and decoded in
memory. Elevation = R*256 + G + B/256 - 32768 (metres above sea level).
The same tiles drive the 3D terrain in the browser, so the ground profile of
the altitude chart matches the 3D view.
"""

from __future__ import annotations

import io
import logging
import math
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx
from PIL import Image

log = logging.getLogger(__name__)

TILE_SIZE = 256
FAILED_RETRY_SECONDS = 600


class ElevationService:
    def __init__(self, url_template: str, zoom: int, cache_dir: Path, enabled: bool = True, memory_tiles: int = 96):
        self.url_template = url_template
        self.zoom = zoom
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.memory_tiles = memory_tiles
        self._tiles: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._failed: dict[tuple[int, int, int], float] = {}
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._queue: queue.Queue[tuple[int, int, int]] = queue.Queue()
        self._queued: set[tuple[int, int, int]] = set()
        self._worker: threading.Thread | None = None
        self._client: httpx.Client | None = None

    # -- tile math -----------------------------------------------------------------

    def _pixel(self, lat: float, lon: float) -> tuple[int, int, float, float]:
        n = 2**self.zoom
        lat = max(-85.0511, min(85.0511, lat))
        x = (lon + 180.0) / 360.0 * n
        lat_r = math.radians(lat)
        y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
        tx, ty = int(x) % n, int(y)
        return tx, ty, (x - int(x)) * TILE_SIZE, (y - int(y)) * TILE_SIZE

    @staticmethod
    def _sample(data: bytes, px: float, py: float) -> float:
        # bilinear interpolation between pixel centres, clamped to the tile
        fx = min(max(px - 0.5, 0.0), TILE_SIZE - 1.001)
        fy = min(max(py - 0.5, 0.0), TILE_SIZE - 1.001)
        x0, y0 = int(fx), int(fy)
        dx, dy = fx - x0, fy - y0

        def h(x: int, y: int) -> float:
            i = (y * TILE_SIZE + x) * 3
            return data[i] * 256.0 + data[i + 1] + data[i + 2] / 256.0 - 32768.0

        top = h(x0, y0) * (1 - dx) + h(x0 + 1, y0) * dx
        bottom = h(x0, y0 + 1) * (1 - dx) + h(x0 + 1, y0 + 1) * dx
        return top * (1 - dy) + bottom * dy

    # -- tile access ---------------------------------------------------------------

    def _memory_get(self, key: tuple[int, int, int]) -> bytes | None:
        with self._lock:
            data = self._tiles.get(key)
            if data is not None:
                self._tiles.move_to_end(key)
            return data

    def _memory_put(self, key: tuple[int, int, int], data: bytes) -> None:
        with self._lock:
            self._tiles[key] = data
            self._tiles.move_to_end(key)
            while len(self._tiles) > self.memory_tiles:
                self._tiles.popitem(last=False)

    def _disk_path(self, key: tuple[int, int, int]) -> Path:
        z, x, y = key
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    def _http(self) -> httpx.Client:
        with self._fetch_lock:
            if self._client is None:
                self._client = httpx.Client(timeout=20, follow_redirects=True)
            return self._client

    def _decode(self, png: bytes) -> bytes:
        with Image.open(io.BytesIO(png)) as img:
            return img.convert("RGB").tobytes()

    def _load_tile(self, key: tuple[int, int, int], download: bool) -> bytes | None:
        data = self._memory_get(key)
        if data is not None:
            return data
        path = self._disk_path(key)
        if path.is_file():
            try:
                data = self._decode(path.read_bytes())
                self._memory_put(key, data)
                return data
            except Exception:  # noqa: BLE001 - corrupt cache file, fetch again
                path.unlink(missing_ok=True)
        if not download or not self.enabled:
            return None
        failed_at = self._failed.get(key)
        if failed_at and time.time() - failed_at < FAILED_RETRY_SECONDS:
            return None
        z, x, y = key
        url = self.url_template.format(z=z, x=x, y=y)
        try:
            response = self._http().get(url)
            response.raise_for_status()
            data = self._decode(response.content)
        except Exception as exc:  # noqa: BLE001
            self._failed[key] = time.time()
            log.warning("DEM tile %s unavailable: %s", key, exc)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        self._memory_put(key, data)
        return data

    # -- public API ----------------------------------------------------------------

    def tile_png(self, z: int, x: int, y: int) -> bytes | None:
        """Raw Terrarium PNG for the browser's 3D terrain (served from the disk cache)."""
        key = (z, x, y)
        path = self._disk_path(key)
        if path.is_file():
            return path.read_bytes()
        if not self.enabled:
            return None
        failed_at = self._failed.get(key)
        if failed_at and time.time() - failed_at < FAILED_RETRY_SECONDS:
            return None
        try:
            response = self._http().get(self.url_template.format(z=z, x=x, y=y))
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._failed[key] = time.time()
            log.warning("DEM tile %s unavailable: %s", key, exc)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        return response.content

    def get(self, lat: float, lon: float) -> float | None:
        """Blocking lookup (downloads the tile if necessary)."""
        tx, ty, px, py = self._pixel(lat, lon)
        data = self._load_tile((self.zoom, tx, ty), download=True)
        return None if data is None else round(self._sample(data, px, py), 1)

    def get_cached(self, lat: float, lon: float) -> float | None:
        """Non blocking lookup; schedules a background download on a miss."""
        tx, ty, px, py = self._pixel(lat, lon)
        key = (self.zoom, tx, ty)
        data = self._memory_get(key)
        if data is None:
            data = self._load_tile(key, download=False)
        if data is None:
            self._schedule(key)
            return None
        return round(self._sample(data, px, py), 1)

    def get_many(self, points: list[tuple[float, float]]) -> list[float | None]:
        """Blocking lookup for many points, grouped per tile."""
        results: list[float | None] = [None] * len(points)
        by_tile: dict[tuple[int, int, int], list[tuple[int, float, float]]] = {}
        for i, (lat, lon) in enumerate(points):
            tx, ty, px, py = self._pixel(lat, lon)
            by_tile.setdefault((self.zoom, tx, ty), []).append((i, px, py))
        for key, items in by_tile.items():
            data = self._load_tile(key, download=True)
            if data is None:
                continue
            for i, px, py in items:
                results[i] = round(self._sample(data, px, py), 1)
        return results

    def _schedule(self, key: tuple[int, int, int]) -> None:
        if not self.enabled:
            return
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="dem-fetch", daemon=True)
                self._worker.start()
        self._queue.put(key)

    def _work(self) -> None:
        while True:
            key = self._queue.get()
            try:
                self._load_tile(key, download=True)
            finally:
                with self._lock:
                    self._queued.discard(key)
