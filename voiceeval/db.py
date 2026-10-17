"""Database models and session handling (SQLite by default, Postgres via DATABASE_URL)."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import get_settings


def _uuid() -> str:
    return uuid.uuid4().hex[:16]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    audio_key: Mapped[str] = mapped_column(String(512))          # canonical stereo wav
    transcript_key: Mapped[str | None] = mapped_column(String(512), nullable=True)  # optional sidecar
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    human_channel: Mapped[int] = mapped_column(Integer, default=0)
    ai_channel: Mapped[int] = mapped_column(Integer, default=1)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)        # accent, noise_condition, language, scenario...
    meta: Mapped[dict] = mapped_column(JSON, default=dict)        # events, annotations, cost, survey, ai_asr_transcript...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), default="")
    profile: Mapped[str] = mapped_column(String(128), default="default")
    metric_ids: Mapped[list] = mapped_column(JSON, default=list)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|done|failed
    summary: Mapped[dict] = mapped_column(JSON, default=dict)            # aggregated metric results
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["RunItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunItem(Base):
    """One recording inside a run. The unit of work that workers claim."""

    __tablename__ = "run_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    worker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    results: Mapped[dict] = mapped_column(JSON, default=dict)     # metric_id -> MetricResult dict
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)    # timeline / transcript summary for UI
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="items")
    recording: Mapped[Recording] = relationship()


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, pool_pre_ping=True, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
        Base.metadata.create_all(_engine)
    return _engine


@contextmanager
def session_scope() -> Iterator:
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
