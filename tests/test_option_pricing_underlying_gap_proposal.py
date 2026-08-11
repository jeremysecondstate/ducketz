from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from datafetching.decision_time import completed_bar_clock_for_target
from ml.artifacts import semantic_metadata_fingerprint


def test_no_trade_proposal_is_hashed_inactive_and_requires_exact_approval() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "datafetch-ml"
        / "opra-underlying-no-trade-proposal-v2.json"
    )
    proposal = json.loads(path.read_text(encoding="utf-8"))

    assert proposal["status"] == "PROPOSED_NOT_APPROVED"
    assert proposal["active"] is False
    assert proposal["explicit_operator_approval_received"] is False
    assert proposal["automated_action_allowed"] is False
    assert proposal["policy"]["exact_rule"]["status"] == (
        "CURRENT_ACTIVE_RULE_REMAINS_UNCHANGED"
    )
    assert proposal["policy"]["gap_rule"]["maximum_staleness_seconds"] == 60
    assert proposal["policy"]["gap_rule"]["next_interval_fallback_forbidden"] is True
    assert proposal["policy"]["bbo_alternative"][
        "separate_cost_authorization_required"
    ] is True
    assert proposal["policy_hash_sha256"] == semantic_metadata_fingerprint(
        proposal["policy"]
    )


def test_active_completed_bar_contract_still_rejects_prior_minute_substitution(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "bars"
        / "1m"
        / "databento"
        / "normalized"
        / "source.parquet"
    )
    path.parent.mkdir(parents=True)
    _bars(["2026-04-10T18:58:00Z"]).to_parquet(path, index=False)

    with pytest.raises(FileNotFoundError, match="Exact completed"):
        completed_bar_clock_for_target(
            tmp_path,
            symbol="GOOG",
            target_snapshot_for="2026-04-10T19:00:00Z",
            as_of="2026-04-10T19:00:00Z",
        )

    _bars(
        ["2026-04-10T18:58:00Z", "2026-04-10T18:59:00Z"]
    ).to_parquet(path, index=False)
    exact = completed_bar_clock_for_target(
        tmp_path,
        symbol="GOOG",
        target_snapshot_for="2026-04-10T19:00:00Z",
        as_of="2026-04-10T19:00:00Z",
    )
    assert exact.bar_timestamp == pd.Timestamp("2026-04-10T18:59:00Z")
    assert exact.decision_timestamp == pd.Timestamp("2026-04-10T19:00:00Z")


def _bars(timestamps: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": "GOOG",
            "source": "databento",
            "timeframe": "1m",
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000.0,
            "provider_timeframe": "1m",
            "canonical_timeframe": "1m",
            "request_key": "source_1000d_1m_ohlcv-1m_1m",
            "provider_dataset": "EQUS.MINI",
            "bar_complete": True,
            "session_type": "REGULAR",
        }
    )
