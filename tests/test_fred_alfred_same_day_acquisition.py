from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

from datafetching.fred_alfred_readiness import (
    FredAlfredReadinessError,
    _validate_vintage_integrity,
)
from datafetching.fred_vintage_import import (
    FRED_ALFRED_RAW_NAME,
    FRED_ALFRED_SUPPORTED_SERIES,
    FredAlfredClient,
    import_fred_alfred_vintages,
    read_fred_alfred_vintage_import,
)
from datafetching.fred_vintages import (
    ALFRED_VINTAGE_AVAILABILITY_BASIS,
    alfred_provider_available_at,
    derive_macro_release_features,
    normalize_fred_vintage_rows,
)
from ml.contracts import MLContractError
from ml.datasets.families import load_macro_features


class _SameDaySession:
    def get(
        self, url: str, *, params: Mapping[str, object], timeout: float
    ) -> object:
        del timeout
        endpoint = url.removeprefix("https://api.stlouisfed.org/fred/")
        series = str(params["series_id"])
        if endpoint == "series":
            payload = {"seriess": [{"id": series, "units": "Percent"}]}
        elif endpoint == "series/vintagedates":
            payload = {
                "count": 2,
                "vintage_dates": ["2026-10-13", "2026-10-14"],
            }
        else:
            assert endpoint == "series/observations"
            payload = {
                "count": 2,
                "output_type": 1,
                "observations": [
                    {
                        "date": "2026-09-01",
                        "realtime_start": "2026-10-13",
                        "realtime_end": "2026-10-13",
                        "value": "4.0",
                    },
                    {
                        "date": "2026-09-01",
                        "realtime_start": "2026-10-14",
                        "realtime_end": "2026-10-14",
                        "value": "5.0",
                    },
                ],
            }

        class Response:
            status_code = 200

            def json(self) -> Mapping[str, object]:
                return payload

        return Response()


def test_same_day_import_preserves_receipt_and_defers_model_visibility(
    tmp_path: Path,
) -> None:
    acquired = pd.Timestamp("2026-10-15T04:02:00Z")
    result = import_fred_alfred_vintages(
        tmp_path,
        client=FredAlfredClient("a" * 32, session=_SameDaySession()),
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        realtime_start="2026-10-13",
        realtime_end="2026-10-14",
        observation_start="2026-09-01",
        observation_end="2026-10-14",
        acquired_at=acquired,
    )
    verified = read_fred_alfred_vintage_import(
        result.evidence_directory, datastore_root=tmp_path
    )
    vintages = verified["vintages"]
    assert isinstance(vintages, pd.DataFrame)
    assert result.row_count == 8
    assert vintages["fetched_at"].eq(acquired).all()
    today = vintages.loc[
        vintages["realtime_start"].eq(pd.Timestamp("2026-10-14T00:00:00Z"))
    ]
    assert len(today) == 4
    assert today["available_at"].eq(pd.Timestamp("2026-10-15T05:00:00Z")).all()
    raw = json.loads((result.evidence_directory / FRED_ALFRED_RAW_NAME).read_text())
    assert pd.Timestamp(raw["acquired_at"]) == acquired
    raw_rows = raw["series"]["FEDFUNDS"]["observation_pages"][0]["observations"]
    assert raw_rows[1]["realtime_start"] == "2026-10-14"
    _validate_vintage_integrity(vintages)

    decisions = pd.DataFrame(
        {
            "decision_timestamp": pd.to_datetime(
                ["2026-10-15T04:59:59.999999999Z", "2026-10-15T05:00:00Z"],
                utc=True,
                format="ISO8601",
            )
        }
    )
    joined = load_macro_features(
        decisions,
        derive_macro_release_features(vintages),
        vintage_source=vintages,
        value_columns={"macro__fed_funds_level": "macro__fed_funds_level"},
    )
    assert joined["macro__fed_funds_level"].tolist() == [4.0, 5.0]
    assert (
        joined["macro__fed_funds_level__available_at"]
        <= decisions["decision_timestamp"]
    ).all()


@pytest.mark.parametrize(
    ("provider_date", "provider_midnight", "available_at"),
    [
        ("2026-10-14", "2026-10-14T05:00:00Z", "2026-10-15T05:00:00Z"),
        ("2026-01-14", "2026-01-14T06:00:00Z", "2026-01-15T06:00:00Z"),
        ("2026-03-08", "2026-03-08T06:00:00Z", "2026-03-09T05:00:00Z"),
        ("2026-11-01", "2026-11-01T05:00:00Z", "2026-11-02T06:00:00Z"),
    ],
)
def test_provider_date_boundary_rejects_future_vintages_in_every_consumer(
    provider_date: str, provider_midnight: str, available_at: str
) -> None:
    acquired = pd.Timestamp(provider_midnight)
    rows = [
        {
            "series_name": series,
            "observation_date": "2025-12-01",
            "realtime_start": provider_date,
            "realtime_end": provider_date,
            "release_at": alfred_provider_available_at(provider_date),
            "release_time_precision": "DATE",
            "fetched_at": acquired,
            "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
            "value": 5.0,
        }
        for series in FRED_ALFRED_SUPPORTED_SERIES
    ]
    vintages = normalize_fred_vintage_rows(rows)
    assert vintages["fetched_at"].eq(acquired).all()
    assert vintages["available_at"].eq(pd.Timestamp(available_at)).all()
    _validate_vintage_integrity(vintages)
    context = derive_macro_release_features(vintages)
    invalid = vintages.copy()
    invalid["fetched_at"] = acquired - pd.Timedelta(nanoseconds=1)
    with pytest.raises(ValueError, match="on or after their provider date"):
        normalize_fred_vintage_rows(invalid)
    with pytest.raises(FredAlfredReadinessError, match="precedes its provider date"):
        _validate_vintage_integrity(invalid)
    with pytest.raises(MLContractError, match="precedes provider date"):
        load_macro_features(
            pd.DataFrame({"decision_timestamp": [pd.Timestamp(available_at)]}),
            context,
            vintage_source=invalid,
            value_columns={"macro__fed_funds_level": "macro__fed_funds_level"},
        )
