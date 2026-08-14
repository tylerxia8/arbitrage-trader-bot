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


def _baskets(
    max_age_seconds: float, limit: int, event: str | None, since_hours: float | None
) -> int:
    """Report moments when a full leg set priced below its payout.

    Research, not detection: no relationship here is approved, fees are
    unmodelled, and only the best price level is used.
    """
    import datetime as dt

    from arbbot.analysis.baskets import scan_baskets
    from arbbot.db.session import session_factory

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    engine = create_engine_from_settings(settings)
    with session_factory(engine)() as session:
        since = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours)
            if since_hours is not None
            else None
        )
        result = scan_baskets(
            session,
            max_leg_age=dt.timedelta(seconds=max_age_seconds),
            since=since,
            event=event,
        )

    print(result.render(limit=limit))
    return 0


def _probe(interval: float, event_ticker: str | None) -> int:
    """Poll one event's legs at high frequency to measure how long edges last.

    Every profitable episode the archive has shown so far reports a duration of
    zero seconds, which only means "shorter than one thirty-second poll". One
    second and twenty-nine seconds imply opposite conclusions -- the first says
    nothing at retail latency can capture it, the second says the edge is real
    and the collector is too slow. This resolves which.
    """
    import asyncio
    import logging

    from arbbot.collection.collector import PROBE_CHANNEL
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

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    async def run() -> None:
        engine = create_engine_from_settings(settings)
        # A rate budget of its own, so the probe cannot throttle the broad
        # collector: both draw on the venue's bucket, and 6 requests a second
        # here leaves the collector's eight intact inside the ~20/s tier.
        async with KalshiRestClient(
            base_url=settings.venue_api_base, requests_per_second=6
        ) as client:
            resolver = UniverseResolver(client)
            legs = await resolver.resolve()
            if event_ticker:
                legs = [t for t in legs if t.startswith(event_ticker)]
            else:
                # Whichever complete partition the resolver lists first, taken
                # whole: a probe on part of a basket measures nothing.
                first_event = legs[0].rsplit("-", 1)[0] if legs else ""
                legs = [t for t in legs if t.rsplit("-", 1)[0] == first_event]

            if not legs:
                print("no live partition matched; nothing to probe", file=sys.stderr)
                return

            print(f"probing {len(legs)} legs of {legs[0].rsplit('-', 1)[0]} every {interval}s")
            print("archived under a separate channel, so the broad collector is unaffected")

            service = CollectionService(
                session_factory=session_factory(engine),
                client=client,
                tickers=legs,
                poll_interval_seconds=interval,
                channel=PROBE_CHANNEL,
                progress_interval_seconds=120.0,
            )
            service.resume_all()
            await service.run_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _maker(since_hours: float | None, event: str | None, max_age: float, horizon: float) -> int:
    """Replay the archive as a market maker rather than a taker.

    The taker verdict was negative on economics, not on latency, and the venue
    charges no maker fee on its standard series -- so the whole cost model this
    system has been fighting is the price of immediacy. This asks what quoting
    would have cost instead, and whether the quotes would have filled.
    """
    import datetime as dt

    from arbbot.analysis.maker import scan_maker_capacity
    from arbbot.db.session import session_factory

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    engine = create_engine_from_settings(settings)
    with session_factory(engine)() as session:
        since = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours)
            if since_hours is not None
            else None
        )
        report = scan_maker_capacity(
            session,
            since=since,
            event=event,
            max_leg_age=dt.timedelta(seconds=max_age),
            horizon=dt.timedelta(seconds=horizon),
        )

    print(report.render())
    return 0


def _relationships(action: str, args: argparse.Namespace) -> int:
    """Draft, list, and approve logical relationships.

    The approval step is deliberately awkward: it requires a named reviewer and
    a written record of what they read, and it will refuse without both. That
    is the point. Every arbitrage this system can detect is downstream of a
    claim that some set of contracts is mutually exclusive and collectively
    exhaustive, and nothing but a person reading the settlement terms
    establishes that.
    """
    import asyncio

    from arbbot.db.session import session_factory
    from arbbot.registry import (
        RelationshipRegistry,
        approve_group,
        fingerprint_of,
        group_pending,
        propose_from_events,
        slug_for,
    )
    from arbbot.venues.kalshi.rest import KalshiRestClient
    from arbbot.venues.kalshi.universe import TEMPERATURE_PREFIXES

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    engine = create_engine_from_settings(settings)
    factory = session_factory(engine)

    if action == "propose":

        async def fetch() -> list[tuple[dict[str, object], list[dict[str, object]]]]:
            async with KalshiRestClient(
                base_url=settings.venue_api_base, requests_per_second=6
            ) as client:
                series = (
                    await client.fetch("/series", {"category": "Climate and Weather"})
                ).payload
                pairs: list[tuple[dict[str, object], list[dict[str, object]]]] = []
                for entry in series.get("series", []):
                    ticker = entry.get("ticker")
                    if not isinstance(ticker, str) or not ticker.startswith(TEMPERATURE_PREFIXES):
                        continue
                    body = (
                        await client.fetch(
                            "/events",
                            {
                                "series_ticker": ticker,
                                "limit": 4,
                                "with_nested_markets": "true",
                                "status": "open",
                            },
                        )
                    ).payload
                    for event in body.get("events") or []:
                        markets = [
                            m for m in event.get("markets") or [] if m.get("status") == "active"
                        ]
                        if markets:
                            pairs.append((event, markets))
                return pairs

        print("resolving live events from the venue...")
        events = asyncio.run(fetch())
        with factory() as session:
            report = propose_from_events(session, events)
            session.commit()
        print(report.render())
        return 0

    if action == "list":
        with factory() as session:
            groups = group_pending(session)
            waiting = sum(len(records) for records in groups.values())
            if not waiting:
                print("nothing is waiting on a reviewer.")
                return 0

            print(f"{waiting} relationship(s) awaiting review, in {len(groups)} distinct claim(s).")
            print("Grouped by the settlement wording a reviewer would actually read: events")
            print("in one group are the same claim asked about different days.\n")

            for fingerprint, records in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                example = records[0]
                proof = dict(example.payout_proof)
                print(f"=== {len(records)} event(s), fingerprint {fingerprint[:12] or 'none'}")
                print(f"    representative : {example.slug} (version {example.version})")
                print(f"    claim          : {proof.get('claim', '')}")
                print(f"    coverage       : {proof.get('integer_coverage', '')}")
                for item in proof.get("reviewer_must_confirm", []):
                    print(f"    confirm        : {item}")
                # The masked templates, not the raw rules: these are what the
                # fingerprint was taken over, so showing anything else would ask
                # the reviewer to check one thing and sign for another.
                for template in proof.get("rules_templates", []):
                    print(f"    rule (masked)  : {template[:240]}")
                print("    events         : " + ", ".join(sorted(r.slug for r in records[:6])))
                if len(records) > 6:
                    print(f"                     ...and {len(records) - 6} more")
                print()

            print("Approve one event:")
            print("  arbbot relationships approve <slug> --reviewer NAME --evidence TEXT")
            print("Approve everything one reading covers (each still gets its own record):")
            print("  arbbot relationships approve <slug> --group --reviewer NAME --evidence TEXT")
        return 0

    if action == "approve":
        slug = args.slug if args.slug.startswith("kalshi:") else slug_for(args.slug)
        with factory() as session:
            registry = RelationshipRegistry(session)
            found = registry.latest(slug)
            if found is None:
                print(f"no relationship with slug {slug!r}", file=sys.stderr)
                return 1
            record = found

            try:
                if args.group:
                    approved = approve_group(
                        session,
                        fingerprint_of(record),
                        reviewer=args.reviewer,
                        evidence=args.evidence,
                    )
                else:
                    registry.approve(record, reviewer=args.reviewer, evidence=args.evidence)
                    approved = [record]
            except Exception as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1

            session.commit()
            print(f"approved {len(approved)} relationship(s), reviewer: {args.reviewer}")
            for item in approved[:10]:
                print(f"  {item.slug} v{item.version}, {len(item.dependency_hashes)} legs")
            if len(approved) > 10:
                print(f"  ...and {len(approved) - 10} more")
            print("")
            print("Each approval is bound to its own legs' settlement terms as they are")
            print("right now. If any leg's terms change, that relationship -- and only")
            print("that one -- suspends on the next proposal pass.")
        return 0

    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


def _falsify(quantity: str, research: bool, since_hours: float | None) -> int:
    """Replay the archive through the detector and report where candidates die."""
    import datetime as dt
    from decimal import Decimal

    from arbbot.analysis.falsification import run_falsification
    from arbbot.db.session import session_factory

    try:
        settings = load_settings()
    except ValidationError as exc:
        print("configuration          : INVALID", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    engine = create_engine_from_settings(settings)
    with session_factory(engine)() as session:
        since = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=since_hours)
            if since_hours is not None
            else None
        )
        report = run_falsification(
            session, quantity=Decimal(quantity), research_mode=research, since=since
        )

    print(report.render())
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

    baskets = subparsers.add_parser(
        "baskets", help="scan the archive for baskets priced below their payout (research)"
    )
    baskets.add_argument(
        "--max-leg-age",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="how far apart two legs' quotes may be and still count as one basket "
        "(default: 60). Loosening this manufactures edges from quotes that never coexisted.",
    )
    baskets.add_argument("--limit", type=int, default=15, help="rows to show")
    baskets.add_argument(
        "--event",
        default=None,
        help="restrict the scan to one event ticker, e.g. KXHIGHTDAL-26AUG14. Use this to "
        "read the fast-poll probe on its own: mixing a one-second stream with the "
        "thirty-second collector averages a measured duration with an unmeasured one.",
    )
    baskets.add_argument(
        "--since-hours",
        type=float,
        default=None,
        metavar="HOURS",
        help="only scan the last N hours",
    )

    maker = subparsers.add_parser(
        "maker", help="replay the archive as a market maker: what would quoting have cost?"
    )
    maker.add_argument("--since-hours", type=float, default=None, metavar="HOURS")
    maker.add_argument("--event", default=None, help="restrict to one event ticker")
    maker.add_argument(
        "--max-leg-age", type=float, default=2.0, metavar="SECONDS", help="freshness gate"
    )
    maker.add_argument(
        "--horizon",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="how long a resting order is given to fill (default: 120). A basket assembled "
        "over more than a couple of minutes is six directional bets, not an arbitrage.",
    )

    relationships = subparsers.add_parser(
        "relationships",
        help="draft, list, and approve the logical claims every arbitrage rests on",
    )
    relationship_actions = relationships.add_subparsers(dest="action", required=True)
    relationship_actions.add_parser(
        "propose", help="draft PENDING relationships from the venue's live event structure"
    )
    relationship_actions.add_parser("list", help="show everything awaiting a reviewer")
    approve = relationship_actions.add_parser(
        "approve", help="record a human's approval of one relationship"
    )
    approve.add_argument("slug", help="relationship slug, or the bare event ticker")
    approve.add_argument(
        "--group",
        action="store_true",
        help="also approve every other pending relationship this same reading covers -- "
        "the same settlement wording and bucket shape asked about a different day. Each "
        "still gets its own approval record, bound to its own legs' terms.",
    )
    approve.add_argument(
        "--reviewer",
        required=True,
        help="an authenticated human identity. Never a model, never a service account -- "
        "the point of this record is that a person is answerable for the claim.",
    )
    approve.add_argument(
        "--evidence",
        required=True,
        help="what was read: the settlement wording, the source URL, the quoted rule. "
        "An approval that cannot say what was confirmed is a rubber stamp with a name on it.",
    )

    probe = subparsers.add_parser(
        "probe",
        help="poll one event's legs at high frequency to measure how long edges last",
    )
    probe.add_argument(
        "--interval", type=float, default=1.0, help="seconds between polls (default: 1)"
    )
    probe.add_argument(
        "--event", default=None, help="event ticker to probe (default: first live partition)"
    )

    falsify = subparsers.add_parser(
        "falsify", help="replay the archive through the detector and report the funnel"
    )
    falsify.add_argument(
        "--quantity", default="10", help="basket size to price (default: 10 contracts)"
    )
    falsify.add_argument(
        "--since-hours",
        type=float,
        default=None,
        metavar="HOURS",
        help="only replay the last N hours. Use this to restrict the funnel to the window "
        "where poll cycles were recorded: outside it, quote age falls back to time-since-change "
        "and the staleness column means something different.",
    )
    falsify.add_argument(
        "--strict",
        action="store_true",
        help="require approved relationships and verified fees; without it the run "
        "prices structurally-discovered sets as if approved, and says so",
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
    if args.command == "baskets":
        return _baskets(args.max_leg_age, args.limit, args.event, args.since_hours)
    if args.command == "maker":
        return _maker(args.since_hours, args.event, args.max_leg_age, args.horizon)
    if args.command == "relationships":
        return _relationships(args.action, args)
    if args.command == "probe":
        return _probe(args.interval, args.event)
    if args.command == "falsify":
        return _falsify(args.quantity, not args.strict, args.since_hours)
    if args.command == "coverage":
        return _coverage()
    if args.command == "collect":
        if not args.tickers and not args.universe:
            parser.error("give tickers or --universe")
        return _collect(args.tickers, args.interval, args.universe)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover -- NoReturn


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
