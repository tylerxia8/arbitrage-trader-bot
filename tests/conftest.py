"""Shared fixtures.

Settings tests must not depend on the developer's shell or on a ``.env`` file
that happens to be present, so the environment is scrubbed for every test.
A config test that passes only on the machine that wrote it is worse than no
test, because it will pass in CI for the wrong reason.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from arbbot.db import models  # noqa: F401  -- registers tables on Base.metadata
from arbbot.db.base import Base


@pytest.fixture
def session() -> Iterator[Session]:
    """An isolated in-memory database per test.

    SQLite rather than PostgreSQL: these tests exercise application logic, and
    binding them to a running container would make the suite unrunnable on a
    laptop with no Docker. The behaviours that genuinely need PostgreSQL --
    the append-only triggers, migration reversibility -- are verified against
    a real server in CI instead.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


@pytest.fixture(autouse=True)
def _scrub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ARBBOT_* variable for the duration of a test.

    ``monkeypatch`` restores them at teardown, so no explicit cleanup is needed.
    """
    for key in list(os.environ):
        if key.startswith("ARBBOT_"):
            monkeypatch.delenv(key, raising=False)
