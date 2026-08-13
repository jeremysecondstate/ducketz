from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from pathlib import Path

import pandas as pd
import pytest

from datafetching import fred_fetch
from datafetching.fred_fetch import FredSeriesSpec, StaleFredSeriesError
from datafetching.parquet_store import ParquetStore


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _FrozenDateTime:
        value = cls(2026, 7, 30, 6, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


class _CsvResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_fred_series_spec_accepts_legacy_freshness_limit_keyword() -> None:
    spec = FredSeriesSpec(
        output_symbol="legacy",
        series_id="LEGACY",
        label="Legacy test series",
        source_agency="Test agency",
        cadence="monthly",
        unit="index",
        freshness_limit_days=90,
    )

    assert spec.fetch_liveness_limit_days == 90
    assert spec.freshness_limit_days == 90


def test_current_period_dated_fred_set_returns_no_provider_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest_by_series = {
        "GDP": date(2026, 1, 1),
        "CPIAUCSL": date(2026, 6, 1),
        "UNRATE": date(2026, 6, 1),
        "FEDFUNDS": date(2026, 6, 1),
    }

    def get_csv(
        _url: str,
        *,
        params: dict[str, str],
        timeout: int,
    ) -> _CsvResponse:
        assert timeout == 30
        series_id = params["id"]
        return _CsvResponse(
            f"observation_date,{series_id}\n"
            f"{latest_by_series[series_id].isoformat()},1.0\n"
        )

    monkeypatch.setattr(fred_fetch, "datetime", _FrozenDateTime)
    monkeypatch.setattr(fred_fetch.requests, "get", get_csv)

    result = fred_fetch.fetch("IGNORED", ParquetStore(tmp_path))

    assert result.error_files == 0
    assert result.data_files == 9
    assert tuple(tmp_path.glob("**/errors/fred/macro/*.parquet")) == ()
    rate_paths = tuple(
        tmp_path.glob(
            "pools/macro/features/prospective-release-context/fred/*.parquet"
        )
    )
    assert len(rate_paths) == 1
    rate = pd.read_parquet(rate_paths[0]).iloc[-1]
    assert rate["fed_funds_available_at"] == pd.Timestamp(
        "2026-07-30T06:00:00Z"
    )
    assert rate["macro__fed_funds_level"] == 1.0


@pytest.mark.parametrize(
    ("series_id", "latest_observation", "expected_age_days"),
    (
        ("GDP", date(2026, 1, 1), 210),
        ("CPIAUCSL", date(2026, 6, 1), 59),
        ("UNRATE", date(2026, 6, 1), 59),
        ("FEDFUNDS", date(2026, 6, 1), 59),
    ),
)
def test_period_dated_current_fred_series_pass_fetch_liveness(
    monkeypatch: pytest.MonkeyPatch,
    series_id: str,
    latest_observation: date,
    expected_age_days: int,
) -> None:
    spec = next(spec for spec in fred_fetch.FRED_SERIES if spec.series_id == series_id)
    _mock_csv(monkeypatch, spec, latest_observation)

    rows, _raw_csv, latest_date, age_days = fred_fetch.fetch_series(spec)

    assert latest_date == latest_observation
    assert age_days == expected_age_days
    assert rows[-1]["fetch_liveness_limit_days"] == spec.fetch_liveness_limit_days
    assert rows[-1]["freshness_limit_days"] == spec.fetch_liveness_limit_days
    assert spec.freshness_limit_days == spec.fetch_liveness_limit_days


@pytest.mark.parametrize(
    ("series_id", "latest_observation", "expected_age_days", "expected_limit_days"),
    (
        ("GDP", date(2025, 10, 1), 302, 240),
        ("CPIAUCSL", date(2026, 4, 1), 120, 90),
    ),
)
def test_truly_stale_fred_series_fail_fetch_liveness(
    monkeypatch: pytest.MonkeyPatch,
    series_id: str,
    latest_observation: date,
    expected_age_days: int,
    expected_limit_days: int,
) -> None:
    spec = next(spec for spec in fred_fetch.FRED_SERIES if spec.series_id == series_id)
    _mock_csv(monkeypatch, spec, latest_observation)

    with pytest.raises(StaleFredSeriesError) as captured:
        fred_fetch.fetch_series(spec)

    assert captured.value.latest_observation_date == latest_observation
    assert captured.value.age_days == expected_age_days
    assert captured.value.limit_days == expected_limit_days


def test_stale_fred_error_persists_structured_liveness_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = FredSeriesSpec(
        output_symbol="staleMonthly",
        series_id="STALEMONTHLY",
        label="Stale monthly test series",
        source_agency="Test agency",
        cadence="monthly",
        unit="index",
        fetch_liveness_limit_days=90,
    )
    monkeypatch.setattr(fred_fetch, "FRED_SERIES", (spec,))
    _mock_csv(monkeypatch, spec, date(2026, 4, 1))

    result = fred_fetch.fetch("IGNORED", ParquetStore(tmp_path))

    assert result.error_files == 1
    assert result.data_files == 0
    error_paths = tuple(tmp_path.glob("**/errors/fred/macro/*.parquet"))
    assert len(error_paths) == 1
    error = pd.read_parquet(error_paths[0]).iloc[0]
    assert error["latest_observation_date"] == "2026-04-01"
    assert error["fetch_liveness_age_days"] == 120
    assert error["fetch_liveness_status"] == "STALE"
    assert error["fetch_liveness_limit_days"] == 90
    assert error["freshness_age_days"] == 120
    assert error["freshness_status"] == "STALE"
    assert error["freshness_limit_days"] == 90


def _mock_csv(
    monkeypatch: pytest.MonkeyPatch,
    spec: FredSeriesSpec,
    latest_observation: date,
) -> None:
    csv_text = (
        f"observation_date,{spec.series_id}\n"
        f"{latest_observation.isoformat()},1.0\n"
    )
    monkeypatch.setattr(fred_fetch, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        fred_fetch.requests,
        "get",
        lambda *_args, **_kwargs: _CsvResponse(csv_text),
    )
