from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.models.market_data import MarketQuote
from app.services.market_fetch_specs import SchwabPriceHistorySpec
from app.services.schwab_market_data import SchwabMarketDataProvider
from datafetching.decision_time import DecisionClock
from datafetching.parquet_store import ParquetStore
from datafetching.quote_liquidity import (
    QuoteLiquidityQualityError,
    calculate_quote_liquidity,
    persist_quote_liquidity,
    quote_liquidity_freshness_expected_at,
)
from datafetching import schwab_fetch
from options import OptionSnapshotOutput
from options.features import (
    OPTION_FEATURE_VERSION,
    OPTION_SELECTION_POLICY_VERSION,
    OPTION_SURFACE_QUALITY_POLICY_VERSION,
    calculate_option_snapshot_features,
    load_realized_volatility_evidence,
)
from options.snapshot import (
    OPTION_CHAIN_SCHEMA_VERSION,
    normalize_schwab_option_chain,
    persist_schwab_option_snapshot,
)


UTC = timezone.utc
SNAPSHOT_FOR = pd.Timestamp("2026-07-29T15:30:00Z")
QUOTE_EVENT_AT = pd.Timestamp("2026-07-29T15:30:30Z")
QUOTE_CUTOFF_AT = pd.Timestamp("2026-07-29T15:31:00Z")
AVAILABLE_AT = pd.Timestamp("2026-07-29T15:32:00Z")


class _QuoteSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def get_equity_quote(self, symbol: str) -> dict[str, Any]:
        assert symbol == "GOOG"
        self.calls += 1
        return self.payload


def test_schwab_quote_captures_provider_event_time() -> None:
    session = _QuoteSession(
        {
            "bidPrice": 199.9,
            "askPrice": 200.1,
            "quoteTimeInLong": int(QUOTE_EVENT_AT.timestamp() * 1000),
        }
    )

    quote, _payload = SchwabMarketDataProvider(session=session).fetch_quote("goog")

    assert session.calls == 1
    assert quote.quote_event_at == QUOTE_EVENT_AT.to_pydatetime()
    assert quote.fetched_at.tzinfo is not None


def test_schwab_price_history_duplicate_timestamp_keeps_last_provider_candle() -> None:
    payload = {
        "candles": [
            {
                "datetime": int(
                    pd.Timestamp("2026-07-29T11:01:00Z").timestamp() * 1000
                ),
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.5,
                "volume": 80,
            },
            {
                "datetime": int(
                    pd.Timestamp("2026-07-29T11:00:00Z").timestamp() * 1000
                ),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100,
            },
            {
                "datetime": int(
                    pd.Timestamp("2026-07-29T11:00:00Z").timestamp() * 1000
                ),
                "open": 100.0,
                "high": 101.25,
                "low": 98.75,
                "close": 100.75,
                "volume": 120,
            },
        ]
    }
    original_payload = deepcopy(payload)

    class DuplicateHistorySession:
        def get_price_history(
            self,
            symbol: str,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            assert symbol == "GOOG"
            return payload

    spec = SchwabPriceHistorySpec(
        key="day_1_minute_1",
        period_type="day",
        period=1,
        frequency_type="minute",
        frequency=1,
        need_extended_hours_data=True,
    )

    bars, raw_payload = SchwabMarketDataProvider(
        session=DuplicateHistorySession()
    ).fetch_bars_for_spec("goog", spec)

    assert [bar.timestamp for bar in bars] == [
        pd.Timestamp("2026-07-29T11:00:00Z").to_pydatetime(),
        pd.Timestamp("2026-07-29T11:01:00Z").to_pydatetime(),
    ]
    revised = bars[0]
    assert (revised.open, revised.high, revised.low, revised.close, revised.volume) == (
        100.0,
        101.25,
        98.75,
        100.75,
        120.0,
    )
    assert raw_payload is payload
    assert raw_payload == original_payload
    assert len(raw_payload["candles"]) == 3


def test_quote_liquidity_preserves_each_immutable_receipt(tmp_path: Path) -> None:
    first = _quote(available_at=AVAILABLE_AT)
    second = _quote(available_at=AVAILABLE_AT + pd.Timedelta(minutes=1))

    path = persist_quote_liquidity(tmp_path, first)
    assert persist_quote_liquidity(tmp_path, first) == path
    assert persist_quote_liquidity(tmp_path, second) == path

    stored = pd.read_parquet(path)
    assert stored.columns[0] == "id"
    assert stored.columns.tolist().count("id") == 1
    assert len(stored) == 2
    assert stored["id"].is_unique
    assert stored["id"].str.startswith("GOOG|").all()
    assert stored["relative_bid_ask_spread"].tolist() == pytest.approx(
        [(200.1 - 199.9) / 200.0] * 2
    )
    assert stored["quote_quality_pass"].all()


def test_quote_liquidity_tolerates_bounded_provider_clock_skew() -> None:
    fetched_at = pd.Timestamp("2026-07-29T15:30:30Z")
    quote_event_at = fetched_at + pd.Timedelta(seconds=3)

    result = calculate_quote_liquidity(
        MarketQuote(
            symbol="GOOG",
            source="schwab",
            fetched_at=fetched_at.to_pydatetime(),
            quote_event_at=quote_event_at.to_pydatetime(),
            bid=199.9,
            ask=200.1,
        )
    ).iloc[0]

    assert result["fetched_at"] == fetched_at
    assert result["quote_event_at"] == quote_event_at
    assert result["available_at"] == quote_event_at
    assert result["quote_staleness_seconds"] == 0.0
    assert bool(result["quote_quality_pass"])


def test_quote_liquidity_rejects_excessive_provider_clock_skew_with_details() -> None:
    fetched_at = pd.Timestamp("2026-07-29T15:30:30Z")
    quote_event_at = fetched_at + pd.Timedelta(seconds=6)

    with pytest.raises(
        QuoteLiquidityQualityError,
        match=r"6\.000s.*quote_event_at=.*fetched_at=",
    ):
        calculate_quote_liquidity(
            MarketQuote(
                symbol="GOOG",
                source="schwab",
                fetched_at=fetched_at.to_pydatetime(),
                quote_event_at=quote_event_at.to_pydatetime(),
                bid=199.9,
                ask=200.1,
            )
        )


@pytest.mark.parametrize(
    ("available_at", "expected"),
    [
        ("2026-07-29T15:30:00Z", True),
        ("2026-07-29T11:30:00Z", True),
        ("2026-07-29T23:30:00Z", True),
        ("2026-07-30T00:30:00Z", False),
        ("2026-07-30T06:44:20Z", False),
        ("2026-08-01T15:30:00Z", False),
        ("2026-11-27T17:30:00Z", True),
        ("2026-11-27T18:30:00Z", False),
    ],
)
def test_quote_liquidity_expectation_uses_exact_exchange_sessions(
    available_at: str,
    expected: bool,
) -> None:
    assert (
        quote_liquidity_freshness_expected_at(pd.Timestamp(available_at))
        is expected
    )


@pytest.mark.parametrize(
    "receipt",
    [
        pd.Timestamp("2026-07-29T15:32:00Z"),
        pd.Timestamp("2026-07-29T23:32:00Z"),
    ],
    ids=("regular-session", "extended-session"),
)
def test_quote_liquidity_rejects_stale_quote_during_live_session(
    receipt: pd.Timestamp,
) -> None:
    assert quote_liquidity_freshness_expected_at(receipt)

    with pytest.raises(QuoteLiquidityQualityError, match="stale"):
        calculate_quote_liquidity(
            MarketQuote(
                symbol="GOOG",
                source="schwab",
                fetched_at=receipt.to_pydatetime(),
                quote_event_at=(receipt - pd.Timedelta(minutes=6)).to_pydatetime(),
                bid=199.9,
                ask=200.1,
            )
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"bid": 200.2, "ask": 200.1}, "crossed"),
        ({"bid": 200.0, "ask": 200.0}, "locked"),
        ({"bid": 0.0}, "positive"),
        ({"quote_event_at": AVAILABLE_AT + pd.Timedelta(seconds=6)}, "exceeds"),
        ({"quote_event_at": None}, "missing"),
    ],
)
def test_quote_liquidity_rejects_noncausal_or_invalid_quotes(
    changes: dict[str, Any],
    message: str,
) -> None:
    values = {
        "symbol": "GOOG",
        "source": "schwab",
        "fetched_at": AVAILABLE_AT.to_pydatetime(),
        "quote_event_at": QUOTE_EVENT_AT.to_pydatetime(),
        "bid": 199.9,
        "ask": 200.1,
    }
    values.update(changes)

    with pytest.raises(QuoteLiquidityQualityError, match=message):
        calculate_quote_liquidity(MarketQuote(**values))


def test_option_receipts_use_snapshot_and_availability_natural_key(
    tmp_path: Path,
) -> None:
    payload = _option_payload()
    clock = _decision_clock(tmp_path)

    first = persist_schwab_option_snapshot(
        tmp_path,
        symbol="goog",
        payload=payload,
        clock=clock,
        quote_cutoff_at=QUOTE_CUTOFF_AT,
        fetched_at=AVAILABLE_AT,
    )
    persist_schwab_option_snapshot(
        tmp_path,
        symbol="goog",
        payload=payload,
        clock=clock,
        quote_cutoff_at=QUOTE_CUTOFF_AT,
        fetched_at=AVAILABLE_AT,
    )
    second = persist_schwab_option_snapshot(
        tmp_path,
        symbol="goog",
        payload=payload,
        clock=clock,
        quote_cutoff_at=QUOTE_CUTOFF_AT,
        fetched_at=AVAILABLE_AT + pd.Timedelta(minutes=1),
    )

    assert first == second
    raw = pd.read_parquet(first.raw_path)
    contracts = pd.read_parquet(first.contracts_path)
    features = pd.read_parquet(first.features_path)
    for frame in (raw, contracts, features):
        assert frame.columns[0] == "id"
        assert frame.columns.tolist().count("id") == 1
        assert frame["id"].is_unique
        assert set(("symbol", "snapshot_for", "available_at")).issubset(frame)
        assert "timestamp" not in frame

    assert len(raw) == 2
    assert len(features) == 2
    assert len(contracts) == 12
    assert raw["snapshot_for"].nunique() == 1
    assert raw["available_at"].nunique() == 2
    assert raw["schema_version"].eq(OPTION_CHAIN_SCHEMA_VERSION).all()
    assert features["calculation_version"].eq(OPTION_FEATURE_VERSION).all()
    assert features["selection_policy_version"].eq(
        OPTION_SELECTION_POLICY_VERSION
    ).all()
    assert features["surface_quality_policy_version"].eq(
        OPTION_SURFACE_QUALITY_POLICY_VERSION
    ).all()
    assert features["surface_quality_pass"].all()
    assert features["quote_coverage"].eq(1.0).all()
    assert features["quote_time_coverage"].eq(1.0).all()
    assert features["iv_coverage"].eq(1.0).all()
    assert features["greeks_coverage"].eq(1.0).all()
    assert features["open_interest_coverage"].eq(1.0).all()
    assert features["realized_volatility_source_provider"].eq("databento").all()
    assert features["realized_volatility_source_timeframe"].eq("1d").all()


def test_realized_volatility_provenance_excludes_later_available_rows(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "technicals"
        / "market-regime"
        / "databento"
        / "1d.parquet"
    )
    path.parent.mkdir(parents=True)
    timestamps = pd.date_range("2026-06-20", periods=22, freq="D", tz="UTC")
    eligible = pd.DataFrame(
        {
            "timestamp": timestamps,
            "close": [100.0 + index for index in range(len(timestamps))],
            "available_at": timestamps + pd.Timedelta(days=1),
            "calculation_version": "market-regime-test-v1",
            "price_adjustment_status": "NO_SPLIT_EVENTS_IN_RANGE",
            "split_event_count": 0,
        }
    )
    later_revision = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-07-29T00:00:00Z")],
            "close": [1_000.0],
            "available_at": [pd.Timestamp("2026-07-30T00:00:00Z")],
            "calculation_version": ["market-regime-test-v2"],
            "price_adjustment_status": ["SPLIT_ADJUSTED"],
            "split_event_count": [1],
        }
    )
    pd.concat([eligible, later_revision], ignore_index=True).to_parquet(
        path,
        index=False,
    )

    evidence = load_realized_volatility_evidence(
        tmp_path,
        symbol="GOOG",
        as_of=QUOTE_CUTOFF_AT,
    )

    assert evidence.value is not None
    assert evidence.source_provider == "databento"
    assert evidence.source_timeframe == "1d"
    assert evidence.source_available_at <= QUOTE_CUTOFF_AT
    assert evidence.source_calculation_version == "market-regime-test-v1"
    assert evidence.price_adjustment_status == "NO_SPLIT_EVENTS_IN_RANGE"
    assert evidence.split_event_count == 0


def test_option_duplicate_contract_natural_key_fails_before_writes(
    tmp_path: Path,
) -> None:
    payload = _option_payload()
    payload["callExpDateMap"]["2026-08-28:30"]["100.0"][0]["symbol"] = (
        "GOOG-C95"
    )

    with pytest.raises(ValueError, match="duplicate natural keys"):
        persist_schwab_option_snapshot(
            tmp_path,
            symbol="GOOG",
            payload=payload,
            clock=_decision_clock(tmp_path),
            quote_cutoff_at=QUOTE_CUTOFF_AT,
            fetched_at=AVAILABLE_AT,
        )

    assert not (tmp_path / "stocks" / "GOOG" / "options").exists()


@pytest.mark.parametrize(
    ("mutation", "expected_column"),
    [
        ("future", "quote_after_cutoff_count"),
        ("crossed", "crossed_quote_count"),
        ("locked", "locked_quote_count"),
        ("intrinsic", "intrinsic_value_violation"),
    ],
)
def test_option_surface_quality_gates_fail_closed(
    tmp_path: Path,
    mutation: str,
    expected_column: str,
) -> None:
    payload = _option_payload(mutation=mutation)
    contracts = normalize_schwab_option_chain(
        payload,
        symbol="GOOG",
        clock=_decision_clock(tmp_path),
        quote_cutoff_at=QUOTE_CUTOFF_AT,
        fetched_at=AVAILABLE_AT,
    )

    features = calculate_option_snapshot_features(contracts).iloc[0]

    assert not bool(features["surface_quality_pass"])
    assert bool(features[expected_column])


@pytest.mark.parametrize(
    "receipt",
    [
        datetime(2026, 7, 29, 15, 32, tzinfo=UTC),
        datetime(2026, 7, 30, 6, 44, 20, tzinfo=UTC),
    ],
    ids=("regular-session", "fresh-overnight"),
)
def test_schwab_fetch_reuses_a_fresh_fetched_quote_for_liquidity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt: datetime,
) -> None:
    provider_calls = {"quotes": 0}
    option_call: dict[str, Any] = {}

    class FakeSession:
        def get_option_chain_snapshot(
            self,
            symbol: str,
            *,
            as_of: datetime,
        ) -> dict[str, Any]:
            assert symbol == "GOOG"
            return {"already_fetched": True}

    class FakeProvider:
        def __init__(self, *, session: FakeSession) -> None:
            self.session = session

        def fetch_quote(self, symbol: str) -> tuple[MarketQuote, dict[str, Any]]:
            provider_calls["quotes"] += 1
            quote = MarketQuote(
                symbol=symbol,
                source="schwab",
                fetched_at=receipt,
                quote_event_at=receipt - timedelta(seconds=1),
                bid=99.9,
                ask=100.1,
            )
            return quote, {"quoteTimeInLong": int(receipt.timestamp() * 1000)}

    def fake_persist(
        datastore_root: Path,
        *,
        symbol: str,
        payload: dict[str, Any],
        clock: DecisionClock,
        fetched_at: datetime,
        quote_cutoff_at: datetime,
    ) -> OptionSnapshotOutput:
        option_call.update(
            {
                "symbol": symbol,
                "payload": payload,
                "clock": clock,
                "fetched_at": fetched_at,
                "quote_cutoff_at": quote_cutoff_at,
            }
        )
        return OptionSnapshotOutput(
            tmp_path / "contracts.parquet",
            tmp_path / "features.parquet",
            tmp_path / "raw.parquet",
            0,
        )

    monkeypatch.setattr(schwab_fetch, "DataFetchingSchwabSession", FakeSession)
    monkeypatch.setattr(schwab_fetch, "SchwabMarketDataProvider", FakeProvider)
    monkeypatch.setattr(schwab_fetch, "_specs_for_profile", lambda _profile: ())
    monkeypatch.setattr(
        schwab_fetch,
        "latest_completed_bar_clock",
        lambda *_args, **_kwargs: _decision_clock(tmp_path),
    )
    monkeypatch.setattr(
        schwab_fetch,
        "persist_schwab_option_snapshot",
        fake_persist,
    )

    result = schwab_fetch.fetch("GOOG", ParquetStore(tmp_path))

    assert result.error_files == 0
    assert provider_calls["quotes"] == 1
    assert option_call["payload"] == {"already_fetched": True}
    assert option_call["quote_cutoff_at"] <= option_call["fetched_at"]
    liquidity_paths = list(
        (tmp_path / "stocks" / "GOOG" / "quotes" / "features").rglob(
            "*.parquet"
        )
    )
    assert len(liquidity_paths) == 1
    assert len(pd.read_parquet(liquidity_paths[0])) == 1


def test_schwab_option_local_contract_skip_is_advisory_and_raw_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = datetime.now(UTC)

    class FakeSession:
        def get_option_chain_snapshot(
            self,
            symbol: str,
            *,
            as_of: datetime,
        ) -> dict[str, Any]:
            assert symbol == "GOOG"
            assert as_of <= datetime.now(UTC)
            return {"provider_payload": "preserved"}

    class FakeProvider:
        def __init__(self, *, session: FakeSession) -> None:
            self.session = session

        def fetch_quote(self, symbol: str) -> tuple[MarketQuote, dict[str, Any]]:
            return (
                MarketQuote(
                    symbol=symbol,
                    source="schwab",
                    fetched_at=receipt,
                    quote_event_at=receipt - timedelta(seconds=1),
                    bid=99.9,
                    ask=100.1,
                ),
                {"quoteTimeInLong": int(receipt.timestamp() * 1000)},
            )

    def local_contract_skip(*_: object, **__: object) -> None:
        raise ValueError("optional option feature contract is ambiguous")

    monkeypatch.setattr(schwab_fetch, "DataFetchingSchwabSession", FakeSession)
    monkeypatch.setattr(schwab_fetch, "SchwabMarketDataProvider", FakeProvider)
    monkeypatch.setattr(schwab_fetch, "_specs_for_profile", lambda _profile: ())
    monkeypatch.setattr(
        schwab_fetch,
        "latest_completed_bar_clock",
        lambda *_args, **_kwargs: _decision_clock(tmp_path),
    )
    monkeypatch.setattr(
        schwab_fetch,
        "persist_schwab_option_snapshot",
        local_contract_skip,
    )

    result = schwab_fetch.fetch("GOOG", ParquetStore(tmp_path))

    assert result.error_files == 0
    assert result.advisory_files == 1
    raw_options = list(
        (tmp_path / "stocks" / "GOOG" / "options").rglob("raw/*.parquet")
    )
    assert len(raw_options) == 1
    assert "provider_payload" in pd.read_parquet(raw_options[0]).loc[0, "payload_json"]


def test_schwab_fetch_persists_closed_session_quote_as_unavailable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = datetime(2026, 7, 30, 6, 44, 20, tzinfo=UTC)

    class FakeSession:
        def get_option_chain_snapshot(
            self,
            symbol: str,
            *,
            as_of: datetime,
        ) -> dict[str, Any]:
            assert symbol == "GOOG"
            return {"already_fetched": True}

    class FakeProvider:
        def __init__(self, *, session: FakeSession) -> None:
            self.session = session

        def fetch_quote(self, symbol: str) -> tuple[MarketQuote, dict[str, Any]]:
            quote_event_at = receipt - timedelta(seconds=24_516.035)
            return (
                MarketQuote(
                    symbol=symbol,
                    source="schwab",
                    fetched_at=receipt,
                    quote_event_at=quote_event_at,
                    bid=99.9,
                    ask=100.1,
                ),
                {"quoteTimeInLong": int(quote_event_at.timestamp() * 1000)},
            )

    def fake_persist(
        datastore_root: Path,
        *,
        symbol: str,
        payload: dict[str, Any],
        clock: DecisionClock,
        fetched_at: datetime,
        quote_cutoff_at: datetime,
    ) -> OptionSnapshotOutput:
        return OptionSnapshotOutput(
            tmp_path / "contracts.parquet",
            tmp_path / "features.parquet",
            tmp_path / "raw.parquet",
            0,
        )

    monkeypatch.setattr(schwab_fetch, "DataFetchingSchwabSession", FakeSession)
    monkeypatch.setattr(schwab_fetch, "SchwabMarketDataProvider", FakeProvider)
    monkeypatch.setattr(schwab_fetch, "_specs_for_profile", lambda _profile: ())
    monkeypatch.setattr(
        schwab_fetch,
        "latest_completed_bar_clock",
        lambda *_args, **_kwargs: _decision_clock(tmp_path),
    )
    monkeypatch.setattr(
        schwab_fetch,
        "persist_schwab_option_snapshot",
        fake_persist,
    )

    result = schwab_fetch.fetch("GOOG", ParquetStore(tmp_path))

    assert result.error_files == 0
    liquidity_path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "quotes"
        / "features"
        / "quote-liquidity"
        / "schwab"
        / "2026-07.parquet"
    )
    stored = pd.read_parquet(liquidity_path)
    assert len(stored) == 1
    assert stored.loc[0, "quote_event_at"] == pd.Timestamp(
        receipt - timedelta(seconds=24_516.035)
    )
    assert stored.loc[0, "available_at"] == pd.Timestamp(receipt)
    assert not bool(stored.loc[0, "quote_quality_pass"])


def _quote(*, available_at: pd.Timestamp) -> MarketQuote:
    return MarketQuote(
        symbol="GOOG",
        source="schwab",
        fetched_at=available_at.to_pydatetime(),
        quote_event_at=QUOTE_EVENT_AT.to_pydatetime(),
        bid=199.9,
        ask=200.1,
    )


def _decision_clock(root: Path) -> DecisionClock:
    return DecisionClock(
        decision_timestamp=SNAPSHOT_FOR,
        bar_timestamp=SNAPSHOT_FOR - pd.Timedelta(minutes=1),
        provider="databento",
        timeframe="1m",
        source_file=root / "databento-1m.parquet",
    )


def _option_payload(*, mutation: str = "") -> dict[str, Any]:
    quote_ms = int(QUOTE_EVENT_AT.timestamp() * 1000)
    if mutation == "future":
        quote_ms = int(
            (QUOTE_CUTOFF_AT + pd.Timedelta(seconds=1)).timestamp() * 1000
        )

    calls = [
        _contract("GOOG-C95", "CALL", 95.0, 5.5, 5.7, 0.75, quote_ms),
        _contract("GOOG-C100", "CALL", 100.0, 3.0, 3.2, 0.50, quote_ms),
        _contract("GOOG-C105", "CALL", 105.0, 1.0, 1.2, 0.25, quote_ms),
    ]
    puts = [
        _contract("GOOG-P95", "PUT", 95.0, 1.0, 1.2, -0.25, quote_ms),
        _contract("GOOG-P100", "PUT", 100.0, 3.0, 3.2, -0.50, quote_ms),
        _contract("GOOG-P105", "PUT", 105.0, 5.5, 5.7, -0.75, quote_ms),
    ]
    target = calls[1]
    if mutation == "crossed":
        target["bid"], target["ask"] = 3.3, 3.2
        target["mark"] = 3.25
    elif mutation == "locked":
        target["bid"], target["ask"] = 3.2, 3.2
        target["mark"] = 3.2
    elif mutation == "intrinsic":
        calls[0]["mark"] = 4.0

    return {
        "symbol": "GOOG",
        "underlyingPrice": 100.0,
        "interestRate": 4.0,
        "dividendYield": 0.5,
        "underlying": {
            "mark": 100.0,
            "quoteTime": int(QUOTE_EVENT_AT.timestamp() * 1000),
        },
        "callExpDateMap": {
            "2026-08-28:30": {
                "95.0": [calls[0]],
                "100.0": [calls[1]],
                "105.0": [calls[2]],
            }
        },
        "putExpDateMap": {
            "2026-08-28:30": {
                "95.0": [puts[0]],
                "100.0": [puts[1]],
                "105.0": [puts[2]],
            }
        },
    }


def _contract(
    symbol: str,
    call_put: str,
    strike: float,
    bid: float,
    ask: float,
    delta: float,
    quote_ms: int,
) -> dict[str, Any]:
    intrinsic = (
        max(100.0 - strike, 0.0)
        if call_put == "CALL"
        else max(strike - 100.0, 0.0)
    )
    return {
        "symbol": symbol,
        "strikePrice": strike,
        "expirationDate": "2026-08-28T20:00:00Z",
        "daysToExpiration": 30,
        "underlyingPrice": 100.0,
        "bid": bid,
        "ask": ask,
        "mark": (bid + ask) / 2.0,
        "last": (bid + ask) / 2.0,
        "totalVolume": 100,
        "openInterest": 1_000,
        "volatility": 25.0,
        "delta": delta,
        "gamma": 0.03,
        "theta": -0.04,
        "vega": 0.12,
        "rho": 0.02,
        "intrinsicValue": intrinsic,
        "quoteTimeInLong": quote_ms,
        "tradeTimeInLong": quote_ms,
        "multiplier": 100,
    }
