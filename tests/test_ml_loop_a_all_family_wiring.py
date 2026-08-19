from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from datafetching.ids import is_opaque_identifier
from ml.contracts import FeatureSet, FeatureSpec
from ml.datasets.families import (
    WEEKLY_CONTEXT_VALUES,
    load_weekly_context_features,
)
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import horizon_specifications_for_profile
from ml.model_features import model_matrix_for_feature_set
import ml.rolling_materialization as rolling_materialization
from ml.runtime_pipeline import _prediction_frame
from technicals.calculations.weekly_context import calculate_weekly_context
from technicals.parquet_io import BarDataset


_DISPATCH_BY_SOURCE_FAMILY = {
    "bar": {"technical bar-shape"},
    "weekly": {"technical weekly-context"},
    "life": {"technical lifecycle"},
    "fdir": {"FMP fundamental-direction"},
    "fund": {"FMP point-in-time fundamentals"},
    "ftlife": {"fundamental-technical lifecycle"},
    "quote": {"Schwab quote-liquidity"},
    "opt": {"Schwab option-quality"},
    "energy": {"FMP energy-context"},
    "macro": set(),
    "sec": {"SEC event"},
    "cme": {
        "Databento CME cme_context_ohlcv-1m",
        "Databento CME cme_context_bbo-1m",
        "Databento CME cme_context_mbp-10",
    },
}


@pytest.mark.parametrize("horizon", ("1h", "4h", "1d", "1w"))
def test_loop_a_all_profile_dispatches_and_projects_every_ordered_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    horizon: str,
) -> None:
    specification = horizon_specifications_for_profile(
        "loop-a-all-v1",
        horizons=(horizon,),
    )[horizon]
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        specification.feature_set,
        require_active=True,
        horizon=horizon,
    )
    decision = _technical_decision(feature_set, horizon=horizon)
    dispatched: list[str] = []
    attached: set[str] = set()
    derived: list[str] = []

    def source_paths(
        root: Path,
        symbols: tuple[str, ...],
        parts: tuple[str, ...],
    ) -> tuple[Path, ...]:
        return tuple(
            root.joinpath("source-fixtures", symbol, *parts, "part.parquet")
            for symbol in symbols
        )

    def read_sources(
        paths: tuple[Path, ...],
        *,
        family: str,
        cache: dict[tuple[Path, ...], pd.DataFrame],
    ) -> pd.DataFrame:
        assert paths
        dispatched.append(family)
        return pd.DataFrame({"dispatch_family": [family]})

    def append_values(
        decisions: pd.DataFrame,
        source: pd.DataFrame,
        *,
        value_columns: dict[str, str],
        **_: object,
    ) -> pd.DataFrame:
        assert not source.empty
        output = decisions.copy()
        for offset, model_name in enumerate(value_columns, start=1):
            output[model_name] = float(offset)
            attached.add(model_name)
        return output

    def derive_cme(*_: pd.DataFrame) -> pd.DataFrame:
        derived.append("cme")
        return pd.DataFrame({"derived": ["cme"]})

    monkeypatch.setattr(
        rolling_materialization,
        "_partitioned_stock_paths",
        source_paths,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_stock_glob_paths",
        source_paths,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_read_required_sources",
        read_sources,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_bar_shape_features",
        append_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_weekly_context_features",
        append_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_sec_event_features",
        append_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_join_symbol_values",
        append_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_join_shared_values",
        append_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "read_verified_macro_evidence",
        lambda _root: rolling_materialization.VerifiedMacroEvidence(
            release_context=pd.DataFrame({"derived": ["macro"]}),
            vintages=pd.DataFrame({"fixture": [1.0]}),
            source_files=(tmp_path / "source-fixtures" / "alfred.parquet",),
            readiness=None,  # type: ignore[arg-type]
        ),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "load_macro_features",
        append_values,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "_derive_current_cme_context",
        derive_cme,
    )

    assembled, source_files = rolling_materialization._attach_loop_a_features(
        tmp_path,
        decision,
        symbols=("MU",),
        horizon=horizon,
        source_timeframe=specification.source_timeframe,
        provider="databento",
        feature_set_name=feature_set.name,
        parquet_cache={},
        derived_cache={},
    )

    expected_attached = {
        feature.name
        for feature in feature_set.features
        if feature.source_family not in {"mr", "bp"}
    }
    expected_dispatches = set().union(
        *(
            _DISPATCH_BY_SOURCE_FAMILY[family]
            for family in {
                feature.source_family for feature in feature_set.features
            }
            if family not in {"mr", "bp"}
        )
    )
    assert attached == expected_attached
    assert set(dispatched) == expected_dispatches
    assert set(derived) == {
        family for family in ("cme",) if feature_set.for_family(family)
    }
    assert source_files
    assert list(assembled.loc[:, feature_set.names].columns) == list(
        feature_set.names
    )

    matrix = model_matrix_for_feature_set(assembled, feature_set)

    assert list(matrix.columns) == list(feature_set.names)
    assert {
        "id",
        "symbol",
        "provider",
        "source_file_path",
        "decision_timestamp",
        "available_at",
        "audit_metadata",
    }.isdisjoint(matrix.columns)


def test_canonical_prediction_has_readable_identity_and_model_version() -> None:
    prediction_created_at = pd.Timestamp("2026-07-30T08:31:00Z")
    model_version = "20260730T083100.000000Z"
    rows = pd.DataFrame(
        {
            "symbol": ["MU"],
            "provider": ["databento"],
            "horizon": ["1d"],
            "decision_timestamp": [
                pd.Timestamp("2026-07-29T20:05:00Z")
            ],
            "information_available_at": [
                pd.Timestamp("2026-07-29T20:05:00Z")
            ],
            "target_window_start": [
                pd.Timestamp("2026-07-30T13:30:00Z")
            ],
            "target_window_end": [
                pd.Timestamp("2026-07-30T20:00:00Z")
            ],
            "actionable_until": [
                pd.Timestamp("2026-07-30T13:30:00Z")
            ],
            "assumed_round_trip_cost": [0.001],
        }
    )

    class PredictionModel:
        model_name = "logistic-1d"
        artifact_directory = Path("models") / model_version
        calibration_method = "platt"

        @staticmethod
        def probabilities(_: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
            return np.array([0.61]), np.array([0.64])

    prediction = _prediction_frame(
        PredictionModel(),
        rows,
        prediction_created_at=prediction_created_at,
        mode="LIVE",
    )

    assert len(prediction) == 1
    assert prediction.loc[0, "id"] == (
        "MU|1d|2026-07-29T20:05:00Z|2026-07-30T08:31:00Z"
    )
    assert not is_opaque_identifier(prediction.loc[0, "id"])
    assert prediction.loc[0, "raw_probability"] == pytest.approx(0.61)
    assert prediction.loc[0, "calibrated_probability"] == pytest.approx(0.64)
    assert prediction.loc[0, "prediction_created_at"] == prediction_created_at
    assert prediction.loc[0, "model_name"] == "logistic-1d"
    assert prediction.loc[0, "model_version"] == model_version
    assert not is_opaque_identifier(prediction.loc[0, "model_version"])


def test_partitioned_inputs_require_a_parquet_for_every_symbol(
    tmp_path: Path,
) -> None:
    mu = tmp_path / "stocks" / "MU" / "features"
    mu.mkdir(parents=True)
    pd.DataFrame({"value": [1.0]}).to_parquet(
        mu / "part.parquet",
        index=False,
    )

    with pytest.raises(
        FileNotFoundError,
        match=r"NVDA.*features",
    ):
        rolling_materialization._partitioned_stock_paths(
            tmp_path,
            ("MU", "NVDA"),
            ("features",),
        )


@pytest.mark.parametrize("horizon", ("1h", "4h"))
def test_intraday_routes_report_the_missing_daily_technical_lifecycle_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    horizon: str,
) -> None:
    specification = horizon_specifications_for_profile(
        "loop-a-all-v1",
        horizons=(horizon,),
    )[horizon]
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        specification.feature_set,
        require_active=True,
        horizon=horizon,
    )
    decisions = _technical_decision(feature_set, horizon=horizon)
    bar_path = (
        tmp_path
        / "stocks"
        / "MU"
        / "technicals"
        / "bar-shape"
        / "databento"
        / "1h.parquet"
    )
    bar_path.parent.mkdir(parents=True)
    pd.DataFrame({"fixture": [1.0]}).to_parquet(bar_path, index=False)

    def attach_bar_values(
        frame: pd.DataFrame,
        _source: pd.DataFrame,
        *,
        value_columns: dict[str, str],
        **_kwargs: object,
    ) -> pd.DataFrame:
        output = frame.copy()
        for column in value_columns:
            output[column] = 1.0
        return output

    monkeypatch.setattr(
        rolling_materialization,
        "load_bar_shape_features",
        attach_bar_values,
    )

    with pytest.raises(
        FileNotFoundError,
        match=(
            r"technical lifecycle.*stocks[\\/]MU[\\/]signals[\\/]"
            r"technical-lifecycle[\\/]consensus[\\/]daily.parquet"
        ),
    ):
        rolling_materialization._attach_loop_a_features(
            tmp_path,
            decisions,
            symbols=("MU",),
            horizon=horizon,
            source_timeframe=specification.source_timeframe,
            provider="databento",
            feature_set_name=feature_set.name,
            parquet_cache={},
            derived_cache={},
        )


@pytest.mark.parametrize("horizon", ("1d", "1w"))
def test_daily_routes_report_the_missing_market_regime_file(
    tmp_path: Path,
    horizon: str,
) -> None:
    specification = horizon_specifications_for_profile(
        "loop-a-all-v1",
        horizons=(horizon,),
    )[horizon]
    bars = BarDataset(
        provider="databento",
        timeframe=specification.source_timeframe,
        symbol="GOOG",
        frame=pd.DataFrame({"timestamp": [pd.Timestamp("2026-07-30T00:00:00Z")]}),
        source_files=(tmp_path / "bars.parquet",),
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
    )
    expected = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "technicals"
        / "market-regime"
        / "databento"
        / "1d.parquet"
    )

    with pytest.raises(FileNotFoundError, match=r"market-regime.*1d.parquet"):
        rolling_materialization._load_operational_sources(
            tmp_path,
            symbol="GOOG",
            provider="databento",
            timeframe=specification.source_timeframe,
            bars=bars,
        )
    assert not expected.exists()


def test_symbol_join_is_backward_asof_and_respects_freshness() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["MU", "MU", "MU"],
            "decision_timestamp": pd.to_datetime(
                [
                    "2026-07-30T10:00:00Z",
                    "2026-07-30T11:00:00Z",
                    "2026-07-30T13:00:00Z",
                ],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["MU"],
            "available_at": pd.to_datetime(
                ["2026-07-30T10:30:00Z"],
                utc=True,
            ),
            "signal": [7.0],
        }
    )

    joined = rolling_materialization._join_symbol_values(
        decisions,
        source,
        family="fixture",
        value_columns={"fixture__signal": "signal"},
        freshness=pd.Timedelta(hours=1),
    )

    assert pd.isna(joined.loc[0, "fixture__signal"])
    assert joined.loc[1, "fixture__signal"] == 7.0
    assert pd.isna(joined.loc[2, "fixture__signal"])


@pytest.mark.parametrize("future_receipt_exists", (False, True))
def test_option_family_before_first_causal_receipt_is_explicitly_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_receipt_exists: bool,
) -> None:
    feature = FeatureSpec(
        name="opt__fixture",
        source_family="opt",
        source_column="fixture",
        applicable_horizons=("1h",),
    )
    feature_set = FeatureSet(
        "option-first-receipt-fixture",
        (feature,),
        applicable_horizons=("1h",),
    )
    decisions = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "decision_timestamp": [pd.Timestamp("2026-08-18T20:05:00Z")],
        }
    )
    monkeypatch.setattr(
        rolling_materialization.DEFAULT_FEATURE_REGISTRY,
        "feature_set",
        lambda *_args, **_kwargs: feature_set,
    )
    monkeypatch.setattr(
        rolling_materialization,
        "read_committed_option_surfaces",
        lambda *_args, **_kwargs: (pd.DataFrame(), ()),
    )
    monkeypatch.setattr(
        rolling_materialization,
        "committed_option_snapshots",
        lambda *_args, **_kwargs: ((object(),) if future_receipt_exists else ()),
    )
    if future_receipt_exists:
        monkeypatch.setattr(
            rolling_materialization,
            "_stock_glob_paths",
            lambda *_args, **_kwargs: pytest.fail(
                "a future receipt must not activate a legacy or future mapping"
            ),
        )

    assembled, sources = rolling_materialization._attach_loop_a_features(
        tmp_path,
        decisions,
        symbols=("AAPL",),
        horizon="1h",
        source_timeframe="1h",
        provider="databento",
        feature_set_name=feature_set.name,
        parquet_cache={},
        derived_cache={},
        input_available_at="2026-08-18T20:05:00Z",
    )

    assert pd.isna(assembled.loc[0, "opt__fixture"])
    assert sources == ()


def test_option_family_does_not_hide_a_committed_reader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = FeatureSpec(
        name="opt__fixture",
        source_family="opt",
        source_column="fixture",
        applicable_horizons=("1h",),
    )
    feature_set = FeatureSet(
        "option-corruption-fixture",
        (feature,),
        applicable_horizons=("1h",),
    )
    decisions = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "decision_timestamp": [pd.Timestamp("2026-08-18T20:05:00Z")],
        }
    )
    monkeypatch.setattr(
        rolling_materialization.DEFAULT_FEATURE_REGISTRY,
        "feature_set",
        lambda *_args, **_kwargs: feature_set,
    )

    def corrupt(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("checksum-invalid option receipt")

    monkeypatch.setattr(
        rolling_materialization,
        "read_committed_option_surfaces",
        corrupt,
    )

    with pytest.raises(RuntimeError, match="checksum-invalid option receipt"):
        rolling_materialization._attach_loop_a_features(
            tmp_path,
            decisions,
            symbols=("AAPL",),
            horizon="1h",
            source_timeframe="1h",
            provider="databento",
            feature_set_name=feature_set.name,
            parquet_cache={},
            derived_cache={},
            input_available_at="2026-08-18T20:05:00Z",
        )


def test_cme_family_before_first_verified_context_is_explicitly_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = FeatureSpec(
        name="cme__fixture",
        source_family="cme",
        source_column="fixture",
        applicable_horizons=("1h",),
    )
    feature_set = FeatureSet(
        "cme-first-context-fixture",
        (feature,),
        applicable_horizons=("1h",),
    )
    decisions = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "decision_timestamp": [pd.Timestamp("2026-08-18T20:05:00Z")],
        }
    )
    monkeypatch.setattr(
        rolling_materialization.DEFAULT_FEATURE_REGISTRY,
        "feature_set",
        lambda *_args, **_kwargs: feature_set,
    )

    assembled, sources = rolling_materialization._attach_loop_a_features(
        tmp_path,
        decisions,
        symbols=("AAPL",),
        horizon="1h",
        source_timeframe="1h",
        provider="databento",
        feature_set_name=feature_set.name,
        parquet_cache={},
        derived_cache={},
    )

    assert pd.isna(assembled.loc[0, "cme__fixture"])
    assert sources == ()


def test_partially_present_legacy_cme_family_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = FeatureSpec(
        name="cme__fixture",
        source_family="cme",
        source_column="fixture",
        applicable_horizons=("1h",),
    )
    feature_set = FeatureSet(
        "cme-partial-fixture",
        (feature,),
        applicable_horizons=("1h",),
    )
    decisions = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "decision_timestamp": [pd.Timestamp("2026-08-18T20:05:00Z")],
        }
    )
    paths = rolling_materialization._cme_normalized_source_paths(tmp_path)
    paths[0].parent.mkdir(parents=True)
    pd.DataFrame({"fixture": [1.0]}).to_parquet(paths[0], index=False)
    monkeypatch.setattr(
        rolling_materialization.DEFAULT_FEATURE_REGISTRY,
        "feature_set",
        lambda *_args, **_kwargs: feature_set,
    )

    with pytest.raises(FileNotFoundError, match="Databento CME"):
        rolling_materialization._attach_loop_a_features(
            tmp_path,
            decisions,
            symbols=("AAPL",),
            horizon="1h",
            source_timeframe="1h",
            provider="databento",
            feature_set_name=feature_set.name,
            parquet_cache={},
            derived_cache={},
        )


def test_midweek_1w_model_matrix_uses_only_last_completed_exchange_week(
    canonical_daily_bars: pd.DataFrame,
) -> None:
    decision_at = pd.Timestamp("2026-07-29T20:05:00Z")
    full_source = calculate_weekly_context(
        canonical_daily_bars,
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )
    prefix_source = calculate_weekly_context(
        canonical_daily_bars.loc[
            canonical_daily_bars["timestamp"].le(
                pd.Timestamp("2026-07-29T00:00:00Z")
            )
        ],
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )
    decisions = pd.DataFrame(
        {
            "symbol": ["GOOG"],
            "horizon": ["1w"],
            "decision_timestamp": [decision_at],
        }
    )
    joined_full = load_weekly_context_features(
        decisions,
        full_source,
        value_columns=WEEKLY_CONTEXT_VALUES,
    )
    joined_prefix = load_weekly_context_features(
        decisions,
        prefix_source,
        value_columns=WEEKLY_CONTEXT_VALUES,
    )
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        "loop-a-all-v1-1w",
        require_active=True,
        horizon="1w",
    )
    joined_full = _with_complete_model_features(joined_full, feature_set)
    joined_prefix = _with_complete_model_features(joined_prefix, feature_set)

    full_matrix = model_matrix_for_feature_set(joined_full, feature_set)
    prefix_matrix = model_matrix_for_feature_set(joined_prefix, feature_set)
    last_completed = full_source.loc[
        full_source["week_end_session"].eq(
            pd.Timestamp("2026-07-24T00:00:00Z")
        )
    ].iloc[0]
    future_completed = full_source.loc[
        full_source["week_end_session"].eq(
            pd.Timestamp("2026-07-31T00:00:00Z")
        )
    ].iloc[0]

    assert future_completed["available_at"] > decision_at
    assert joined_full.loc[
        0, "weekly__audit_week_end_session"
    ] == pd.Timestamp(
        "2026-07-24T00:00:00Z"
    )
    assert joined_full.loc[0, "weekly__available_at"] == last_completed[
        "available_at"
    ]
    for model_name, source_name in WEEKLY_CONTEXT_VALUES.items():
        assert full_matrix.loc[0, model_name] == pytest.approx(
            last_completed[source_name]
        )
    pd.testing.assert_frame_equal(full_matrix, prefix_matrix)
    assert list(full_matrix.columns) == list(feature_set.names)
    assert {
        "weekly__available_at",
        "weekly__audit_week_start_session",
        "weekly__audit_week_end_session",
        "decision_timestamp",
    }.isdisjoint(full_matrix.columns)


def test_cme_fallback_uses_common_full_hour_endpoints() -> None:
    window_start = pd.Timestamp("2026-07-30T03:00:00Z")
    fetched_at = pd.Timestamp("2026-07-30T05:00:00Z")
    ohlcv_rows: list[dict[str, object]] = []
    for offset, root in enumerate(("NQ", "ES", "RTY", "GC", "CL")):
        start = 100.0 + offset
        ohlcv_rows.extend(
            [
                {
                    "symbol": f"{root}.v.0",
                    "timestamp": window_start,
                    "fetched_at": fetched_at,
                    "open": start,
                    "close": start,
                },
                {
                    "symbol": f"{root}.v.0",
                    "timestamp": window_start + pd.Timedelta(minutes=59),
                    "fetched_at": fetched_at,
                    "open": start + 1.0,
                    "close": start + 1.0,
                },
            ]
        )
    empty_bbo = pd.DataFrame(
        columns=[
            "symbol",
            "timestamp",
            "fetched_at",
            "bid_px_00",
            "ask_px_00",
        ]
    )
    empty_book = pd.DataFrame(
        columns=[
            "symbol",
            "timestamp",
            "fetched_at",
            "bid_sz_00",
            "ask_sz_00",
        ]
    )

    context = rolling_materialization._derive_current_cme_context(
        pd.DataFrame(ohlcv_rows),
        empty_bbo,
        empty_book,
    )

    assert context.loc[0, "window_start"] == window_start
    assert context.loc[0, "window_end"] == window_start + pd.Timedelta(hours=1)
    assert context.loc[
        0,
        [
            "nq_return",
            "es_return",
            "rty_minus_es_return",
            "nq_minus_es_return",
            "gold_return",
            "crude_return",
        ],
    ].notna().all()
    assert pd.isna(context.loc[0, "relative_spread"])
    assert pd.isna(context.loc[0, "book_imbalance"])


def test_cme_fallback_uses_existing_status_snapshot_for_a_closed_schema(
    tmp_path: Path,
) -> None:
    for dataset in ("cme_context_ohlcv-1m", "cme_context_bbo-1m"):
        path = (
            tmp_path
            / "pools"
            / "cme"
            / "CME_CONTEXT"
            / dataset
            / "databento"
            / "normalized"
            / f"CME_CONTEXT_{dataset}.parquet"
        )
        path.parent.mkdir(parents=True)
        path.touch()
    status_path = (
        tmp_path
        / "pools"
        / "cme"
        / "CME_CONTEXT"
        / "cme_context_mbp-10_status"
        / "databento"
        / "normalized"
        / "CME_CONTEXT_cme_context_mbp-10_status.parquet"
    )
    status_path.parent.mkdir(parents=True)
    status_path.touch()

    paths = rolling_materialization._cme_normalized_source_paths(tmp_path)

    assert paths[-1] == status_path


def _technical_decision(feature_set, *, horizon: str) -> pd.DataFrame:
    decision_timestamp = pd.Timestamp("2026-07-29T20:05:00Z")
    row: dict[str, object] = {
        "id": f"MU|{horizon}|2026-07-29T20:05:00Z",
        "symbol": "MU",
        "provider": "databento",
        "horizon": horizon,
        "decision_timestamp": decision_timestamp,
        "information_available_at": decision_timestamp,
        "available_at": decision_timestamp,
        "source_file_path": r"C:\DATASTORE\source.parquet",
        "audit_metadata": "excluded",
    }
    for feature in feature_set.features:
        if feature.source_family in {"mr", "bp"}:
            row[feature.name] = 1.0
    return pd.DataFrame([row])


def _with_complete_model_features(frame: pd.DataFrame, feature_set) -> pd.DataFrame:
    missing = {
        feature.name: [float(offset) / 100.0]
        for offset, feature in enumerate(feature_set.features, start=1)
        if feature.name not in frame
    }
    return pd.concat(
        [frame.reset_index(drop=True), pd.DataFrame(missing)],
        axis=1,
    )


@pytest.fixture
def canonical_daily_bars() -> pd.DataFrame:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar(
        "XNYS",
        start="2025-05-05",
        end="2026-07-31",
    )
    sessions = calendar.sessions_in_range("2025-05-05", "2026-07-31")
    timestamps = pd.DatetimeIndex(sessions)
    if timestamps.tz is None:
        timestamps = timestamps.tz_localize("UTC")
    else:
        timestamps = timestamps.tz_convert("UTC")
    row = np.arange(len(timestamps), dtype=float)
    opens = 100.0 + row * 0.05
    closes = opens + np.sin(row / 3.0)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": np.maximum(opens, closes) + 0.5,
            "low": np.minimum(opens, closes) - 0.5,
            "close": closes,
            "volume": 1_000_000.0 + row,
            "bar_complete": True,
        }
    )
