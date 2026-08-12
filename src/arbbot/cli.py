"""Command-line entry point.

``arbbot doctor`` is the one-command answer to "what is this deployment
allowed to do right now". It prints the state of both FR-016 gates explicitly,
because the failure mode worth preventing is an operator believing the system
is armed when it is not, or -- far worse -- the reverse.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError
from sqlalchemy import text

from arbbot import __version__, buildflags
from arbbot.config import Settings, load_settings
from arbbot.db.session import create_engine_from_settings

__all__ = ["main"]


def _print_gates(settings: Settings) -> None:
    print("execution gates (FR-016)")
    print(f"  build flag  LIVE_EXECUTION_COMPILED_IN : {buildflags.LIVE_EXECUTION_COMPILED_IN}")
    print(f"  build flag  DEMO_EXECUTION_COMPILED_IN : {buildflags.DEMO_EXECUTION_COMPILED_IN}")
    print(f"  runtime     live_trading_enabled       : {settings.live_trading_enabled}")
    print(f"  environment                            : {settings.environment.value}")
    print(f"  -> may submit live orders              : {settings.may_submit_live_orders()}")
    print("  -> per-basket human approval is required in addition to the above")


def _print_limits(settings: Settings) -> None:
    limits = settings.risk
    print("risk limits (section 22)")
    print(f"  max order notional     : ${limits.max_order_notional_usd}")
    print(f"  max unmatched exposure : ${limits.max_unmatched_exposure_usd}")
    print(f"  max total open exposure: ${limits.max_total_open_exposure_usd}")
    print(f"  daily loss limit       : ${limits.daily_loss_limit_usd}")
    print(f"  min net edge           : ${limits.min_net_edge_usd}")
    print(f"  max quote age          : {limits.max_quote_age_ms} ms")


def _check_database(settings: Settings) -> bool:
    try:
        engine = create_engine_from_settings(settings)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    # doctor reports problems, it does not raise them: an unreachable database
    # is exactly what the operator ran this command to find out about.
    except Exception as exc:
        print(f"database               : UNREACHABLE ({type(exc).__name__}: {exc})")
        return False
    print("database               : reachable")
    return True


def _doctor() -> int:
    print(f"arbbot {__version__}")
    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    print("configuration          : valid")
    ok = _check_database(settings)
    print()
    _print_gates(settings)
    print()
    _print_limits(settings)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arbbot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"arbbot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate configuration and report execution gates")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover -- NoReturn


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
