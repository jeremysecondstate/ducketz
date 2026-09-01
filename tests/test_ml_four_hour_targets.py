from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import semantic_metadata_fingerprint
from ml.calendars import (
    FOUR_HOUR_CHECKPOINT_START_POLICY,
    HYBRID_TARGET_START_POLICY,
    US_EQUITY_ACTIONABLE_TARGET_POLICY,
    US_EQUITY_EXTENDED_SOURCE_POLICY,
    ExchangeSessionCalendar,
)
from ml.contracts import MLContractError
from ml.horizons import (
    DEFAULT_HORIZON_SPECIFICATIONS,
    FEATURE_PROFILES,
    HORIZON_ORDER,
    horizon_specification,
)
from ml.rolling_materialization import materialize_rolling_samples
from ml.rolling_samples import build_rolling_samples


def test_horizon_order_and_nonweekly_contract_fingerprint_are_stable() -> None:
    assert tuple(DEFAULT_HORIZON_SPECIFICATIONS) == HORIZON_ORDER
    assert HORIZON_ORDER == ("1h", "4h", "1d", "1w")
    assert {
        horizon: semantic_metadata_fingerprint(
            DEFAULT_HORIZON_SPECIFICATIONS[horizon].as_dict()
        )
        for horizon in ("1d",)
    } == {
        "1d": "0ee76710b742789a91817307dda134e70454cf53d1bb296ab76da0e91ae9258f",
    }


def test_every_closed_feature_profile_uses_canonical_horizon_order() -> None:
    assert all(
        tuple(profile) == HORIZON_ORDER
        for profile in FEATURE_PROFILES.values()
    )


@pytest.mark.parametrize(
    ("horizon", "version", "minute_count"),
    (
        (
            "1h",
            "next-60-eligible-equity-minutes-open-close-v4",
            "60",
        ),
        (
            "4h",
            "next-180-eligible-equity-minutes-four-checkpoints-v4",
            "180",
        ),
    ),
)
def test_intraday_contracts_are_explicit_and_horizon_scoped(
    horizon: str,
    version: str,
    minute_count: str,
) -> None:
    specification = horizon_specification(horizon)
    assert specification.target_definition_version == version
    assert specification.source_timeframe == "1h"
    assert specification.target_price_provider == "databento"
    assert specification.target_price_timeframe == "1m"
    assert specification.target_price_source_version == (
        "canonical-adjusted-native-1m-causal-no-trade-marks-v2"
    )
    assert minute_count in specification.target_window_end_rule
    assert specification.processing_delay == pd.Timedelta(minutes=5)
    assert specification.target_calendar_policy_version == (
        "us-equity-actionable-segments-plus-versioned-start-v1"
    )
    assert specification.intraday_source_session_policy == (
        US_EQUITY_EXTENDED_SOURCE_POLICY
    )
    assert specification.intraday_target_session_policy == (
        US_EQUITY_ACTIONABLE_TARGET_POLICY
    )
    assert specification.intraday_target_start_policy == (
        HYBRID_TARGET_START_POLICY
        if horizon == "1h"
        else FOUR_HOUR_CHECKPOINT_START_POLICY
    )
    with pytest.raises(ValueError, match="expected 1h, 4h, 1d, 1w"):
        horizon_specification("unknown")


def test_us_equity_candidates_cover_pre_regular_and_post_checkpoints() -> None:
    session = "2026-07-27"
    calendar = ExchangeSessionCalendar(
        "XNAS",
        start=pd.Timestamp(session) - pd.Timedelta(days=7),
        end=pd.Timestamp(session) + pd.Timedelta(days=7),
    )
    candidates = calendar.target_start_candidates(
        start_session=session,
        end_session=session,
        session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
        start_policy=HYBRID_TARGET_START_POLICY,
    )
    assert candidates == tuple(
        pd.to_datetime(
            [
                "2026-07-27T11:00:00Z",
                "2026-07-27T12:00:00Z",
                "2026-07-27T13:30:00Z",
                "2026-07-27T14:00:00Z",
                "2026-07-27T15:00:00Z",
                "2026-07-27T16:00:00Z",
                "2026-07-27T17:00:00Z",
                "2026-07-27T18:00:00Z",
                "2026-07-27T19:00:00Z",
                "2026-07-27T20:05:00Z",
                "2026-07-27T21:00:00Z",
                "2026-07-27T22:00:00Z",
                "2026-07-27T23:00:00Z",
            ],
            utc=True,
        )
    )
    four_hour = calendar.target_start_candidates(
        start_session=session,
        end_session=session,
        session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
        start_policy=FOUR_HOUR_CHECKPOINT_START_POLICY,
    )
    assert four_hour == tuple(
        pd.to_datetime(
            [
                "2026-07-27T11:30:00Z",
                "2026-07-27T15:30:00Z",
                "2026-07-27T19:30:00Z",
                "2026-07-27T23:30:00Z",
            ],
            utc=True,
        )
    )


@pytest.mark.parametrize(
    ("horizon", "expected_start", "expected_end"),
    (
        ("1h", "2026-07-24T21:00:00Z", "2026-07-24T22:00:00Z"),
        ("4h", "2026-07-24T23:30:00Z", "2026-07-27T13:35:00Z"),
    ),
)
def test_late_regular_decision_targets_same_day_postmarket(
    horizon: str,
    expected_start: str,
    expected_end: str,
) -> None:
    feature = _feature(
        horizon=horizon,
        session="2026-07-24",
        bar_start="2026-07-24T19:00:00Z",
        decision="2026-07-24T20:05:00Z",
    )
    sample = _build(feature)
    assert sample["target_window_start"] == pd.Timestamp(expected_start)
    assert sample["target_window_end"] == pd.Timestamp(expected_end)
    assert sample["actionable_until"] == sample["target_window_start"]


def test_one_hour_aftermarket_decision_uses_latest_completed_extended_bar() -> None:
    feature = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T20:00:00Z",
        decision="2026-07-27T21:05:00Z",
    )

    sample = _build(feature, source_open=100.0, source_close=102.0)

    assert sample["decision_timestamp"] == pd.Timestamp("2026-07-27T21:05:00Z")
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T22:00:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T23:00:00Z"
    )
    assert sample["previous_period_direction"] == 1.0


def test_four_hour_ordinary_target_uses_the_0830_pacific_checkpoint() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    feature["model_probe"] = 7.25
    sample = _build(
        feature,
        target_open=102.0,
        target_close=108.0,
        source_open=100.0,
        source_close=101.0,
        cost=0.001,
    )
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T15:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T18:30:00Z"
    )
    assert sample["label_available_at"] == pd.Timestamp(
        "2026-07-27T18:35:00Z"
    )
    assert sample["target_open"] == 102.0
    assert sample["target_close"] == 108.0
    assert sample["previous_period_direction"] == 1.0
    assert sample["model_probe"] == 7.25
    assert sample["id"] == "GOOG|4h|2026-07-27T15:05:00Z"


def test_intraday_target_rejects_aggregated_thirty_minute_prices() -> None:
    feature = _feature(
        horizon="1h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    specification = horizon_specification("1h")
    _window, target_prices = _window_and_target_prices(
        feature,
        minute_count=60,
        open_value=100.0,
        close_value=101.0,
        missing_indices=(),
    )
    target_prices["timeframe"] = "30m"

    with pytest.raises(MLContractError, match="require native 1m prices"):
        build_rolling_samples(
            feature,
            target_prices,
            specification=specification,
            assumed_round_trip_cost=0.001,
            materialized_at="2026-07-27T17:05:00Z",
            source_adjusted_prices=_source_hour(
                feature,
                open_value=100.0,
                close_value=101.0,
            ),
        )


def test_exact_target_start_equality_is_too_late() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T16:00:00Z",
    )
    sample = _build(feature)
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T19:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T22:35:00Z"
    )


def test_midday_four_hour_target_pauses_across_core_to_post_gap() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T17:00:00Z",
        decision="2026-07-27T18:05:00Z",
    )
    sample = _build(feature, source_open=100.0, source_close=99.0)
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T19:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T22:35:00Z"
    )
    assert sample["previous_period_direction"] == 0.0


def test_four_hour_target_crosses_weekend_and_xnas_holiday() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-02",
        bar_start="2026-07-02T19:00:00Z",
        decision="2026-07-02T20:05:00Z",
    )
    sample = _build(feature)
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-02T23:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-06T13:35:00Z"
    )


def test_four_hour_target_respects_early_close_before_crossing_weekend() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-11-27",
        bar_start="2026-11-27T15:00:00Z",
        decision="2026-11-27T16:05:00Z",
    )
    sample = _build(feature)
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-11-27T16:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-11-30T13:30:00Z"
    )


def test_four_hour_target_uses_exchange_dst_schedule_for_postmarket() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-03-06",
        bar_start="2026-03-06T19:00:00Z",
        decision="2026-03-06T20:05:00Z",
    )
    sample = _build(feature)
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-03-06T20:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-03-06T23:35:00Z"
    )


def test_four_hour_target_pauses_across_exchange_break() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-24",
        bar_start="2026-07-24T07:00:00Z",
        decision="2026-07-24T08:05:00Z",
        exchange_calendar="XHKG",
    )
    sample = _build(feature)
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T01:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T05:30:00Z"
    )


@pytest.mark.parametrize("missing_target_index", (0, 179))
def test_missing_boundary_mark_keeps_four_hour_label_incomplete(
    missing_target_index: int,
) -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    sample = _build(feature, missing_indices=(missing_target_index,))
    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T15:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T18:30:00Z"
    )
    assert sample["label_status"] == "INCOMPLETE_LABEL"
    assert sample["label_exclusion_reason"] == (
        "complete_target_prices_unavailable"
    )
    assert pd.isna(sample["target_open"])
    assert pd.isna(sample["target_close"])
    assert pd.isna(sample["forward_raw_return"])
    assert pd.isna(sample["target_cost_adjusted_positive"])


def test_missing_middle_no_trade_minute_uses_only_a_prior_close_mark() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    sample = _build(feature, missing_indices=(90,))

    assert sample["target_window_start"] == pd.Timestamp(
        "2026-07-27T15:30:00Z"
    )
    assert sample["target_window_end"] == pd.Timestamp(
        "2026-07-27T18:30:00Z"
    )
    assert sample["label_status"] == "COMPLETE"
    assert sample["target_open"] == 100.0
    assert sample["target_close"] == 106.0


def test_four_hour_label_matures_only_at_end_plus_five_minutes() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    immature = _build(
        feature,
        materialized_at="2026-07-27T18:34:59.999999Z",
    )
    mature = _build(
        feature,
        materialized_at="2026-07-27T18:35:00Z",
    )
    assert immature["label_status"] == "INCOMPLETE_LABEL"
    assert immature["label_exclusion_reason"] == "target_window_not_mature"
    assert pd.isna(immature["target_open"])
    assert mature["label_status"] == "COMPLETE"


def test_four_hour_cost_is_subtracted_once_and_positive_is_strict() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    positive = _build(
        feature,
        target_open=100.0,
        target_close=102.0,
        cost=0.005,
    )
    assert positive["forward_raw_return"] == pytest.approx(0.02)
    assert positive["forward_cost_adjusted_return"] == pytest.approx(0.015)
    assert positive["target_cost_adjusted_positive"] == 1

    break_even_cost = 101.0 / 100.0 - 1.0
    break_even = _build(
        feature,
        target_open=100.0,
        target_close=101.0,
        cost=break_even_cost,
    )
    assert break_even["forward_cost_adjusted_return"] == 0.0
    assert break_even["target_cost_adjusted_positive"] == 0


def test_target_prices_do_not_replace_features_or_source_direction() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    feature["mr__trend_atr"] = 7.25
    original = _build(
        feature,
        target_close=106.0,
        source_open=100.0,
        source_close=99.0,
    )
    changed = _build(
        feature,
        target_close=1.0,
        source_open=100.0,
        source_close=99.0,
    )
    assert original["mr__trend_atr"] == changed["mr__trend_atr"] == 7.25
    assert original["previous_period_direction"] == 0.0
    assert changed["previous_period_direction"] == 0.0
    assert original["target_cost_adjusted_positive"] == 1
    assert changed["target_cost_adjusted_positive"] == 0


def test_sample_persists_canonical_target_compatibility() -> None:
    feature = _feature(
        horizon="4h",
        session="2026-07-27",
        bar_start="2026-07-27T14:00:00Z",
        decision="2026-07-27T15:05:00Z",
    )
    sample = _build(feature)
    specification = horizon_specification("4h")
    assert sample["target_definition_version"] == (
        specification.target_definition_version
    )
    assert json.loads(sample["target_specification"]) == specification.as_dict()
    assert sample["target_specification"] == json.dumps(
        specification.as_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )


def test_intraday_target_adjustment_basis_must_match_source_bars() -> None:
    import ml.rolling_materialization as module

    source_bars = SimpleNamespace(
        adjustment_status="SPLIT_ADJUSTED",
        split_event_count=1,
        split_events_json='[{"effective_date":"2024-06-10"}]',
    )
    target_bars = SimpleNamespace(
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
    )

    with pytest.raises(ValueError, match="adjustment basis does not match"):
        module._validate_target_price_adjustment_basis(
            source_bars,
            target_bars=target_bars,
        )


def test_intraday_target_range_after_every_source_split_is_compatible() -> None:
    import ml.rolling_materialization as module

    source_bars = SimpleNamespace(
        adjustment_status="SPLIT_ADJUSTED",
        split_event_count=1,
        split_events_json='[{"ex_date":"2024-06-10"}]',
    )
    target_bars = SimpleNamespace(
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
        frame=pd.DataFrame(
            {"timestamp": pd.to_datetime(["2026-05-11T08:03:00Z"], utc=True)}
        ),
    )

    module._validate_target_price_adjustment_basis(
        source_bars,
        target_bars=target_bars,
    )


def test_intraday_target_range_overlapping_source_split_is_rejected() -> None:
    import ml.rolling_materialization as module

    source_bars = SimpleNamespace(
        adjustment_status="SPLIT_ADJUSTED",
        split_event_count=1,
        split_events_json='[{"ex_date":"2024-06-10"}]',
    )
    target_bars = SimpleNamespace(
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
        frame=pd.DataFrame(
            {"timestamp": pd.to_datetime(["2024-06-07T13:30:00Z"], utc=True)}
        ),
    )

    with pytest.raises(ValueError, match="adjustment basis does not match"):
        module._validate_target_price_adjustment_basis(
            source_bars,
            target_bars=target_bars,
        )


def test_materialization_reuses_source_and_target_caches_for_1h_and_4h(
    tmp_path,
    monkeypatch,
) -> None:
    import ml.rolling_materialization as module

    source_path = tmp_path / "native-1h.parquet"
    target_path = tmp_path / "native-1m.parquet"
    source_path.write_bytes(b"source")
    target_path.write_bytes(b"target")
    source_bars = SimpleNamespace(
        provider="databento",
        timeframe="1h",
        frame=pd.DataFrame(),
        source_files=(source_path,),
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
    )
    target_bars = SimpleNamespace(
        provider="databento",
        timeframe="1m",
        frame=pd.DataFrame(),
        source_files=(target_path,),
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
    )
    source_calls: list[tuple[str, str]] = []
    bar_load_calls: list[tuple[str, tuple[str, ...]]] = []
    price_frame_calls: list[str] = []
    build_calls: list[tuple[str, str, str]] = []
    decision_policies: list[tuple[str, bool]] = []

    def fake_load(root, *, symbol, provider, timeframe, bars):
        source_calls.append((symbol, timeframe))
        assert bars is source_bars
        technical = pd.DataFrame(
            {"timestamp": ["2026-07-27T14:00:00Z"]}
        )
        return technical, technical, source_bars, (source_path,)

    def fake_bar_datasets(root, *, symbol, provider, timeframes):
        bar_load_calls.append((symbol, tuple(sorted(timeframes))))
        return {"1h": source_bars, "1m": target_bars}

    monkeypatch.setattr(module, "_load_operational_sources", fake_load)
    monkeypatch.setattr(module, "_bar_datasets", fake_bar_datasets)
    monkeypatch.setattr(
        module,
        "initial_universe_membership",
        lambda *args, **kwargs: pd.DataFrame(),
    )
    def fake_assemble(*args, config, **kwargs):
        decision_policies.append(
            (config.feature_set, config.include_extended_hours)
        )
        return pd.DataFrame({"symbol": ["GOOG"]})

    monkeypatch.setattr(module, "assemble_technical_feature_frame", fake_assemble)
    monkeypatch.setattr(
        module,
        "_attach_loop_a_features",
        lambda root, decisions, **kwargs: (decisions, ()),
    )
    def fake_price_frame(bars):
        price_frame_calls.append(str(bars.timeframe))
        return pd.DataFrame(
            {"kind": ["target" if bars is target_bars else "source"]}
        )

    monkeypatch.setattr(module, "_price_frame", fake_price_frame)

    def fake_build(
        features,
        prices,
        *,
        specification,
        source_adjusted_prices,
        **kwargs,
    ):
        build_calls.append(
            (
                specification.horizon,
                str(prices.iloc[0]["kind"]),
                str(source_adjusted_prices.iloc[0]["kind"]),
            )
        )
        timestamp = pd.Timestamp("2026-07-27T15:05:00Z")
        return pd.DataFrame(
            {
                "horizon": [specification.horizon],
                "information_available_at": [timestamp],
                "symbol": ["GOOG"],
                "id": [
                    f"GOOG|{specification.horizon}|2026-07-27T15:05:00Z"
                ],
            }
        )

    monkeypatch.setattr(module, "build_rolling_samples", fake_build)
    materialized = materialize_rolling_samples(
        tmp_path,
        symbols=("GOOG",),
        specifications={
            horizon: DEFAULT_HORIZON_SPECIFICATIONS[horizon]
            for horizon in ("1h", "4h")
        },
        reporter=None,
    )
    assert source_calls == [("GOOG", "1h")]
    assert bar_load_calls == [("GOOG", ("1h", "1m"))]
    assert price_frame_calls == ["1h", "1m"]
    assert build_calls == [
        ("1h", "target", "source"),
        ("4h", "target", "source"),
    ]
    assert decision_policies == [
        ("technical-all", True),
        ("technical-all-4h", True),
    ]
    assert materialized.samples["horizon"].tolist() == ["1h", "4h"]


def _feature(
    *,
    horizon: str,
    session: str,
    bar_start: str,
    decision: str,
    exchange_calendar: str = "XNAS",
) -> pd.DataFrame:
    start = pd.Timestamp(bar_start)
    decision_timestamp = pd.Timestamp(decision)
    return pd.DataFrame(
        {
            "id": [f"GOOG|{decision_timestamp.isoformat()}"],
            "symbol": ["GOOG"],
            "venue": ["NASDAQ"],
            "currency": ["USD"],
            "provider": ["databento"],
            "exchange_calendar": [exchange_calendar],
            "exchange_session": [pd.Timestamp(session)],
            "horizon": [horizon],
            "bar_timestamp": [start],
            "bar_end_timestamp": [start + pd.Timedelta(hours=1)],
            "decision_timestamp": [decision_timestamp],
            "feature_available_at": [decision_timestamp],
            "feature_set": [horizon_specification(horizon).feature_set],
        }
    )


def _source_hour(
    feature: pd.DataFrame,
    *,
    open_value: float,
    close_value: float,
) -> pd.DataFrame:
    start = pd.Timestamp(feature.iloc[0]["bar_timestamp"])
    return pd.DataFrame(
        [
            {
                "symbol": "GOOG",
                "provider": "databento",
                "timeframe": "1h",
                "timestamp": start,
                "bar_end_timestamp": start + pd.Timedelta(hours=1),
                "open": open_value,
                "close": close_value,
            }
        ]
    )


def _window_and_target_prices(
    feature: pd.DataFrame,
    *,
    minute_count: int,
    open_value: float,
    close_value: float,
    missing_indices: tuple[int, ...],
) -> tuple[object, pd.DataFrame]:
    decision = pd.Timestamp(feature.iloc[0]["decision_timestamp"])
    exchange_calendar = str(feature.iloc[0]["exchange_calendar"])
    specification = horizon_specification(str(feature.iloc[0]["horizon"]))
    calendar = ExchangeSessionCalendar(
        exchange_calendar,
        start=decision.tz_convert("UTC").tz_localize(None).normalize()
        - pd.Timedelta(days=14),
        end=decision.tz_convert("UTC").tz_localize(None).normalize()
        + pd.Timedelta(days=120),
    )
    window = calendar.target_window_after(
        decision,
        eligible_minute_count=minute_count,
        session_policy=(
            specification.intraday_target_session_policy
        ),
        start_policy=specification.intraday_target_start_policy,
    )
    missing = set(missing_indices)
    rows = [
        {
            "symbol": "GOOG",
            "provider": "databento",
            "timeframe": "1m",
            "timestamp": timestamp,
            "bar_end_timestamp": timestamp + pd.Timedelta(minutes=1),
            "open": open_value,
            "close": (
                close_value
                if index == minute_count - 1
                else open_value
            ),
        }
        for index, timestamp in enumerate(window.constituent_timestamps)
        if index not in missing
    ]
    return window, pd.DataFrame(rows)


def _build(
    feature: pd.DataFrame,
    *,
    target_open: float = 100.0,
    target_close: float = 106.0,
    source_open: float = 100.0,
    source_close: float = 101.0,
    missing_indices: tuple[int, ...] = (),
    materialized_at: str | None = None,
    cost: float = 0.0,
) -> pd.Series:
    specification = horizon_specification(str(feature.iloc[0]["horizon"]))
    minute_count = 60 if specification.horizon == "1h" else 180
    window, target_prices = _window_and_target_prices(
        feature,
        minute_count=minute_count,
        open_value=target_open,
        close_value=target_close,
        missing_indices=missing_indices,
    )
    samples = build_rolling_samples(
        feature,
        target_prices,
        specification=specification,
        assumed_round_trip_cost=cost,
        materialized_at=(
            materialized_at
            if materialized_at is not None
            else window.end_timestamp + specification.processing_delay
        ),
        source_adjusted_prices=_source_hour(
            feature,
            open_value=source_open,
            close_value=source_close,
        ),
    )
    assert len(samples) == 1
    return samples.iloc[0]
