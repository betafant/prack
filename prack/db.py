from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, insert
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def utcnow() -> datetime:
    """Current time as naive UTC (the convention used for all stored timestamps)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        kwargs: dict = {"future": True}
        if self.is_sqlite:
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(url, **kwargs)
        if self.is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self._sessionmaker()

    def insert_ignore(self, model):
        """INSERT that silently skips rows whose primary key already exists."""
        if self.is_sqlite:
            return sqlite.insert(model).on_conflict_do_nothing()
        if self.engine.dialect.name == "postgresql":
            return postgresql.insert(model).on_conflict_do_nothing()
        return insert(model)

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()
