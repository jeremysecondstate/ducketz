"""Offline native-history fixtures for rolling UTC price changes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import rolling_price_change as prices
from technicals.split_adjustments import SplitEvent

UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


AS_OF = instant("2026-10-28T17:00:30Z")


def candle(end, close, *, volume=10):
    start = instant(end) - timedelta(minutes=1)
    return {"datetime": int(start.timestamp() * 1000), "open": close, "high": close,
            "low": close, "close": close, "volume": volume}


def payload(*rows, symbol=None):
    return {"candles": list(rows), **({"symbol": symbol} if symbol else {})}


def observations(*rows, as_of=AS_OF):
    return prices.observations_from_payload(payload(*rows), sessions=prices.market_sessions(as_of))


def calculate(*rows, as_of=AS_OF, fresh_history=False):
    return prices.calculate_change("TEST", observations(*rows, as_of=as_of),
                                   as_of=as_of, sessions=prices.market_sessions(as_of), fresh_history=fresh_history)


def write_profile(root, symbols, *, local=True):
    target = root / "datafetching" / ("watchlist.local.txt" if local else "watchlist.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(symbols), encoding="utf-8")


def test_signed_arithmetic_uses_unrounded_values_and_latest_eligible_completed_close():
    row = calculate(candle("2026-10-27T17:00:00Z", 100.12345),
                    candle("2026-10-28T16:59:00Z", 101.23456),
                    candle("2026-10-28T17:00:00Z", 102.34567),
                    candle("2026-10-28T17:01:00Z", 1000))
    assert row.status == "available"
    assert row.change_pct == pytest.approx(100 * (102.34567 / 100.12345 - 1))
    assert row.current.observed_at == instant("2026-10-28T17:00:00Z")
    assert row.previous.observed_at == instant("2026-10-27T17:00:00Z")
    assert row.current.interval_start == instant("2026-10-28T16:59:00Z")


@pytest.mark.parametrize("current,expected", [(101, 1), (99, -1), (100, 0)])
def test_positive_negative_and_zero_are_actual_price_returns(current, expected):
    row = calculate(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", current))
    assert row.change_pct == pytest.approx(expected)


def test_baseline_candle_crossing_cutoff_is_never_selected():
    row = calculate(candle("2026-10-27T16:59:00Z", 100), candle("2026-10-27T17:01:00Z", 999),
                    candle("2026-10-28T17:00:00Z", 102))
    assert row.change_pct == pytest.approx(2)


def test_unfinished_or_future_candle_cannot_contaminate_normalization():
    raw = payload(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", 102),
                  candle("2026-10-28T17:01:00Z", 100000))
    result = prices.normalize_known_splits(raw, as_of=AS_OF)
    assert len(result["candles"]) == 2


@pytest.mark.parametrize("stamp", [
    datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
    datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=ZoneInfo("America/Los_Angeles")),
])
def test_window_is_24_elapsed_hours_across_dst(stamp):
    normalized = prices.utc(stamp)
    cutoff = normalized - timedelta(hours=24)
    snapshot = prices.unavailable_snapshot(stamp, "fixture", symbols=("TEST",))
    assert snapshot.as_of == normalized
    assert (snapshot.as_of - cutoff).total_seconds() == 86400
    # Local wall time differs by one hour across the clock transition.
    assert cutoff.astimezone(ZoneInfo("America/Los_Angeles")).hour != stamp.hour


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError, match="aware"):
        prices.utc(datetime(2026, 10, 28))


def test_weekend_closure_carries_actual_close_at_both_cutoffs_without_fabrication():
    stamp = instant("2026-11-01T18:00:00Z")
    row = calculate(candle("2026-10-31T00:00:00Z", 100), as_of=stamp)
    assert row.status == "closed" and row.change_pct == 0
    assert row.current == row.previous
    assert row.current.observed_at == instant("2026-10-31T00:00:00Z")


def test_weekend_does_not_hide_missing_friday_afternoon():
    stamp = instant("2026-11-01T18:00:00Z")
    row = calculate(candle("2026-10-30T20:00:00Z", 100), as_of=stamp)
    assert row.status == "stale" and row.change_pct is None


def test_holiday_is_distinguished_from_missing_open_session():
    stamp = instant("2026-11-26T18:00:00Z")
    row = calculate(candle("2026-11-25T18:00:00Z", 100), candle("2026-11-26T01:00:00Z", 102), as_of=stamp)
    assert row.status == "closed" and row.change_pct == pytest.approx(2)


def test_early_close_keeps_actual_extended_prices_to_17_eastern():
    stamp = instant("2026-11-27T22:10:00Z")
    sessions = prices.market_sessions(stamp)
    early = next(s for s in sessions if s.expected_close.date() == stamp.date())
    assert early.expected_open == instant("2026-11-27T09:00:00Z")
    assert early.expected_close == instant("2026-11-27T22:00:00Z")
    result = prices.observations_from_payload(payload(candle("2026-11-27T21:59:00Z", 100),
        candle("2026-11-27T22:01:00Z", 999)), sessions=sessions)
    assert len(result) == 1 and result[0].price == 100


def test_early_premarket_supported_observations_are_admitted():
    stamp = instant("2026-10-28T09:00:00Z")
    row = calculate(candle("2026-10-27T09:00:00Z", 100), candle("2026-10-28T09:00:00Z", 101), as_of=stamp)
    assert row.change_pct == pytest.approx(1)


def test_missing_early_premarket_cannot_be_mislabeled_closed():
    stamp = instant("2026-10-28T10:30:00Z")
    row = calculate(candle("2026-10-27T10:30:00Z", 100), candle("2026-10-28T08:01:00Z", 101), as_of=stamp)
    assert row.status == "stale" and row.change_pct is None


def test_fresh_server_reply_keeps_sparse_premarket_actual_price_without_fabrication():
    stamp = instant("2026-10-28T10:30:00Z")
    row = calculate(candle("2026-10-27T10:30:00Z", 100), candle("2026-10-28T08:01:00Z", 101),
                    as_of=stamp, fresh_history=True)
    assert row.status == "available" and row.change_pct == pytest.approx(1)
    assert row.current.observed_at == instant("2026-10-28T08:01:00Z")
    assert "Fresh server history" in row.reason


def test_fresh_sparse_after_hours_reply_preserves_actual_close_at_each_cutoff():
    stamp = instant("2026-10-29T04:03:00Z")  # Oct28,21:03 Pacific.
    row = calculate(candle("2026-10-27T23:28:00Z", 100), candle("2026-10-28T20:11:00Z", 101),
                    as_of=stamp, fresh_history=True)
    assert row.status == "closed" and row.change_pct == pytest.approx(1)
    assert row.current.observed_at == instant("2026-10-28T20:11:00Z")
    assert row.previous.observed_at == instant("2026-10-27T23:28:00Z")
    assert "sparse extended hours" in row.reason


def test_old_unverified_observations_do_not_gain_fresh_server_exemption():
    stamp = instant("2026-10-29T04:03:00Z")
    row = calculate(candle("2026-10-27T23:28:00Z", 100), candle("2026-10-28T20:11:00Z", 101), as_of=stamp)
    assert row.status == "stale" and row.change_pct is None


@pytest.mark.parametrize("stamp", ["2026-10-29T04:03:00Z", "2026-11-01T18:00:00Z"])
def test_fresh_server_reply_does_not_hide_a_missed_full_regular_session(stamp):
    row = calculate(candle("2026-10-26T23:00:00Z", 100), candle("2026-10-27T23:00:00Z", 101),
                    as_of=instant(stamp), fresh_history=True)
    assert row.status == "stale" and row.change_pct is None
    assert "regular-session" in row.reason


def test_fresh_reply_regular_session_age_limit_still_applies():
    row = calculate(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T16:49:00Z", 101), fresh_history=True)
    assert row.status == "stale" and row.change_pct is None


def test_more_than_ten_open_minutes_is_stale_but_ten_is_permitted():
    okay = calculate(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T16:51:00Z", 101))
    stale = calculate(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T16:50:00Z", 101))
    assert okay.status == "available"
    assert stale.status == "stale" and stale.change_pct is None


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 0, -1])
def test_invalid_or_missing_prices_never_become_zero(invalid):
    row = calculate(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", invalid))
    assert row.change_pct is None and row.status in {"stale", "unavailable"}


def test_zero_volume_candles_do_not_fabricate_a_trade():
    row = calculate(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", 101, volume=0))
    assert row.change_pct is None


def test_inconsistent_source_or_adjustment_basis_fails_closed():
    base = prices.PriceObservation(100, AS_OF - timedelta(hours=24), basis="raw")
    current = prices.PriceObservation(101, AS_OF, basis="split-adjusted")
    row = prices.calculate_change("TEST", (base, current), as_of=AS_OF, sessions=prices.market_sessions(AS_OF))
    assert row.status == "unavailable" and row.change_pct is None


def test_equal_weight_average_requires_every_member_and_never_rounds_inputs():
    rows = tuple(prices.SymbolPriceChange(f"S{i}", value, "available", "fixture")
                 for i, value in enumerate([1.004] * 10 + [-1.005]))
    full = prices.RollingPriceSnapshot(AS_OF, rows)
    assert full.average_pct == pytest.approx((10 * 1.004 - 1.005) / 11)
    missing = prices.RollingPriceSnapshot(AS_OF, rows[:-1] + (prices.SymbolPriceChange("S10", None, "unavailable", "missing"),))
    assert missing.available_count == 10 and missing.total_count == 11
    assert missing.average_pct is None and missing.partial_average_pct == pytest.approx(1.004)


@pytest.mark.parametrize("symbols", [
    ("DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN"),
    ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ"),
])
def test_both_pc_production_profiles_are_local_and_ignore_candidate_environment(tmp_path, monkeypatch, symbols):
    write_profile(tmp_path, symbols)
    override = tmp_path / "candidate.txt"
    override.write_text("FOREIGN\n", encoding="utf-8")
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", str(override))
    assert prices.local_symbols(tmp_path) == symbols


def test_fallback_shared_watchlist_still_works(tmp_path):
    write_profile(tmp_path, ("AAPL", "NVDA"), local=False)
    assert prices.local_symbols(tmp_path) == ("AAPL", "NVDA")


def test_load_reads_fresh_history_every_time_and_never_depends_on_order_or_review(tmp_path):
    write_profile(tmp_path, ("AAA", "BBB"))
    calls = []
    values = {"price": 101}

    class Session:
        def get_price_history(self, symbol, **kwargs):
            calls.append((symbol, kwargs))
            return payload(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", values["price"]), symbol=symbol)

    first = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path,
        split_fetcher=lambda _: (), session_factory=Session)
    values["price"] = 102
    second = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path,
        split_fetcher=lambda _: (), session_factory=Session)
    assert first.average_pct == pytest.approx(1) and second.average_pct == pytest.approx(2)
    assert len(calls) == 4 and first.symbols == second.symbols == ("AAA", "BBB")
    for _, request in calls:
        assert request["frequency"] == 1 and request["frequency_type"] == "minute"
        assert request["need_extended_hours_data"] is True and request["period"] is None
        assert request["end_datetime"] == AS_OF


def test_fresh_server_sparse_eleven_symbol_after_hours_snapshot_has_full_average(tmp_path):
    symbols = ("DOCU", "DBX", "SDGR", "QBTS", "PYPL", "GLOB", "OUST", "ABCL", "MRNA", "RR", "PDYN")
    write_profile(tmp_path, symbols)
    stamp = instant("2026-10-29T04:03:00Z")
    current_times = {symbol: "23:58:00" for symbol in symbols}
    current_times.update(DBX="20:11:00", GLOB="20:55:00")
    previous_times = {symbol: "23:58:00" for symbol in symbols}
    previous_times.update(SDGR="23:28:00", PYPL="23:49:00", ABCL="23:42:00", PDYN="23:37:00")
    requests = []

    class Session:
        def get_price_history(self, symbol, **kwargs):
            requests.append((symbol, kwargs["end_datetime"]))
            return payload(candle(f"2026-10-27T{previous_times[symbol]}Z", 100),
                           candle(f"2026-10-28T{current_times[symbol]}Z", 101), symbol=symbol)

    snapshot = prices.load_rolling_price_change(as_of=stamp, repository_root=tmp_path,
        session_factory=Session, split_fetcher=lambda _: [])
    assert snapshot.available_count == snapshot.total_count == 11
    assert snapshot.average_pct == pytest.approx(1)
    assert all(row.status == "closed" for row in snapshot.rows)
    dbx = next(row for row in snapshot.rows if row.symbol == "DBX")
    assert dbx.current.observed_at == instant("2026-10-28T20:11:00Z")
    assert set(requests) == {(symbol, stamp) for symbol in symbols}


def test_provider_failure_preserves_membership_and_missing_average(tmp_path):
    write_profile(tmp_path, ("AAA", "BBB"))

    def broken():
        raise OSError("secret provider body must not be displayed")

    result = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path,
        split_fetcher=lambda _: (), session_factory=broken)
    assert result.symbols == ("AAA", "BBB") and result.average_pct is None
    assert all(row.status == "unavailable" and "secret" not in row.reason for row in result.rows)


def test_bounded_timeout_does_not_start_remaining_requests_after_return(tmp_path):
    write_profile(tmp_path, ("AAA", "BBB", "CCC", "DDD", "EEE"))
    release = threading.Event()
    started = []

    class SlowSession:
        def get_price_history(self, symbol, **_kwargs):
            started.append(symbol)
            release.wait(2)
            return payload()

    try:
        start = time.monotonic()
        result = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path,
            split_fetcher=lambda _: (), session_factory=SlowSession, timeout_seconds=.08)
        assert time.monotonic() - start < 1
        assert len(started) == 3
        assert result.available_count == 0 and result.total_count == 5
    finally:
        release.set()
    time.sleep(.03)
    assert len(started) == 3


def test_known_raw_split_is_normalized_once_and_already_reflected_is_preserved():
    event = SplitEvent(pd.Timestamp("2026-10-28", tz="UTC"), 4, 1, Path("fixture.parquet"))
    raw = payload(candle("2026-10-27T17:00:00Z", 400), candle("2026-10-28T17:00:00Z", 102))
    adjusted = prices.normalize_known_splits(raw, events=(event,))
    row = prices.calculate_change("TEST", prices.observations_from_payload(adjusted, sessions=prices.market_sessions(AS_OF)),
                                  as_of=AS_OF, sessions=prices.market_sessions(AS_OF))
    assert row.change_pct == pytest.approx(2)
    again = prices.normalize_known_splits(adjusted, events=(event,))
    assert again["candles"][0]["close"] == 100


def test_ambiguous_or_unexplained_split_sized_discontinuity_is_unavailable():
    event = SplitEvent(pd.Timestamp("2026-10-28", tz="UTC"), 4, 1, Path("fixture.parquet"))
    ambiguous = payload(candle("2026-10-27T17:00:00Z", 200), candle("2026-10-28T17:00:00Z", 102))
    with pytest.raises(ValueError, match="Ambiguous split"):
        prices.normalize_known_splits(ambiguous, events=(event,))
    with pytest.raises(ValueError, match="Unexplained"):
        prices.normalize_known_splits(payload(candle("2026-10-27T17:00:00Z", 1000), candle("2026-10-28T17:00:00Z", 100)))


def test_winter_split_effective_date_preserves_previous_evening_extended_trades():
    # Nov9's ex-date is not 00:00 UTC: Nov8 trading continues to 01:00 UTC.
    event = SplitEvent(pd.Timestamp("2026-11-09", tz="UTC"), 4, 1, Path("fixture.parquet"))
    raw = payload(candle("2026-11-09T00:59:00Z", 400), candle("2026-11-09T09:01:00Z", 100))
    result = prices.normalize_known_splits(raw, events=(event,))
    assert result["candles"][0]["close"] == 100
    assert result["candles"][1]["close"] == 100


def test_small_split_with_overlapping_raw_adjusted_tolerances_is_unavailable():
    event = SplitEvent(pd.Timestamp("2026-10-28", tz="UTC"), 11, 10, Path("fixture.parquet"))
    raw = payload(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", 101))
    with pytest.raises(ValueError, match="both adjusted and raw"):
        prices.normalize_known_splits(raw, events=(event,))


@pytest.mark.parametrize("raw", [[], (), {"symbol": "AAA", "historical": []}])
def test_successful_empty_fresh_split_response_is_valid(raw):
    assert prices.split_events_from_payload("AAA", raw) == ()


@pytest.mark.parametrize("raw", [None, {}, {"error": "secret error"}, [None],
    [{"date": "2026-10-28", "numerator": 2}],
    [{"date": "2026-10-28", "numerator": 2, "denominator": 0}],
    [{"date": "2026-10-28", "numerator": 2, "denominator": 1, "symbol": "OTHER"}]])
def test_missing_or_invalid_fresh_split_response_cannot_substitute_no_splits(raw):
    with pytest.raises((ValueError, TypeError)):
        prices.split_events_from_payload("AAA", raw)


def test_fresh_two_for_one_split_is_comparable_and_not_a_fifty_percent_loss(tmp_path):
    write_profile(tmp_path, ("AAA",))
    split_calls = []

    class Session:
        def get_price_history(self, symbol, **_kwargs):
            return payload(candle("2026-10-27T17:00:00Z", 200), candle("2026-10-28T17:00:00Z", 101))

    def split_fetcher(symbol):
        split_calls.append(symbol)
        return [{"symbol": symbol, "date": "2026-10-28", "numerator": 2, "denominator": 1}]

    result = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path,
        session_factory=Session, split_fetcher=split_fetcher)
    assert split_calls == ["AAA"] and result.average_pct == pytest.approx(1)
    assert "FMP" in result.source and "FMP" in result.rows[0].current.basis


def test_split_failure_or_unknown_data_makes_full_average_unavailable(tmp_path):
    write_profile(tmp_path, ("AAA", "BBB"))

    class Session:
        def get_price_history(self, symbol, **_kwargs):
            return payload(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T17:00:00Z", 101))

    def split_fetcher(symbol):
        if symbol == "BBB":
            raise RuntimeError("secret credential context must not leak")
        return []

    result = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path,
        session_factory=Session, split_fetcher=split_fetcher)
    assert result.available_count == 1 and result.total_count == 2 and result.average_pct is None
    assert result.rows[1].reason == "Fresh split evidence unavailable (RuntimeError)"


def test_zero_volume_synthetic_prices_do_not_contaminate_split_validation():
    raw = payload(candle("2026-10-27T17:00:00Z", 100), candle("2026-10-28T16:00:00Z", 1000, volume=0),
                  candle("2026-10-28T17:00:00Z", 101))
    result = prices.normalize_known_splits(raw, as_of=AS_OF)
    assert len(result["candles"]) == 2
    row = prices.calculate_change("TEST", prices.observations_from_payload(result, sessions=prices.market_sessions(AS_OF)),
                                  as_of=AS_OF, sessions=prices.market_sessions(AS_OF))
    assert row.change_pct == pytest.approx(1)


def test_calendar_failure_is_explicit_and_preserves_watchlist(tmp_path, monkeypatch):
    write_profile(tmp_path, ("AAA", "BBB"))
    monkeypatch.setattr(prices, "market_sessions", lambda *_: (_ for _ in ()).throw(ValueError("fixture")))
    result = prices.load_rolling_price_change(as_of=AS_OF, repository_root=tmp_path)
    assert result.error == "Exchange calendar unavailable" and result.symbols == ("AAA", "BBB")


def test_default_split_adapter_reuses_existing_readonly_provider_with_bounded_timeout(monkeypatch):
    from app.services import fmp_corporate_data
    calls = []
    class Provider:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def _get_json(self, endpoint, params):
            calls.append((endpoint, params))
            return []
    monkeypatch.setattr(fmp_corporate_data, "FmpCorporateDataProvider", Provider)
    assert prices._fetch_split_payload("AAA") == []
    assert calls == [{"timeout_seconds": 10}, ("splits", {"symbol": "AAA"})]


def test_default_history_adapter_reuses_existing_session_without_constructing_live_session(monkeypatch):
    from datafetching import schwab_fetch
    marker = object()
    monkeypatch.setattr(schwab_fetch, "DataFetchingSchwabSession", lambda: marker)
    assert prices._session_factory() is marker
