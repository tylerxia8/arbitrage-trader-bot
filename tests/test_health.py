"""Feed health.

The distinction under test is between a quiet market and a dead socket. They
look identical in message counts, and conflating them means a collector that
silently stopped reports the same "nothing to see" as one watching a market
with no activity.
"""

from __future__ import annotations

import datetime as dt

import pytest

from arbbot.collection.health import DEFAULT_MAX_SILENCE, StreamHealth

T0 = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)


def stream() -> StreamHealth:
    return StreamHealth(venue="testvenue", subscription_key="orderbook:TEST")


class TestLag:
    def test_no_messages_means_no_lag(self) -> None:
        assert stream().lag(now=T0) is None

    def test_lag_measures_from_the_last_message(self) -> None:
        health = stream()
        health.observe_message(T0)
        assert health.lag(now=T0 + dt.timedelta(seconds=30)) == dt.timedelta(seconds=30)

    def test_lag_ms_is_whole_milliseconds(self) -> None:
        health = stream()
        health.observe_message(T0)
        assert health.lag_ms(now=T0 + dt.timedelta(milliseconds=1500)) == 1500

    def test_out_of_order_processing_cannot_rewind_health(self) -> None:
        """Under concurrency a message may be processed slightly late; health
        must not appear to travel backwards in time."""
        health = stream()
        health.observe_message(T0 + dt.timedelta(seconds=10))
        health.observe_message(T0)
        assert health.last_message_ts == T0 + dt.timedelta(seconds=10)

    def test_naive_timestamps_are_rejected(self) -> None:
        """A feed timestamp without a zone is not a time."""
        with pytest.raises(ValueError, match="timezone-aware"):
            stream().observe_message(dt.datetime(2026, 8, 12, 12, 0))  # noqa: DTZ001


class TestHealthiness:
    def test_a_stream_that_never_delivered_is_not_healthy(self) -> None:
        """Otherwise a collector that failed to subscribe at all looks exactly
        like one watching a quiet market."""
        assert not stream().is_healthy(now=T0)

    def test_recent_traffic_is_healthy(self) -> None:
        health = stream()
        health.observe_message(T0)
        assert health.is_healthy(now=T0 + dt.timedelta(seconds=5))

    def test_silence_beyond_the_threshold_is_unhealthy(self) -> None:
        """NFR-01: no silent outage longer than two minutes."""
        health = stream()
        health.observe_message(T0)
        assert not health.is_healthy(now=T0 + DEFAULT_MAX_SILENCE + dt.timedelta(seconds=1))

    def test_the_threshold_boundary_is_still_healthy(self) -> None:
        health = stream()
        health.observe_message(T0)
        assert health.is_healthy(now=T0 + DEFAULT_MAX_SILENCE)


class TestCounters:
    def test_messages_are_counted(self) -> None:
        health = stream()
        for i in range(3):
            health.observe_message(T0 + dt.timedelta(seconds=i))
        assert health.messages == 3

    def test_reconnect_resets_sequence_history_but_keeps_counters(self) -> None:
        """Anomaly counts describe the session, not the current socket."""
        health = stream()
        health.tracker.observe(1)
        health.tracker.observe(9)
        assert health.tracker.gaps == 1

        health.observe_reconnect()
        assert health.reconnects == 1
        assert health.tracker.gaps == 1
        assert health.tracker.last_sequence is None

    def test_parse_errors_are_counted(self) -> None:
        health = stream()
        health.observe_parse_error()
        assert health.parse_errors == 1


class TestSampling:
    def test_sample_captures_the_current_state(self) -> None:
        health = stream()
        health.observe_message(T0)
        health.tracker.observe(1)
        health.tracker.observe(5)
        health.observe_parse_error()

        row = health.sample(now=T0 + dt.timedelta(seconds=10))

        assert row.venue == "testvenue"
        assert row.subscription_key == "orderbook:TEST"
        assert row.messages == 1
        assert row.gaps == 1
        assert row.missing_messages == 3
        assert row.parse_errors == 1
        assert row.lag_ms == 10_000
        assert row.is_healthy is True

    def test_sample_of_a_dead_stream_records_it_as_unhealthy(self) -> None:
        """The sample is what makes an outage visible: a stopped collector
        leaves a hole in this table rather than writing nothing anywhere."""
        health = stream()
        health.observe_message(T0)
        row = health.sample(now=T0 + dt.timedelta(minutes=10))
        assert row.is_healthy is False
        assert row.lag_ms == 600_000
