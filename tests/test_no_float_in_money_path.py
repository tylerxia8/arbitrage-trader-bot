"""FR-002 enforcement, and enforcement of the enforcer.

The first test is the requirement. The rest verify that the checker actually
detects what it claims to -- a static check that silently passes everything is
strictly worse than no check, because it converts an unverified rule into a
green tick.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from check_no_float import check_file, collect_files, money_path_roots, run  # noqa: E402


def write_module(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "candidate.py"
    path.write_text(source, encoding="utf-8")
    return path


class TestRepository:
    def test_money_path_is_clean(self) -> None:
        violations = run(REPO_ROOT)
        rendered = "\n".join(v.render(REPO_ROOT) for v in violations)
        assert not violations, f"FR-002 violations found:\n{rendered}"

    def test_declared_roots_are_not_vacuous(self) -> None:
        """Guards against the rule being 'satisfied' by checking nothing."""
        files = collect_files(REPO_ROOT, money_path_roots(REPO_ROOT))
        assert files, "money_path_roots resolved to zero files"

    def test_the_core_money_module_is_covered(self) -> None:
        files = collect_files(REPO_ROOT, money_path_roots(REPO_ROOT))
        assert REPO_ROOT / "src" / "arbbot" / "money.py" in files


class TestDetection:
    @pytest.mark.parametrize(
        "source",
        [
            "TOTAL = 0.35\n",
            "def fee() -> float:\n    return 1\n",
            "x = float('0.35')\n",
            "def apply(rate: float) -> None:\n    pass\n",
            "import math\n",
            "from math import fsum\n",
            "import numpy\n",
            "from statistics import mean\n",
        ],
    )
    def test_flags_float_usage(self, tmp_path: Path, source: str) -> None:
        assert check_file(write_module(tmp_path, source))

    def test_flags_a_float_hidden_in_a_decimal_constructor(self, tmp_path: Path) -> None:
        """The exact bug the rule exists to prevent: Decimal(0.35) is not 0.35."""
        path = write_module(tmp_path, "from decimal import Decimal\nFEE = Decimal(0.35)\n")
        violations = check_file(path)
        assert len(violations) == 1
        assert "0.35" in violations[0].message


class TestExemptions:
    def test_allows_rejecting_float_via_isinstance(self, tmp_path: Path) -> None:
        source = (
            "def guard(v: object) -> None:\n"
            "    if isinstance(v, float):\n"
            "        raise TypeError('no floats')\n"
        )
        assert not check_file(write_module(tmp_path, source))

    def test_allows_rejecting_float_via_issubclass(self, tmp_path: Path) -> None:
        source = (
            "def guard(t: type) -> None:\n"
            "    if issubclass(t, float):\n"
            "        raise TypeError('no floats')\n"
        )
        assert not check_file(write_module(tmp_path, source))

    def test_isinstance_exemption_does_not_leak_to_the_rest_of_the_file(
        self, tmp_path: Path
    ) -> None:
        source = (
            "def guard(v: object) -> None:\n"
            "    if isinstance(v, float):\n"
            "        raise TypeError('no floats')\n"
            "RATE = 0.35\n"
        )
        violations = check_file(write_module(tmp_path, source))
        assert len(violations) == 1
        assert violations[0].line == 4

    def test_marker_comment_suppresses_a_reviewed_line(self, tmp_path: Path) -> None:
        source = "LEGACY = 0.35  # money-path: allow -- justified in ADR-0003\n"
        assert not check_file(write_module(tmp_path, source))

    def test_marker_does_not_collide_with_ruff_noqa(self, tmp_path: Path) -> None:
        """A plain ruff suppression must not silence an FR-002 violation."""
        source = "LEGACY = 0.35  # noqa: E501\n"
        assert check_file(write_module(tmp_path, source))


class TestCleanCode:
    def test_decimal_arithmetic_is_not_flagged(self, tmp_path: Path) -> None:
        source = (
            "from decimal import Decimal\n"
            "CENT = Decimal('0.01')\n"
            "def total(price_cents: int, qty: int) -> Decimal:\n"
            "    return Decimal(price_cents) * CENT * Decimal(qty)\n"
        )
        assert not check_file(write_module(tmp_path, source))

    def test_integers_are_not_flagged(self, tmp_path: Path) -> None:
        assert not check_file(write_module(tmp_path, "MAX_PRICE_CENTS = 99\n"))

    def test_booleans_are_not_flagged(self, tmp_path: Path) -> None:
        assert not check_file(write_module(tmp_path, "LIVE = False\n"))
