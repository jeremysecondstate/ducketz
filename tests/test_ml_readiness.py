from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ml.contracts import (
    ACTIVE,
    BLOCKED,
    IMPLEMENTED_BUT_QUARANTINED,
    MLContractError,
)
from ml.readiness import evaluate_feature_readiness


def test_readiness_reports_active_safe_coverage_deterministically(
    tmp_path: Path,
) -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["quote__relative_bid_ask_spread"] = [0.01, 0.02, 0.03, 0.04]
    joined["quote__available_at"] = decisions["decision_timestamp"]
    joined["quote__is_stale"] = False
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "MU"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z", "2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "schema_version": ["quote-liquidity-v1"] * 2,
            "calculation_version": ["1.0.0"] * 2,
        }
    )
    source_path = tmp_path / "quotes.parquet"
    source_path.write_bytes(b"fixture")

    kwargs = {
        "feature_set_name": "quote-candidate-v1",
        "horizon": "1h",
        "feature_names": ("quote__relative_bid_ask_spread",),
        "source_frames": {"quote": source},
        "source_files": {"quote": (source_path,)},
        "natural_keys": {"quote": ("symbol", "available_at")},
        "activation_status": {
            "quote__relative_bid_ask_spread": ACTIVE,
        },
        "required_schema_versions": {"quote": ("quote-liquidity-v1",)},
        "required_calculation_versions": {"quote": ("1.0.0",)},
        "minimum_eligible_decisions": 4,
        "minimum_symbol_coverage": 1.0,
    }
    first = evaluate_feature_readiness(decisions, joined, **kwargs)
    second = evaluate_feature_readiness(decisions, joined, **kwargs)

    assert first.state == ACTIVE
    assert first.as_dict() == second.as_dict()
    evidence = first.features[0]
    assert evidence.symbol_coverage == 1.0
    assert evidence.eligible_decision_count == 4
    assert evidence.null_rate == 0.0
    assert evidence.stale_row_rate == 0.0
    assert evidence.minimum_coverage_passes
    assert evidence.first_safe_available_at == pd.Timestamp(
        "2026-07-29T10:00:00Z"
    )
    first.ensure_model_ready()


def test_readiness_quarantines_insufficient_coverage() -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["quote__relative_bid_ask_spread"] = [0.01, 0.02, None, None]
    joined["quote__available_at"] = pd.to_datetime(
        [
            "2026-07-29T10:00:00Z",
            "2026-07-29T11:00:00Z",
            None,
            None,
        ],
        utc=True,
    )
    joined["quote__is_stale"] = [False, False, True, True]
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "schema_version": ["quote-liquidity-v1"],
            "calculation_version": ["1.0.0"],
        }
    )
    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="quote-candidate-v1",
        horizon="1h",
        feature_names=("quote__relative_bid_ask_spread",),
        source_frames={"quote": source},
        natural_keys={"quote": ("symbol", "available_at")},
        activation_status={
            "quote__relative_bid_ask_spread": ACTIVE,
        },
        minimum_eligible_decisions=4,
        minimum_symbol_coverage=1.0,
    )

    assert report.state == IMPLEMENTED_BUT_QUARANTINED
    evidence = report.features[0]
    assert not evidence.minimum_coverage_passes
    assert evidence.symbol_coverage == 0.5
    assert evidence.null_rate == 0.5
    assert evidence.stale_row_rate == 0.5
    assert "eligible decisions 2 < 4" in evidence.blocking_reason
    with pytest.raises(MLContractError, match="not ready"):
        report.ensure_model_ready()


def test_readiness_blocks_duplicate_keys_and_missing_audit_availability() -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["macro__cpi_yoy"] = [0.02] * 4
    source = pd.DataFrame(
        {
            "series_name": ["CPIAUCSL", "CPIAUCSL"],
            "observation_date": ["2026-06-01", "2026-06-01"],
            "realtime_start": ["2026-07-15", "2026-07-15"],
        }
    )
    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="macro-candidate-v1",
        horizon="1d",
        feature_names=("macro__cpi_yoy",),
        source_frames={"macro": source},
        natural_keys={
            "macro": (
                "series_name",
                "observation_date",
                "realtime_start",
            )
        },
    )

    assert report.state == BLOCKED
    evidence = report.features[0]
    assert evidence.duplicate_key_failures == 1
    assert "duplicate natural keys: 1" in evidence.blocking_reason
    assert "macro__available_at" in evidence.blocking_reason


def test_registry_quarantine_state_prevents_active_report() -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["life__timing_score"] = [1.0] * 4
    joined["life__available_at"] = decisions["decision_timestamp"]
    joined["life__is_stale"] = False
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "MU"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z", "2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "schema_version": ["technical-lifecycle-v1"] * 2,
            "calculation_version": ["1.0.0"] * 2,
        }
    )
    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="technical-lifecycle-candidate-v1",
        horizon="1d",
        feature_names=("life__timing_score",),
        source_frames={"life": source},
        natural_keys={"life": ("symbol", "available_at")},
        activation_status={
            "life__timing_score": IMPLEMENTED_BUT_QUARANTINED
        },
        minimum_eligible_decisions=4,
        minimum_symbol_coverage=1.0,
    )

    assert report.state == IMPLEMENTED_BUT_QUARANTINED
    assert (
        report.features[0].blocking_reason
        == "registry activation is IMPLEMENTED_BUT_QUARANTINED"
    )


def test_readiness_prefers_feature_lineage_clock_and_staleness() -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["macro__cpi_yoy"] = [0.02] * 4
    joined["macro__available_at"] = decisions["decision_timestamp"]
    joined["macro__is_stale"] = False
    lineage = decisions["decision_timestamp"] - pd.Timedelta(days=1)
    joined["macro__cpi_yoy__available_at"] = lineage
    joined["macro__cpi_yoy__is_stale"] = [False, True, False, True]

    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="macro-candidate-v1",
        horizon="1d",
        feature_names=("macro__cpi_yoy",),
        source_frames={
            "macro": pd.DataFrame(
                {
                    "context_name": ["macro-release-context"],
                    "available_at": pd.to_datetime(
                        ["2026-07-29T10:00:00Z"],
                        utc=True,
                    ),
                    "schema_version": ["macro-release-context-v1"],
                    "calculation_version": ["1.0.0"],
                }
            )
        },
        natural_keys={"macro": ("context_name", "available_at")},
        activation_status={"macro__cpi_yoy": ACTIVE},
        minimum_eligible_decisions=2,
        minimum_symbol_coverage=1.0,
    )

    evidence = report.features[0]
    assert report.state == ACTIVE
    assert evidence.eligible_decision_count == 2
    assert evidence.stale_row_rate == 0.5
    assert evidence.first_safe_available_at == pd.Timestamp(
        "2026-07-28T10:00:00Z"
    )
    assert evidence.last_safe_available_at == pd.Timestamp(
        "2026-07-28T10:00:00Z"
    )


def test_readiness_never_activates_empty_or_malformed_evidence() -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["quote__relative_bid_ask_spread"] = [0.01] * 4
    joined["quote__available_at"] = decisions["decision_timestamp"]
    joined["quote__is_stale"] = "garbage"
    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="quote-candidate-v1",
        horizon="1h",
        feature_names=("quote__relative_bid_ask_spread",),
        source_frames={
            "quote": pd.DataFrame(columns=["symbol", "available_at"])
        },
    )

    assert report.state == BLOCKED
    evidence = report.features[0]
    assert not evidence.minimum_coverage_passes
    assert "invalid values" in evidence.blocking_reason


def test_readiness_rejects_permuted_decision_rows() -> None:
    decisions = _decisions()
    joined = decisions.iloc[::-1].reset_index(drop=True)
    joined["quote__relative_bid_ask_spread"] = 0.01
    joined["quote__available_at"] = joined["decision_timestamp"]
    joined["quote__is_stale"] = False

    with pytest.raises(MLContractError, match="misaligned"):
        evaluate_feature_readiness(
            decisions,
            joined,
            feature_set_name="quote-candidate-v1",
            horizon="1h",
            feature_names=("quote__relative_bid_ask_spread",),
            source_frames={
                "quote": pd.DataFrame(
                    {"symbol": ["NVDA"], "available_at": ["2026-07-29"]}
                )
            },
        )


def test_readiness_omitted_policy_cannot_activate_safe_looking_rows() -> None:
    decisions = _decisions()
    joined = decisions.copy()
    joined["quote__relative_bid_ask_spread"] = 0.01
    joined["quote__available_at"] = joined["decision_timestamp"]
    joined["quote__is_stale"] = False
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "MU"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z"] * 2,
                utc=True,
            ),
            "schema_version": ["quote-liquidity-v1"] * 2,
            "calculation_version": ["1.0.0"] * 2,
        }
    )

    missing_activation = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="quote-candidate-v1",
        horizon="1h",
        feature_names=("quote__relative_bid_ask_spread",),
        source_frames={"quote": source},
        natural_keys={"quote": ("symbol", "available_at")},
        minimum_eligible_decisions=4,
    )
    missing_key_policy = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="quote-candidate-v1",
        horizon="1h",
        feature_names=("quote__relative_bid_ask_spread",),
        source_frames={"quote": source},
        activation_status={"quote__relative_bid_ask_spread": ACTIVE},
        minimum_eligible_decisions=4,
    )

    assert missing_activation.state == IMPLEMENTED_BUT_QUARANTINED
    assert "registry activation is IMPLEMENTED_BUT_QUARANTINED" in (
        missing_activation.features[0].blocking_reason
    )
    assert missing_key_policy.state == BLOCKED
    assert "natural-key policy is absent" in (
        missing_key_policy.features[0].blocking_reason
    )


def test_readiness_normalizes_natural_keys_before_duplicate_check() -> None:
    decisions = _decisions().iloc[:1].copy()
    joined = decisions.copy()
    joined["quote__relative_bid_ask_spread"] = 0.01
    joined["quote__available_at"] = joined["decision_timestamp"]
    joined["quote__is_stale"] = False
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", " nvda "],
            "available_at": [
                "2026-07-29T10:00:00Z",
                "2026-07-29T10:00:00+00:00",
            ],
            "schema_version": ["quote-liquidity-v1"] * 2,
            "calculation_version": ["1.0.0"] * 2,
        }
    )

    report = evaluate_feature_readiness(
        decisions,
        joined,
        feature_set_name="quote-candidate-v1",
        horizon="1h",
        feature_names=("quote__relative_bid_ask_spread",),
        source_frames={"quote": source},
        natural_keys={"quote": ("symbol", "available_at")},
        activation_status={"quote__relative_bid_ask_spread": ACTIVE},
    )

    assert report.state == BLOCKED
    assert report.features[0].duplicate_key_failures == 1
    assert "duplicate natural keys: 1" in report.features[0].blocking_reason


def _decisions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA", "MU", "MU"],
            "decision_timestamp": pd.to_datetime(
                [
                    "2026-07-29T10:00:00Z",
                    "2026-07-29T11:00:00Z",
                    "2026-07-29T10:00:00Z",
                    "2026-07-29T11:00:00Z",
                ],
                utc=True,
            ),
        }
    )
