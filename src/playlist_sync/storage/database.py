"""SQLAlchemy database models and session management for sync history."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DEFAULT_DB_PATH = Path.home() / ".config" / "playlist-sync" / "history.db"


class Base(DeclarativeBase):
    pass


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_platform: Mapped[str] = mapped_column(String(50))
    target_platform: Mapped[str] = mapped_column(String(50))
    playlist_name: Mapped[str] = mapped_column(String(500))
    dry_run: Mapped[bool] = mapped_column(default=False)
    total: Mapped[int] = mapped_column(Integer, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    ambiguous: Mapped[int] = mapped_column(Integer, default=0)
    not_found: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)
    errors: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SyncTrackState(Base):
    __tablename__ = "sync_track_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_run_id: Mapped[int] = mapped_column(Integer)
    source_track_key: Mapped[str] = mapped_column(String(500))
    source_track_platform_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    source_index: Mapped[int] = mapped_column(Integer)
    source_title: Mapped[str] = mapped_column(String(500))
    source_artists: Mapped[str] = mapped_column(String(1000))
    source_album: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    matched_track_platform_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    matched_track_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    matched_track_artists: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    matched_track_album: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    applied: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UnmatchedTrack(Base):
    __tablename__ = "unmatched_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_run_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    artists: Mapped[str] = mapped_column(String(500))
    album: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    platform: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50))  # not_found / ambiguous / skipped
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


def create_db(db_path: Path = DEFAULT_DB_PATH) -> sessionmaker[Session]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
