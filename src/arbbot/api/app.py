"""FastAPI application factory.

Read-only at Milestone 1. There is no route here that can move money, and the
approval and kill endpoints the specification describes arrive with the
milestones that earn them -- an approval endpoint that exists before there is
anything to approve is just an unguarded door.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI
from sqlalchemy.orm import Session

from arbbot import __version__
from arbbot.api import health as health_module
from arbbot.config import Settings, load_settings
from arbbot.db.session import create_engine_from_settings, session_factory

__all__ = ["create_app"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Settings are resolved once at startup and injected, so a request cannot
    observe a different configuration than the one the process validated.
    """
    resolved = settings or load_settings()
    engine = create_engine_from_settings(resolved)
    make_session = session_factory(engine)

    app = FastAPI(
        title="arbbot",
        version=__version__,
        description="Logical-arbitrage evidence platform. Read-only at this milestone.",
    )

    def get_session() -> Iterator[Session]:
        with make_session() as session:
            yield session

    app.dependency_overrides[health_module.get_session] = get_session
    app.dependency_overrides[health_module.get_settings] = lambda: resolved
    app.include_router(health_module.router)
    return app
