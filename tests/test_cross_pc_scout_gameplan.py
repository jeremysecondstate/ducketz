"""Actual Gameplan source policy under distinct, preserved local symbol profiles.

Only the pure source selector is imported: no acquisition, training, credentials,
application runtime, or broker modules participate in this offline check.
"""
from copy import deepcopy
import json

import pandas as pd
import pytest

from ml.gameplan_source_selection import (
    GAMEPLAN_SOURCE_SELECTION_VERSION,
    select_prior_session_sources,
)


@pytest.mark.parametrize("available_at,expected_scores", [
    ("2026-01-07T01:04:59Z", [1.0, 2.0]),
    ("2026-01-07T01:05:00Z", [10.0, 20.0]),
])
def test_actual_gameplan_policy_matches_across_preserved_symbol_profiles(
    tmp_path, available_at, expected_scores,
):
    profiles = [
        {"actor": "Atlas", "machine": "pc-original", "symbols": ["COST", "IONQ"]},
        {"actor": "Scout", "machine": "pc-new", "symbols": ["DOCU", "DBX"]},
    ]
    original_profiles = deepcopy(profiles)
    saved_profiles = []
    rows = []
    for profile in profiles:
        profile_path = tmp_path / (profile["machine"] + "-local-profile.json")
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        saved_profiles.append((profile_path, profile_path.read_bytes()))
        for slot, symbol in enumerate(profile["symbols"]):
            for hour, multiplier in [(12, 1.0), (16, 10.0)]:
                start = pd.Timestamp(f"2026-01-06 {hour}:00", tz="America/Los_Angeles").tz_convert("UTC")
                end = start + pd.Timedelta(hours=1)
                known_at = end + pd.Timedelta(minutes=5)
                rows.append({
                    "symbol": symbol, "fixture_slot": slot,
                    "horizon": "1h", "timeframe": "1h",
                    "exchange_calendar": "XNAS",
                    "exchange_session": pd.Timestamp("2026-01-06", tz="UTC"),
                    "bar_timestamp": start, "bar_end_timestamp": end,
                    "information_available_at": known_at, "decision_timestamp": known_at,
                    "mr__technical_score": (slot + 1) * multiplier,
                })

    # Both calls see the same mixed universe. Only their local symbol profile
    # differs; later bars cannot be selected before their availability clock.
    samples = pd.DataFrame(rows)
    original_samples = samples.copy(deep=True)
    results = []
    for profile in profiles:
        selected = select_prior_session_sources(
            samples, symbols=profile["symbols"], available_at=pd.Timestamp(available_at),
            feature_columns=["mr__technical_score"],
        ).sort_values("fixture_slot").reset_index(drop=True)
        assert selected.symbol.tolist() == profile["symbols"]
        assert selected.mr__technical_score.tolist() == expected_scores
        assert selected.source_selection_contract.eq(GAMEPLAN_SOURCE_SELECTION_VERSION).all()
        assert selected.source_action_start.eq(pd.Timestamp("2026-01-07T12:00Z")).all()
        assert selected.information_available_at.le(pd.Timestamp(available_at)).all()
        assert selected.attrs["source_selection"]["selected_rows_by_symbol"] == {
            symbol: 1 for symbol in profile["symbols"]
        }
        # Per-symbol counts differ by design; compare all row policy output
        # after removing only the symbol labels.
        common = selected.drop(columns="symbol")
        common.attrs = {}
        results.append(common)

    pd.testing.assert_frame_equal(results[0], results[1])
    pd.testing.assert_frame_equal(samples, original_samples)
    assert profiles == original_profiles
    for path, original_bytes in saved_profiles:
        assert path.read_bytes() == original_bytes
