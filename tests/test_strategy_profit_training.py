from __future__ import annotations

import json
from pathlib import Path

import joblib
import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml.strategy_profit_training import (
    ExecutionHaircutModel,
    _balanced_cluster_samples,
    _exact_cbbo_chain,
    _modeled_chain,
    _opra_partition_path,
    _within_listed_options_session,
)
from ml.strategy_profit_training_runtime import _validate_opra_history_freshness
from ml.strategy_selection.contracts import (
    STRATEGY_MODEL_POLICY_VERSION,
    StrategyModel,
)
from ml.strategy_selection.runtime import _opra_execution_model_eligible
from ml.strategy_selection.slow_model import (
    CANONICAL_PROFIT_HORIZONS,
    load_promoted_strategy_model,
    publish_slow_strategy_authority,
    strategy_model_promotion_gate,
)


def test_promotion_gate_requires_proper_score_and_calibration_quality() -> None:
    evaluation = {
        "assessment_decisions": 63,
        "calibrated_model": {
            "brier_score": 0.19,
            "log_loss": 0.55,
            "expected_calibration_error_10_bin": 0.08,
        },
        "base_rate_model": {
            "brier_score": 0.20,
            "log_loss": 0.60,
        },
    }
    assert strategy_model_promotion_gate(evaluation)["status"] == "PROMOTED"
    evaluation["calibrated_model"]["brier_score"] = 0.21
    gate = strategy_model_promotion_gate(evaluation)
    assert gate["status"] == "REJECTED"
    assert not gate["checks"]["brier_not_worse_than_training_base_rate"]


def test_intraday_promotion_gate_uses_declared_smaller_holdout() -> None:
    evaluation = {
        "assessment_decisions": 30,
        "calibrated_model": {
            "brier_score": 0.19,
            "log_loss": 0.55,
            "expected_calibration_error_10_bin": 0.08,
        },
        "base_rate_model": {
            "brier_score": 0.20,
            "log_loss": 0.60,
        },
    }

    gate = strategy_model_promotion_gate(
        evaluation,
        minimum_assessment_decisions=30,
    )

    assert gate["status"] == "PROMOTED"
    assert gate["minimum_assessment_decisions"] == 30


def test_intraday_training_excludes_windows_outside_listed_options_session() -> None:
    assert _within_listed_options_session(
        pd.Timestamp("2026-09-03T13:30:00Z"),
        pd.Timestamp("2026-09-03T14:30:00Z"),
    )
    assert _within_listed_options_session(
        pd.Timestamp("2026-09-03T13:30:00Z"),
        pd.Timestamp("2026-09-03T20:00:00Z"),
    )
    assert not _within_listed_options_session(
        pd.Timestamp("2026-09-03T11:00:00Z"),
        pd.Timestamp("2026-09-03T12:00:00Z"),
    )
    assert not _within_listed_options_session(
        pd.Timestamp("2026-09-03T19:00:00Z"),
        pd.Timestamp("2026-09-03T21:00:00Z"),
    )


def test_strategy_training_requires_opra_through_latest_complete_daily_sample() -> None:
    samples = pd.DataFrame(
        {
            "horizon": ["1d", "1d"],
            "label_status": ["COMPLETE", "COMPLETE"],
            "target_window_start": [
                pd.Timestamp("2026-09-02T13:30:00Z"),
                pd.Timestamp("2026-09-03T13:30:00Z"),
            ],
        }
    )
    report = {
        "fit_sessions": ["2026-09-01", "2026-09-02"],
        "assessment_sessions": ["2026-09-03"],
    }

    freshness = _validate_opra_history_freshness(
        samples,
        execution_report=report,
    )

    assert freshness["status"] == "CURRENT_FOR_LATEST_COMPLETE_1D_SAMPLE"
    assert freshness["latest_common_ohlcv_1h_cbbo_1m_session"] == "2026-09-03"


def test_strategy_training_fails_closed_when_opra_history_is_stale() -> None:
    samples = pd.DataFrame(
        {
            "horizon": ["1d"],
            "label_status": ["COMPLETE"],
            "target_window_start": [pd.Timestamp("2026-09-03T13:30:00Z")],
        }
    )

    with pytest.raises(RuntimeError, match="latest_common_session=2026-08-18"):
        _validate_opra_history_freshness(
            samples,
            execution_report={
                "fit_sessions": ["2026-08-17"],
                "assessment_sessions": ["2026-08-18"],
            },
        )


def test_strategy_training_verifies_every_production_history_cursor(
    tmp_path: Path,
) -> None:
    samples = pd.DataFrame(
        {
            "horizon": ["1d"],
            "label_status": ["COMPLETE"],
            "target_window_start": [pd.Timestamp("2026-09-03T13:30:00Z")],
        }
    )
    cursor_root = (
        tmp_path
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "state"
        / "symbol-history"
        / "AAPL"
    )
    cursor_root.mkdir(parents=True)
    for schema in ("ohlcv-1h", "cbbo-1m", "definition"):
        (cursor_root / f"{schema}.json").write_text(
            json.dumps(
                {
                    "dataset": "OPRA.PILLAR",
                    "symbol": "AAPL",
                    "schema": schema,
                    "completed_through": "2026-09-04",
                }
            ),
            encoding="utf-8",
        )

    current = _validate_opra_history_freshness(
        samples,
        execution_report={
            "fit_sessions": ["2026-09-02"],
            "assessment_sessions": ["2026-09-03"],
        },
        datastore_root=tmp_path,
        symbols=("AAPL",),
    )
    assert len(current["verified_strategy_history_cursors"]) == 3

    (cursor_root / "definition.json").unlink()
    with pytest.raises(RuntimeError, match="cursor is missing: AAPL/definition"):
        _validate_opra_history_freshness(
            samples,
            execution_report={
                "fit_sessions": ["2026-09-02"],
                "assessment_sessions": ["2026-09-03"],
            },
            datastore_root=tmp_path,
            symbols=("AAPL",),
        )


def test_modeled_chain_keeps_bar_evidence_distinct_from_bbo() -> None:
    hour = pd.Timestamp("2026-08-18T13:00:00Z")
    rows = []
    for option_type in ("C", "P"):
        for strike in range(95, 106):
            rows.append(
                {
                    "ts_event": hour,
                    "symbol": f"AAPL  260821{option_type}{strike * 1000:08d}",
                    "close": 2.0 + abs(strike - 100) * 0.25,
                    "volume": 25,
                }
            )
    haircuts = ExecutionHaircutModel(
        bucket_haircuts={"1_to_5": 0.10},
        global_haircut=0.10,
        fingerprint="abc",
        report={},
        source_files=(),
    )
    chain = _modeled_chain(
        pd.DataFrame(rows),
        symbol="AAPL",
        hour=hour,
        available_at=hour + pd.Timedelta(hours=1),
        underlying=100.0,
        haircuts=haircuts,
    )
    assert len(chain) == 22
    assert chain["source_provider"].eq(
        "databento-opra-ohlcv-modeled"
    ).all()
    assert chain["ask"].gt(chain["bid"]).all()
    assert chain["quote_timestamp"].eq(hour + pd.Timedelta(hours=1)).all()


def test_exact_cbbo_chain_uses_first_entry_and_last_exit_snapshots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "normalized.parquet"
    rows = []
    for timestamp, shift in (
        (pd.Timestamp("2026-08-18T13:31:00Z"), 0.0),
        (pd.Timestamp("2026-08-18T13:32:00Z"), 0.2),
    ):
        for option_type in ("C", "P"):
            rows.append(
                {
                    "ts_recv": timestamp,
                    "symbol": f"AAPL  260821{option_type}00100000",
                    "bid_px_00": 2.0 + shift,
                    "ask_px_00": 2.2 + shift,
                }
            )
    pd.DataFrame(rows).set_index("ts_recv").to_parquet(path)
    boundary = pd.Timestamp("2026-08-18T13:30:00Z")

    entry = _exact_cbbo_chain(
        path,
        symbol="AAPL",
        boundary=boundary,
        boundary_side="ENTRY",
        underlying=100.0,
    )
    exit_chain = _exact_cbbo_chain(
        path,
        symbol="AAPL",
        boundary=boundary + pd.Timedelta(minutes=2),
        boundary_side="EXIT",
        underlying=100.0,
    )

    assert entry["quote_timestamp"].eq(boundary + pd.Timedelta(minutes=1)).all()
    assert entry["source_provider"].eq("databento-opra").all()
    assert entry["quote_staleness_seconds"].eq(60.0).all()
    assert exit_chain["quote_timestamp"].eq(boundary + pd.Timedelta(minutes=2)).all()
    assert exit_chain["quote_staleness_seconds"].eq(0.0).all()


def test_opra_execution_scoring_fallback_is_still_quality_gated() -> None:
    frame = pd.DataFrame(
        {
            "pricing_mode": ["ACTIVE"] * 5,
            "pricing_source": [
                "UNAVAILABLE",
                "UNAVAILABLE",
                "UNAVAILABLE",
                "UNAVAILABLE",
                "BSGP",
            ],
            # The first row proves that theoretical-Pricing flags do not block
            # a separately qualified OPRA-execution probability.
            "surface_quality_pass": [False] * 5,
            "liquidity_policy_pass": [False] * 5,
            "all_option_quotes_valid": [True, True, True, False, True],
            "max_relative_spread": [0.10, 0.10, 0.50, 0.10, 0.10],
            "maximum_quote_staleness_seconds": [
                6_000.0,
                64_801.0,
                600.0,
                600.0,
                600.0,
            ],
            "minimum_open_interest": [5.0, 5.0, 5.0, 5.0, 5.0],
            "total_volume": [0.0, 0.0, 0.0, 0.0, 0.0],
        }
    )
    assert _opra_execution_model_eligible(frame).tolist() == [
        True,
        False,
        False,
        False,
        False,
    ]


def test_direct_opra_bbo_does_not_require_unpublished_volume_or_open_interest() -> None:
    frame = pd.DataFrame(
        {
            "pricing_mode": ["ACTIVE"],
            "pricing_source": ["OPRA_CBBO_PRIMARY_OHLCV_FALLBACK_EXECUTION"],
            "all_option_quotes_valid": [True],
            "max_relative_spread": [0.10],
            "maximum_quote_staleness_seconds": [21_600.0],
            "minimum_open_interest": [float("nan")],
            "total_volume": [float("nan")],
        }
    )

    assert _opra_execution_model_eligible(frame).tolist() == [True]


def test_training_rotates_one_available_symbol_per_independent_cluster(
    tmp_path: Path,
) -> None:
    rows = []
    starts = pd.date_range("2026-08-17T13:30:00Z", periods=3, freq="1D")
    for start in starts:
        for symbol in ("AAPL", "NVDA"):
            rows.append(
                {
                    "symbol": symbol,
                    "horizon": "1d",
                    "target_window_start": start,
                    "target_window_end": start + pd.Timedelta(hours=6, minutes=30),
                }
            )
    for symbol in ("AAPL", "NVDA"):
        (
            tmp_path
            / "stocks"
            / symbol
            / "bars"
            / "1m"
            / "databento"
            / "normalized"
        ).mkdir(parents=True)
    for row in rows:
        if row["symbol"] == "AAPL" and pd.Timestamp(
            row["target_window_start"]
        ) == starts[2]:
            continue
        session = pd.Timestamp(row["target_window_start"]).date().isoformat()
        path = _opra_partition_path(
            tmp_path, "cbbo-1m", str(row["symbol"]), session
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    selected = _balanced_cluster_samples(
        tmp_path,
        pd.DataFrame(rows),
        horizon="1d",
    )
    assert selected["symbol"].tolist() == ["AAPL", "NVDA", "NVDA"]
    assert selected["target_window_start"].nunique() == 3


def test_slow_model_pointer_verifies_receipts_and_weekly_aliases(
    tmp_path: Path,
) -> None:
    run = tmp_path / "ml" / "strategy-profit-training-runs" / "run-1"
    run.mkdir(parents=True)
    models = {}
    reports = {}
    outputs = []
    for horizon in CANONICAL_PROFIT_HORIZONS:
        artifact = (
            tmp_path
            / "ml"
            / "strategy-models"
            / horizon
            / "market-state-strategy-outcome"
            / "artifact-1"
        )
        artifact.mkdir(parents=True)
        model_path = artifact / "model.joblib"
        joblib.dump(
            {
                "estimator": object(),
                "return_estimator": object(),
                "calibrator": object(),
            },
            model_path,
        )
        manifest = {
            "horizon": horizon,
            "model_policy_version": STRATEGY_MODEL_POLICY_VERSION,
            "effective_calibration_method": "platt",
            "numeric_features": ["underlying_price"],
            "categorical_features": ["strategy_name"],
            "selected_probability_model_family": "hist-gradient",
            "offline_evaluation": {"assessment_decisions": 63},
            "training_data_fingerprint_sha256": f"fingerprint-{horizon}",
            "model_file": {
                "path": model_path.name,
                "size": model_path.stat().st_size,
                "checksum_sha256": file_checksum(model_path),
            },
        }
        (artifact / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        model = StrategyModel(
            horizon=horizon,
            estimator=object(),
            return_estimator=object(),
            calibrator=object(),
            numeric_features=("underlying_price",),
            categorical_features=("strategy_name",),
            artifact_directory=artifact,
            offline_evaluation={"assessment_decisions": 63},
        )
        report = {
            "status": "MODEL_FIT",
            "promotion_gate": {"status": "PROMOTED"},
        }
        report_path = run / f"{horizon}-model-report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        models[horizon] = model
        reports[horizon] = report
        outputs.append(report_path)
    summary = run / "training-report.json"
    summary.write_text("{}", encoding="utf-8")
    outputs.append(summary)
    publish_slow_strategy_authority(
        tmp_path,
        run_directory=run,
        models=models,
        reports=reports,
        published_at="2026-08-23T22:00:00Z",
        output_files=outputs,
    )
    loaded = load_promoted_strategy_model(tmp_path, horizon="1w-d3")
    assert loaded is not None
    assert loaded.canonical_horizon == "1d"
    assert loaded.model.horizon == "1d"

    model_path = models["1d"].artifact_directory / "model.joblib"
    model_path.write_bytes(model_path.read_bytes() + b"tampered")
    assert load_promoted_strategy_model(tmp_path, horizon="1d") is None

    partial = tmp_path / "ml" / "strategy-profit-training-runs" / "run-2"
    partial.mkdir()
    partial_report = partial / "1h-model-report.json"
    partial_report.write_text(json.dumps(reports["1h"]), encoding="utf-8")
    partial_summary = partial / "training-report.json"
    partial_summary.write_text("{}", encoding="utf-8")
    publish_slow_strategy_authority(
        tmp_path,
        run_directory=partial,
        models={"1h": models["1h"]},
        reports={"1h": reports["1h"]},
        published_at="2026-08-24T22:00:00Z",
        output_files=(partial_report, partial_summary),
    )
    assert load_promoted_strategy_model(tmp_path, horizon="1h") is not None
    assert load_promoted_strategy_model(tmp_path, horizon="4h") is None
