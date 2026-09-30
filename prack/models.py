"""Database schema.

All timestamps are stored as naive UTC datetimes. Heights are metres above
mean sea level, speeds km/h, climb rates m/s.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class EpochSeconds(TypeDecorator):
    """Naive UTC datetime stored as integer epoch seconds (compact, fast to index)."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, int):
            return value
        return int(value.replace(tzinfo=timezone.utc).timestamp())

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)


class Scaled(TypeDecorator):
    """Float stored as a scaled integer (e.g. micro degrees): about half the size of a REAL."""

    impl = Integer
    cache_ok = True

    def __init__(self, scale: int) -> None:
        super().__init__()
        self.scale = scale

    def process_bind_param(self, value, dialect):
        return None if value is None else int(round(value * self.scale))

    def process_result_value(self, value, dialect):
        return None if value is None else value / self.scale


class Base(DeclarativeBase):
    pass


class Device(Base):
    """A transmitter seen on OGN (FLARM, FANET, OGN tracker, ...)."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callsign: Mapped[str] = mapped_column(String(16), unique=True)  # e.g. FLRDDA5BA
    address: Mapped[str] = mapped_column(String(8), index=True)  # 24 bit hex
    address_type: Mapped[int] = mapped_column(SmallInteger, default=0)  # 0 random, 1 ICAO, 2 FLARM, 3 OGN
    aircraft_type: Mapped[int] = mapped_column(SmallInteger, default=0)
    source: Mapped[str] = mapped_column(String(24), default="")  # FLARM, FANET, ...
    registration: Mapped[str | None] = mapped_column(String(32))
    competition_id: Mapped[str | None] = mapped_column(String(8))
    model: Mapped[str | None] = mapped_column(String(64))
    pilot_name: Mapped[str | None] = mapped_column(String(64))  # FANET name broadcast
    identified: Mapped[bool] = mapped_column(Boolean, default=True)  # DDB: may show registration
    software_version: Mapped[str | None] = mapped_column(String(16))
    hardware_version: Mapped[str | None] = mapped_column(String(16))
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)

    flights: Mapped[list["Flight"]] = relationship(back_populates="device")


class Flight(Base):
    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    region: Mapped[str] = mapped_column(String(16))
    date: Mapped[date] = mapped_column(Date)  # local date (region time zone) of the start
    aircraft_type: Mapped[int] = mapped_column(SmallInteger, default=0)
    source: Mapped[str] = mapped_column(String(24), default="")
    status: Mapped[str] = mapped_column(String(8), default="active")  # active | closed
    close_reason: Mapped[str | None] = mapped_column(String(8))  # landed | gap
    airborne: Mapped[bool] = mapped_column(Boolean, default=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    takeoff_time: Mapped[datetime | None] = mapped_column(DateTime)
    landing_time: Mapped[datetime | None] = mapped_column(DateTime)
    fix_count: Mapped[int] = mapped_column(Integer, default=0)

    takeoff_lat: Mapped[float | None] = mapped_column(Float)
    takeoff_lon: Mapped[float | None] = mapped_column(Float)
    takeoff_alt: Mapped[float | None] = mapped_column(Float)
    landing_lat: Mapped[float | None] = mapped_column(Float)
    landing_lon: Mapped[float | None] = mapped_column(Float)
    landing_alt: Mapped[float | None] = mapped_column(Float)
    min_lat: Mapped[float | None] = mapped_column(Float)
    max_lat: Mapped[float | None] = mapped_column(Float)
    min_lon: Mapped[float | None] = mapped_column(Float)
    max_lon: Mapped[float | None] = mapped_column(Float)

    max_alt: Mapped[float | None] = mapped_column(Float)
    min_alt: Mapped[float | None] = mapped_column(Float)
    max_agl: Mapped[float | None] = mapped_column(Float)
    alt_gain: Mapped[float | None] = mapped_column(Float)  # cumulated climb
    max_climb: Mapped[float | None] = mapped_column(Float)
    max_sink: Mapped[float | None] = mapped_column(Float)
    max_speed: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)  # track length
    straight_km: Mapped[float | None] = mapped_column(Float)  # takeoff -> landing
    max_from_start_km: Mapped[float | None] = mapped_column(Float)
    ground_filled: Mapped[bool] = mapped_column(Boolean, default=False)
    preview: Mapped[list | None] = mapped_column(JSON)  # simplified [[lon, lat, alt], ...] for day overviews

    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

    device: Mapped[Device] = relationship(back_populates="flights")

    __table_args__ = (
        Index("ix_flights_date_region", "date", "region"),
        Index("ix_flights_status", "status"),
    )


class Fix(Base):
    """One position report. Clustered by (flight, time) so a track is one range scan."""

    __tablename__ = "fixes"

    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(EpochSeconds, primary_key=True)
    lat: Mapped[float] = mapped_column(Scaled(1_000_000))
    lon: Mapped[float] = mapped_column(Scaled(1_000_000))
    alt: Mapped[float] = mapped_column(Scaled(10))
    ground: Mapped[float | None] = mapped_column(Scaled(10))  # terrain elevation below the fix
    speed: Mapped[float | None] = mapped_column(Scaled(10))
    track: Mapped[float | None] = mapped_column(Scaled(1))
    climb: Mapped[float | None] = mapped_column(Scaled(100))
    turn: Mapped[float | None] = mapped_column(Scaled(10))  # deg/s
    receiver: Mapped[str | None] = mapped_column(String(16))
    signal: Mapped[float | None] = mapped_column(Scaled(10))  # dB
    errors: Mapped[int | None] = mapped_column(SmallInteger)
    freq_offset: Mapped[float | None] = mapped_column(Scaled(10))  # kHz
    gps: Mapped[str | None] = mapped_column(String(8))
    raw: Mapped[str | None] = mapped_column(Text)

    __table_args__ = {"sqlite_with_rowid": False}


class DdbEntry(Base):
    """Copy of the OGN device database (registrations and privacy flags)."""

    __tablename__ = "ddb"

    address: Mapped[str] = mapped_column(String(8), primary_key=True)
    device_type: Mapped[str] = mapped_column(String(2), default="")
    model: Mapped[str | None] = mapped_column(String(64))
    registration: Mapped[str | None] = mapped_column(String(32))
    competition_id: Mapped[str | None] = mapped_column(String(8))
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    identified: Mapped[bool] = mapped_column(Boolean, default=True)
    ddb_aircraft_type: Mapped[int | None] = mapped_column(SmallInteger)


class WeatherDay(Base):
    """Weather of one day at one sample point of a region."""

    __tablename__ = "weather"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    region: Mapped[str] = mapped_column(String(16))
    point_id: Mapped[str] = mapped_column(String(32))
    point_name: Mapped[str] = mapped_column(String(64))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    elevation: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(48))
    final: Mapped[bool] = mapped_column(Boolean, default=False)  # fetched after the day ended
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    summary: Mapped[dict] = mapped_column(JSON)
    daily: Mapped[dict] = mapped_column(JSON)
    hourly: Mapped[dict] = mapped_column(JSON)

    __table_args__ = (UniqueConstraint("date", "region", "point_id", name="uq_weather_day_point"),)
