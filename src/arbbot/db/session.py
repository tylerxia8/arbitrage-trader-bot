"""Engine and session construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from arbbot.config import Settings

__all__ = ["create_engine_from_settings", "session_factory"]


def create_engine_from_settings(settings: Settings) -> Engine:
    """Build the engine.

    ``pool_pre_ping`` is on because the collector holds connections across long
    idle stretches between market hours, and a silently dropped connection
    surfacing as a failed write during execution is a worse failure than the
    round-trip costs.
    """
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        echo=False,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory.

    ``expire_on_commit`` is disabled so that objects remain readable after a
    commit -- audit and reporting code frequently needs the values it just
    persisted, and a lazy refresh mid-report is an avoidable failure mode.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
