"""Operator CLI.

``arbbot doctor`` is how a person answers "is this deployment armed" without
reading code. If it ever prints a reassuring answer that does not match the
actual gates, the whole control is worthless -- so the gate lines are asserted
against the real flag values rather than against fixed text.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from arbbot import buildflags
from arbbot.cli import _check_database, main

#: Captured before the autouse fixture replaces the module attribute, so tests
#: that need the genuine probe can put it back.
REAL_CHECK_DATABASE = _check_database


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's database probe is exercised separately; stub it here."""
    monkeypatch.setattr("arbbot.cli._check_database", lambda _settings: True)


class TestDoctor:
    def test_reports_success_with_a_reachable_database(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["doctor"]) == 0
        assert "configuration          : valid" in capsys.readouterr().out

    def test_reports_the_real_build_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["doctor"])
        out = capsys.readouterr().out
        assert f"LIVE_EXECUTION_COMPILED_IN : {buildflags.LIVE_EXECUTION_COMPILED_IN}" in out
        assert f"DEMO_EXECUTION_COMPILED_IN : {buildflags.DEMO_EXECUTION_COMPILED_IN}" in out

    def test_reports_the_system_as_disarmed(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["doctor"])
        out = capsys.readouterr().out
        assert "may submit live orders              : False" in out

    def test_always_states_that_approval_is_still_required(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The standing gates are necessary, never sufficient. An operator must
        not read 'gates open' as 'orders will fire'."""
        main(["doctor"])
        assert "per-basket human approval is required" in capsys.readouterr().out

    def test_prints_the_risk_limits(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["doctor"])
        out = capsys.readouterr().out
        for label in (
            "max order notional",
            "max unmatched exposure",
            "max total open exposure",
            "daily loss limit",
            "min net edge",
            "max quote age",
        ):
            assert label in out, label

    def test_unreachable_database_is_a_nonzero_exit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("arbbot.cli._check_database", lambda _settings: False)
        assert main(["doctor"]) == 1

    def test_invalid_configuration_exits_two_without_reporting_gates(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A broken config must not produce a gate readout at all: a partially
        parsed configuration cannot be trusted to describe what is armed."""
        monkeypatch.setenv("ARBBOT_LOG_LEVEL", "CHATTY")
        assert main(["doctor"]) == 2
        captured = capsys.readouterr()
        assert "INVALID" in captured.err
        assert "may submit live orders" not in captured.out


class TestDatabaseProbe:
    def test_unreachable_database_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """doctor exists to surface this condition, so it must not crash on it.

        The failure is injected rather than provoked with a real connection:
        driving an actual TCP timeout costs two minutes of wall clock and
        tests the operating system's patience, not our error handling.
        """

        def explode(_settings: object) -> None:
            raise OperationalError("connect", None, OSError("connection refused"))

        monkeypatch.setattr("arbbot.cli._check_database", REAL_CHECK_DATABASE)
        monkeypatch.setattr("arbbot.cli.create_engine_from_settings", explode)
        assert main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "UNREACHABLE" in out
        assert "OperationalError" in out


class TestArgumentParsing:
    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "arbbot" in capsys.readouterr().out

    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 2
