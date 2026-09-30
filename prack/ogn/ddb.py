"""OGN device database (DDB): registrations, competition numbers and privacy flags.

Owners can opt out of tracking (``tracked = N``) or of being identified
(``identified = N``) in the DDB. prack honours both: untracked devices are
never stored, unidentified ones are shown without registration.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, insert, select

from ..db import Database
from ..models import DdbEntry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DdbInfo:
    registration: str | None
    competition_id: str | None
    model: str | None
    tracked: bool
    identified: bool


def _yes(value) -> bool:
    return str(value).strip().upper() in ("Y", "YES", "1", "TRUE")


class DeviceDatabase:
    def __init__(self, db: Database, url: str) -> None:
        self.db = db
        self.url = url
        self._entries: dict[str, DdbInfo] = {}
        self._lock = threading.Lock()
        self.last_update: str | None = None

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, address: str) -> DdbInfo | None:
        return self._entries.get(address.upper())

    def add_local(self, address: str, info: DdbInfo) -> None:
        """Register a device without touching the database (demo mode)."""
        with self._lock:
            self._entries[address.upper()] = info

    def load_from_db(self) -> None:
        with self.db.session() as session:
            rows = session.execute(select(DdbEntry)).scalars().all()
        entries = {
            r.address: DdbInfo(r.registration, r.competition_id, r.model, r.tracked, r.identified) for r in rows
        }
        with self._lock:
            self._entries = entries
        log.info("Loaded %d DDB entries from database", len(entries))

    def refresh(self) -> int:
        """Download the DDB and replace the local copy. Returns the number of devices."""
        response = httpx.get(self.url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        devices = response.json().get("devices", [])
        rows = []
        for d in devices:
            address = str(d.get("device_id", "")).upper()
            if len(address) != 6:
                continue
            try:
                ddb_type = int(d.get("aircraft_type") or 0)
            except ValueError:
                ddb_type = None
            rows.append(
                {
                    "address": address,
                    "device_type": str(d.get("device_type", ""))[:2],
                    "model": (d.get("aircraft_model") or None),
                    "registration": (d.get("registration") or None),
                    "competition_id": (d.get("cn") or None),
                    "tracked": _yes(d.get("tracked", "Y")),
                    "identified": _yes(d.get("identified", "Y")),
                    "ddb_aircraft_type": ddb_type,
                }
            )
        unique = {r["address"]: r for r in rows}
        with self.db.session() as session, session.begin():
            session.execute(delete(DdbEntry))
            if unique:
                session.execute(insert(DdbEntry), list(unique.values()))
        self.load_from_db()
        self.last_update = datetime.now(timezone.utc).isoformat()
        log.info("DDB refreshed: %d devices", len(unique))
        return len(unique)
