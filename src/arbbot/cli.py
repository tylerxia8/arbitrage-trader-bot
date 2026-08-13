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


def _collect(tickers: list[str], poll_interval: float, use_universe: bool) -> int:
    """Run the polling collector until interrupted."""
    import asyncio
    import logging

    from arbbot.collection.service import CollectionService
    from arbbot.db.session import session_factory
    from arbbot.venues.kalshi.rest import KalshiRestClient
    from arbbot.venues.kalshi.universe import UniverseResolver

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    # Without this the service's progress heartbeat goes nowhere, and a
    # seven-day run shows three startup lines and then silence.
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every request at INFO. At 120 markets on a 30-second cycle
    # that is 240 lines a minute -- roughly 350,000 a day, which buries the
    # heartbeat completely and churns through the container's log cap in about
    # two days. The requests are not interesting; the failures are, and those
    # still come through at WARNING.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    async def run() -> None:
        engine = create_engine_from_settings(settings)
        async with KalshiRestClient(base_url=settings.venue_api_base) as client:
            resolver = UniverseResolver(client)
            if use_universe:
                # Left empty deliberately: the service resolves on its first
                # cycle. Pre-resolving here would run a full pass over every
                # temperature series twice at startup.
                print("resolving live temperature partitions from the venue...")

            service = CollectionService(
                session_factory=session_factory(engine),
                client=client,
                tickers=[] if use_universe else tickers,
                poll_interval_seconds=poll_interval,
                market_source=resolver.resolve if use_universe else None,
            )
            if use_universe:
                refresh = await service.refresh_markets()
                if refresh.failed:
                    print(f"could not resolve the universe: {refresh.failed}", file=sys.stderr)
                    return

            resumed = service.resume_all()
            carried = {t: s for t, s in resumed.items() if s > 0}
            if carried:
                print(f"resuming {len(carried)} stream(s) from the existing archive")

            # Count the collectors, not the argument list: with --universe the
            # tickers come from the venue, and reporting the empty input list
            # made a healthy collector announce "collecting 0 market(s)".
            print(
                f"collecting {len(service.collectors)} market(s) "
                f"every {poll_interval}s; Ctrl-C to stop"
            )
            await service.run_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _serve(host: str, port: int) -> int:
    """Serve the read-only operator API.

    The ``/health`` endpoint existed and was tested from the first day of this
    milestone, and nothing ran it -- a monitored endpoint nobody can reach is
    indistinguishable from no monitoring.
    """
    import uvicorn

    from arbbot.api.app import create_app

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    print(f"serving read-only API on http://{host}:{port}/health")
    uvicorn.run(create_app(settings), host=host, port=port, log_level=settings.log_level.lower())
    return 0


def _coverage() -> int:
    """Report continuous-collection coverage against the M1 exit gate."""
    from arbbot.collection.coverage import assess_coverage
    from arbbot.db.session import session_factory

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    engine = create_engine_from_settings(settings)
    with session_factory(engine)() as session:
        assessment = assess_coverage(session)

    print(assessment.render())
    # Non-zero until the gate is met, so this can gate a release check rather
    # than needing someone to read the table and decide.
    return 0 if assessment.meets_gate else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arbbot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"arbbot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate configuration and report execution gates")
    subparsers.add_parser(
        "coverage", help="report continuous-collection coverage against the M1 exit gate"
    )

    serve = subparsers.add_parser("serve", help="serve the read-only operator API (/health)")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    serve.add_argument("--port", type=int, default=8000)

    collect = subparsers.add_parser("collect", help="poll public order books and archive them")
    collect.add_argument(
        "tickers", nargs="*", help="market tickers to collect (omit with --universe)"
    )
    collect.add_argument(
        "--universe",
        action="store_true",
        help="resolve live temperature partitions from the venue and refresh them "
        "as markets rotate, instead of using a fixed ticker list",
    )
    collect.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between polls (default: 5). Opportunities shorter than "
        "this are invisible to the collector.",
    )

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "serve":
        return _serve(args.host, args.port)
    if args.command == "coverage":
        return _coverage()
    if args.command == "collect":
        if not args.tickers and not args.universe:
            parser.error("give tickers or --universe")
        return _collect(args.tickers, args.interval, args.universe)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover -- NoReturn


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
