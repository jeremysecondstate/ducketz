from pathlib import Path

import pandas as pd
import pytest

import datafetching.fred_alfred_readiness as readiness


@pytest.mark.parametrize("mode", ["backfill", "incremental"])
@pytest.mark.parametrize(
    ("as_of", "expected_date"),
    [
        ("2026-10-14T04:30:00Z", "2026-10-13"),
        ("2026-06-02T04:59:59Z", "2026-06-01"),
        ("2026-06-02T05:00:00Z", "2026-06-02"),
        ("2026-01-02T05:59:59Z", "2026-01-01"),
        ("2026-01-02T06:00:00Z", "2026-01-02"),
        ("2026-03-09T04:59:59Z", "2026-03-08"),
        ("2026-11-02T05:59:59Z", "2026-11-01"),
        ("2026-10-14T12:00:00Z", "2026-10-14"),
    ],
)
def test_provider_bounds_follow_central_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    as_of: str,
    expected_date: str,
) -> None:
    """UTC rollover cannot send tomorrow to FRED, including DST boundaries."""
    observed = pd.Timestamp(as_of)
    decision = observed - pd.Timedelta(days=365)
    decisions = pd.DataFrame({"decision_timestamp": [decision]})
    source = tmp_path / "samples.parquet"
    monkeypatch.setattr(
        readiness, "_selected_decisions", lambda *_args: (source, decisions)
    )
    vintages = pd.DataFrame(
        {
            "series_name": readiness.FRED_ALFRED_SUPPORTED_SERIES,
            "realtime_start": [observed - pd.Timedelta(days=2)] * 4,
            "realtime_end": [observed.date().isoformat()] * 4,
            "fetched_at": [observed - pd.Timedelta(days=1)] * 4,
        }
    )
    monkeypatch.setattr(
        readiness, "read_persisted_fred_vintages", lambda *_args, **_kwargs: (vintages, ())
    )

    derive = getattr(readiness, f"derive_fred_alfred_{mode}_plan")
    plan = derive(tmp_path, as_of=observed)

    assert plan.as_request()["realtime_end"] == expected_date
    assert plan.as_request()["observation_end"] == expected_date
    assert plan.earliest_eligible_decision == decision
    assert plan.decision_source == source
    assert plan.realtime_start <= plan.realtime_end
    assert plan.observation_start <= plan.observation_end
