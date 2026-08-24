from __future__ import annotations

import json
from pathlib import Path

import joblib
import pandas as pd

from ml.artifacts import file_checksum
from ml.strategy_profit_training import (
    ExecutionHaircutModel,
    _balanced_cluster_samples,
    _modeled_chain,
    _opra_partition_path,
)
from ml.strategy_selection.contracts import (
    STRATEGY_MODEL_POLICY_VERSION,
    StrategyModel,
)
from ml.strategy_selection.runtime import _opra_execution_model_eligible
from ml.strategy_selection.slow_model import (
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
                7_201.0,
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
            / "1h"
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
            tmp_path, "ohlcv-1h", str(row["symbol"]), session
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
    for horizon in ("1d", "1w"):
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
