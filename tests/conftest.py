"""Shared fixtures.

Settings tests must not depend on the developer's shell or on a ``.env`` file
that happens to be present, so the environment is scrubbed for every test.
A config test that passes only on the machine that wrote it is worse than no
test, because it will pass in CI for the wrong reason.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _scrub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ARBBOT_* variable for the duration of a test.

    ``monkeypatch`` restores them at teardown, so no explicit cleanup is needed.
    """
    for key in list(os.environ):
        if key.startswith("ARBBOT_"):
            monkeypatch.delenv(key, raising=False)
