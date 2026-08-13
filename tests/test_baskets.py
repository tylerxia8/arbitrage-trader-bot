"""Scanning the archive for sub-payout baskets.

Every test here corresponds to a way the first, hand-written version of this
scan was wrong -- and each wrong version reported a *larger* edge than the
correct one, which is the direction that gets money lost.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from arbbot.analysis.baskets import event_of, scan_baskets
from arbbot.db.models import BookSnapshot

T0 = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.UTC)
EVENT = "KXHIGHTEST-26AUG13"

D = Decimal


def snap(
    session: Session,
    leg: str,
    *,
    yes_ask: str,
    size: str = "1000",
    at: dt.datetime = T0,
    complete: bool = True,
) -> None:
    """Store a snapshot whose implied YES ask is ``yes_ask``.

    The book stores NO bids; a YES ask of $0.79 means a NO bid at $0.21.
    """
    no_bid = D("1.00") - D(yes_ask)
    session.add(
        BookSnapshot(
            venue="kalshi",
            ticker=f"{EVENT}-{leg}",
            captured_ts=at,
            sequence=1,
            yes_levels={},
            no_levels={f"{no_bid:.4f}": size},
            checksum="x" * 64,
            is_complete=complete,
        )
    )
    session.flush()


def full_set(
    session: Session,
    asks: list[str],
    *,
    at: dt.datetime = T0,
    complete: bool = True,
) -> None:
    for index, ask in enumerate(asks):
        snap(session, f"L{index}", yes_ask=ask, at=at, complete=complete)


class TestPricing:
    def test_a_dear_basket_is_not_reported(self, session: Session) -> None:
        full_set(session, ["0.30", "0.40", "0.35"])
        result = scan_baskets(session)
        assert result.priced >= 1
        assert result.observations == []

    def test_a_cheap_basket_is_reported(self, session: Session) -> None:
        full_set(session, ["0.30", "0.30", "0.30"])
        result = scan_baskets(session)
        assert len(result.observations) == 1
        assert result.observations[0].cost == D("0.90")
        assert result.observations[0].gross_edge == D("0.10")

    def test_the_ask_is_derived_from_the_opposite_side(self, session: Session) -> None:
        """A NO bid at $0.79 is an offer to sell YES at $0.21. Reading it as a
        YES bid would halve every basket and invent edge everywhere."""
        full_set(session, ["0.21", "0.21", "0.21"])
        assert scan_baskets(session).observations[0].cost == D("0.63")


class TestStaleness:
    def test_legs_from_different_moments_are_refused(self, session: Session) -> None:
        """The $0.38 Boston bug: a 16:06 quote summed with a 20:44 one on a
        market that reprices all day."""
        snap(session, "L0", yes_ask="0.01", at=T0)
        snap(session, "L1", yes_ask="0.01", at=T0)
        snap(session, "L2", yes_ask="0.01", at=T0 + dt.timedelta(hours=4))

        result = scan_baskets(session)
        assert result.observations == []
        assert result.skipped_stale >= 1

    def test_legs_within_the_window_are_priced(self, session: Session) -> None:
        snap(session, "L0", yes_ask="0.30", at=T0)
        snap(session, "L1", yes_ask="0.30", at=T0 + dt.timedelta(seconds=10))
        snap(session, "L2", yes_ask="0.30", at=T0 + dt.timedelta(seconds=20))
        assert len(scan_baskets(session).observations) == 1

    def test_the_window_is_configurable(self, session: Session) -> None:
        snap(session, "L0", yes_ask="0.30", at=T0)
        snap(session, "L1", yes_ask="0.30", at=T0)
        snap(session, "L2", yes_ask="0.30", at=T0 + dt.timedelta(minutes=5))

        assert scan_baskets(session).observations == []
        assert scan_baskets(session, max_leg_age=dt.timedelta(minutes=10)).observations


class TestCapacity:
    def test_size_is_the_smallest_leg(self, session: Session) -> None:
        """A basket cannot exceed its thinnest leg."""
        snap(session, "L0", yes_ask="0.30", size="10000")
        snap(session, "L1", yes_ask="0.30", size="4")
        snap(session, "L2", yes_ask="0.30", size="500")

        assert scan_baskets(session).observations[0].max_contracts == D("4")

    def test_gross_dollars_deflates_a_thin_basket(self, session: Session) -> None:
        """The Philadelphia case: a 16% edge that is worth 64 cents, because
        the discount lives entirely in a leg quoted for four contracts."""
        snap(session, "L0", yes_ask="0.01", size="170000")
        snap(session, "L1", yes_ask="0.01", size="63000")
        snap(session, "L2", yes_ask="0.79", size="4")
        snap(session, "L3", yes_ask="0.01", size="3222")
        snap(session, "L4", yes_ask="0.01", size="190000")
        snap(session, "L5", yes_ask="0.01", size="10221")

        observation = scan_baskets(session).observations[0]
        assert observation.cost == D("0.84")
        assert observation.gross_edge == D("0.16")
        assert observation.max_contracts == D("4")
        assert observation.gross_dollars == D("0.64")

    def test_size_does_not_rescue_a_thin_edge(self, session: Session) -> None:
        """The economics that decide this whole strategy.

        The fee is proportional to size, so size never amortises it away --
        only the per-trade rounding floor shrinks. What must clear the fee is
        the edge *per basket*.

        Here the deep basket has $36 of gross edge across 900 contracts and
        still loses $5.13, because at $0.32 a contract the fee is 1.47 cents
        per contract per leg and the edge is only 4 cents across three legs.
        The four-contract basket, with a 10-cent edge, keeps 22 cents.
        """
        snap(session, "L0", yes_ask="0.30", size="4", at=T0)
        snap(session, "L1", yes_ask="0.30", size="4", at=T0)
        snap(session, "L2", yes_ask="0.30", size="4", at=T0)

        # Back above payout in between, so these are two separate episodes
        # rather than one continuous stretch of cheapness.
        between = T0 + dt.timedelta(minutes=5)
        snap(session, "L0", yes_ask="0.40", size="10", at=between)
        snap(session, "L1", yes_ask="0.40", size="10", at=between)
        snap(session, "L2", yes_ask="0.40", size="10", at=between)

        later = T0 + dt.timedelta(minutes=10)
        snap(session, "L0", yes_ask="0.32", size="900", at=later)
        snap(session, "L1", yes_ask="0.32", size="900", at=later)
        snap(session, "L2", yes_ask="0.32", size="900", at=later)

        episodes = {e.best_cost: e for e in scan_baskets(session).episodes}
        deep = episodes[D("0.96")]
        thin = episodes[D("0.90")]

        assert deep.best_dollars == D("36.00")
        assert deep.best_net == D("-5.13"), "gross size is not profit"
        assert thin.best_net == D("0.22")

        best = scan_baskets(session).best
        assert best is not None
        assert best.best_cost == D("0.90"), "ranked on what survives fees"


class TestCompleteness:
    def test_a_partial_set_is_not_priced(self, session: Session) -> None:
        """Summing five legs of six understates the basket and invents a
        discount out of a leg that was simply never collected."""
        full_set(session, ["0.30", "0.30", "0.30"])
        snap(session, "L3", yes_ask="0.30", at=T0 + dt.timedelta(minutes=30))

        # L3 joins the known set, so earlier three-leg pricings are incomplete.
        result = scan_baskets(session)
        assert result.skipped_incomplete >= 1

    def test_a_leg_with_no_offer_blocks_the_basket(self, session: Session) -> None:
        """Nobody offering means the basket cannot be assembled at any price."""
        snap(session, "L0", yes_ask="0.30")
        snap(session, "L1", yes_ask="0.30")
        session.add(
            BookSnapshot(
                venue="kalshi",
                ticker=f"{EVENT}-L2",
                captured_ts=T0,
                sequence=1,
                yes_levels={},
                no_levels={},
                checksum="x" * 64,
                is_complete=True,
            )
        )
        session.flush()
        assert scan_baskets(session).observations == []

    def test_incomplete_books_are_ignored(self, session: Session) -> None:
        """A book with a sequence gap is not evidence of a price."""
        full_set(session, ["0.30", "0.30", "0.30"], complete=False)
        assert scan_baskets(session).observations == []


class TestGrouping:
    def test_event_is_the_ticker_without_its_leg(self) -> None:
        assert event_of("KXHIGHTATL-26AUG13-T92") == "KXHIGHTATL-26AUG13"

    def test_separate_events_do_not_mix(self, session: Session) -> None:
        full_set(session, ["0.30", "0.30", "0.30"])
        for leg in ("A", "B"):
            session.add(
                BookSnapshot(
                    venue="kalshi",
                    ticker=f"KXHIGHOTHER-26AUG13-{leg}",
                    captured_ts=T0,
                    sequence=1,
                    yes_levels={},
                    no_levels={"0.9900": "10"},
                    checksum="y" * 64,
                    is_complete=True,
                )
            )
        session.flush()

        result = scan_baskets(session)
        assert result.events_seen == 2
        assert {o.event for o in result.observations} == {EVENT, "KXHIGHOTHER-26AUG13"}

    def test_a_single_market_is_not_a_basket(self, session: Session) -> None:
        """A lone contract below a dollar is the ordinary state of almost every
        contract, not a 99% edge."""
        snap(session, "ONLY", yes_ask="0.01")
        assert scan_baskets(session).observations == []


class TestEventFilter:
    def test_the_scan_can_be_narrowed_to_one_event(self, session: Session) -> None:
        """The probe covers one event at one second while the collector covers
        a hundred and twenty at thirty. Reading them together averages a
        measured duration together with an unmeasured one."""
        full_set(session, ["0.30", "0.30", "0.30"])
        for leg in ("A", "B"):
            session.add(
                BookSnapshot(
                    venue="kalshi",
                    ticker=f"KXHIGHOTHER-26AUG13-{leg}",
                    captured_ts=T0,
                    sequence=1,
                    yes_levels={},
                    no_levels={"0.9900": "10"},
                    checksum="y" * 64,
                    is_complete=True,
                )
            )
        session.flush()

        result = scan_baskets(session, event=EVENT)
        assert result.events_seen == 1
        assert {o.event for o in result.observations} == {EVENT}

    def test_a_prefix_does_not_match_a_longer_event(self, session: Session) -> None:
        """``KXHIGHNY-26AUG14`` must not sweep in ``KXHIGHNY-26AUG14X``."""
        full_set(session, ["0.30", "0.30", "0.30"])
        assert scan_baskets(session, event=EVENT[:-1]).observations == []


class TestSurvivalCurve:
    def test_a_single_sample_is_not_reported_as_zero_duration(self, session: Session) -> None:
        """The whole reason the probe exists: a duration of zero from one
        observation means "shorter than one poll", not "instantaneous"."""
        full_set(session, ["0.30", "0.30", "0.30"])
        result = scan_baskets(session)
        episode = result.episodes[0]

        assert episode.is_single_observation
        assert dict((label, n) for label, n, _ in result.survival_curve())["single sample"] == 1
        assert dict((label, n) for label, n, _ in result.survival_curve())["<= 2s"] == 0

    def test_a_measured_episode_lands_in_a_duration_bucket(self, session: Session) -> None:
        full_set(session, ["0.30", "0.30", "0.30"], at=T0)
        full_set(session, ["0.31", "0.30", "0.30"], at=T0 + dt.timedelta(seconds=4))
        curve = dict((label, n) for label, n, _ in scan_baskets(session).survival_curve())

        assert curve["single sample"] == 0
        assert curve["<= 5s"] == 1

    def test_the_curve_separates_the_ones_that_survive_fees(self, session: Session) -> None:
        """Duration only matters for episodes that were worth taking. A long
        run of a basket that loses money to fees is not an opportunity."""
        full_set(session, ["0.30", "0.33", "0.33"], at=T0)
        full_set(session, ["0.30", "0.33", "0.34"], at=T0 + dt.timedelta(seconds=4))
        rows = [row for row in scan_baskets(session).survival_curve() if row[1]]

        assert rows
        for _, total, still_positive in rows:
            assert still_positive <= total

    def test_the_report_explains_the_single_sample_bucket(self, session: Session) -> None:
        full_set(session, ["0.30", "0.30", "0.30"])
        rendered = scan_baskets(session).render()
        assert "how long they lasted" in rendered
        assert "not measured" in rendered


class TestReporting:
    def test_report_states_that_nothing_is_tradeable(self, session: Session) -> None:
        full_set(session, ["0.30", "0.30", "0.30"])
        rendered = scan_baskets(session).render()
        assert "TAKER" in rendered
        assert "Slippage, latency and capital" in rendered
        assert "no" in rendered
        assert "relationship here has been approved" in rendered

    def test_an_empty_scan_says_so_plainly(self, session: Session) -> None:
        assert "nothing priced below its payout" in scan_baskets(session).render()
