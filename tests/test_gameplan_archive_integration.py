from dataclasses import dataclass

import pandas as pd
import pytest

from ml.gameplan_archive_integration import combine_archive_sources, exclude_quality_intervals, validate_archive_feature_clocks
from ml.gameplan_source_selection import ARCHIVE_SOURCE_SELECTION_VERSION, source_selection_contract


@dataclass(frozen=True)
class Archive:
    sources: pd.DataFrame
    feature_columns: tuple
    report: dict


def test_archive_history_keeps_old_rows_and_attaches_only_causal_optional_features():
    base = pd.DataFrame({"symbol": ["AAPL"] * 3,
        "action_date": ["2019-10-01", "2026-09-21", "2026-09-22"],
        "decision_timestamp": pd.to_datetime(["2019-10-01T01:05Z", "2026-09-21T01:05Z", "2026-09-22T01:05Z"]),
        "arch__return": [0.1, 0.2, 0.3],
        "source_selection_contract": ARCHIVE_SOURCE_SELECTION_VERSION})
    operational = pd.DataFrame({"symbol": ["AAPL"] * 2,
        "action_date": ["2026-09-21", "2026-09-22"],
        "information_available_at": pd.to_datetime(["2026-09-21T01:00Z", "2026-09-22T12:00Z"]),
        "old__feature": [5.0, 99.0]})
    archive = Archive(base, ("arch__return",), {"source_selection_contract": ARCHIVE_SOURCE_SELECTION_VERSION})
    result = combine_archive_sources(archive, operational, feature_columns=("old__feature",))
    assert len(result.sources) == 3
    assert result.sources["arch__return"].tolist() == [0.1, 0.2, 0.3]
    assert result.sources["old__feature"].isna().tolist() == [True, False, True]
    assert result.sources.iloc[1]["old__feature"] == 5.0
    assert result.report["operational_rows_excluded_as_future"] == 1
    assert source_selection_contract(result.sources) == ARCHIVE_SOURCE_SELECTION_VERSION
    pd.testing.assert_frame_equal(archive.sources, base)
    with pytest.raises(ValueError, match="not unique"):
        combine_archive_sources(archive, pd.concat([operational, operational]), feature_columns=("old__feature",))
    operational.loc[0, "information_available_at"] = pd.NaT
    unknown = combine_archive_sources(archive, operational, feature_columns=("old__feature",))
    assert unknown.sources["old__feature"].isna().all()
    assert unknown.report["operational_rows_excluded_unknown_availability"] == 1


def test_target_windows_reject_degraded_observations_and_split_crossings_only():
    frame = pd.DataFrame({"symbol": ["AAPL", "AAPL", "AAPL", "MU"],
        "target_window_start": ["2020-08-28T11:00Z", "2020-08-31T11:00Z", "2020-09-01T11:00Z", "2020-08-28T11:00Z"],
        "target_window_end": ["2020-09-01T00:00Z", "2020-09-01T00:00Z", "2020-09-02T00:00Z", "2020-09-01T00:00Z"]})
    frame.attrs["target_boundary_quality"] = {"retained": True}
    out = exclude_quality_intervals({"1w": frame}, [{"symbol": "AAPL", "start": "2020-09-01T17:00Z", "end": "2020-09-01T17:01Z"}],
        split_boundaries=[{"symbol": "AAPL", "at": "2020-08-31T07:00Z"}])["1w"]
    assert out.index.tolist() == [1, 3]
    assert out.attrs["archive_quality_excluded_rows"] == 2
    assert out.attrs["target_boundary_quality"] == frame.attrs["target_boundary_quality"]
    with pytest.raises(ValueError, match="timezone aware"):
        exclude_quality_intervals({"1w": frame}, (), split_boundaries=[{"symbol": "AAPL", "at": "2020-08-31"}])


def test_reader_rejects_future_or_incomplete_daily_feature_clocks():
    frame = pd.DataFrame({"action_date": ["2026-09-23"],
        "source_bar_timestamp": pd.to_datetime(["2026-09-22T00:00Z"]),
        "source_bar_end_timestamp": pd.to_datetime(["2026-09-23T00:00Z"]),
        "information_available_at": pd.to_datetime(["2026-09-23T00:05Z"]),
        "decision_timestamp": pd.to_datetime(["2026-09-23T00:05Z"]),
        "source_effective_cutoff": pd.to_datetime(["2026-09-23T00:05Z"]),
        "source_feature_cutoff": pd.to_datetime(["2026-09-23T00:05Z"]),
        "source_action_start": pd.to_datetime(["2026-09-23T11:00Z"])})
    validate_archive_feature_clocks(frame)
    for name, value in (("information_available_at", "2026-09-22T23:00Z"),
                        ("decision_timestamp", "2026-09-23T11:01Z"),
                        ("source_bar_timestamp", "2026-09-22T23:00Z"),
                        ("source_action_start", "2026-09-24T11:00Z")):
        bad = frame.copy()
        bad[name] = pd.to_datetime([value])
        with pytest.raises(RuntimeError, match="causal daily contract"):
            validate_archive_feature_clocks(bad)
