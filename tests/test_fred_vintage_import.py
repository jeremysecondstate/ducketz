from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

from datafetching.fred_vintage_import import (
    FRED_ALFRED_MANIFEST_NAME,
    FRED_ALFRED_RAW_NAME,
    FredAlfredClient,
    FredVintageImportError,
    import_fred_alfred_vintages,
    read_fred_alfred_vintage_import,
)
from ml import option_pricing_fred
from ml.option_pricing.rates import (
    load_point_in_time_rate_observations,
    rate_coverage_report,
)

_TEST_KEY = "a" * 32


class _Response:
    def __init__(self, payload: Mapping[str, object], status_code: int = 200) -> None:
        self._payload = dict(payload)
        self.status_code = status_code

    def json(self) -> Mapping[str, object]:
        return self._payload


class _FredSession:
    def __init__(self, *, current_revised: bool = False) -> None:
        self.current_revised = current_revised
        self.calls: list[tuple[str, Mapping[str, object], float]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        timeout: float,
    ) -> _Response:
        self.calls.append((url, dict(params), timeout))
        endpoint = url.removeprefix(
            "https://api.stlouisfed.org/fred/"
        )
        if endpoint == "series":
            return _Response(
                {
                    "seriess": [
                        {
                            "id": "FEDFUNDS",
                            "frequency": "Monthly",
                            "units": "Percent",
                            "last_updated": "2026-08-03 15:16:01-05",
                        }
                    ]
                }
            )
        if endpoint == "series/vintagedates":
            values = ["2026-02-05", "2026-03-05"]
            return _Response(_page("vintage_dates", values, params))
        if endpoint == "series/observations":
            observations: list[dict[str, object]] = [
                {
                    "date": "2026-01-01",
                    "realtime_start": "2026-02-05",
                    "realtime_end": "2026-03-04",
                    "value": "3.64",
                },
                {
                    "date": "2026-02-01",
                    "realtime_start": "2026-03-05",
                    "realtime_end": "9999-12-31",
                    "value": "3.62",
                },
            ]
            if self.current_revised:
                observations[0].pop("realtime_start")
                observations[0].pop("realtime_end")
            return _Response(
                {
                    **_page("observations", observations, params),
                    "output_type": 1,
                }
            )
        raise AssertionError(endpoint)


def test_alfred_import_separates_provider_and_local_clocks_and_proves_rate(
    tmp_path: Path,
) -> None:
    session = _FredSession()
    result = import_fred_alfred_vintages(
        tmp_path,
        client=FredAlfredClient(_TEST_KEY, session=session, sleeper=lambda _: None),
        series_ids=("FEDFUNDS",),
        realtime_start="2026-02-01",
        realtime_end="2026-08-10",
        observation_start="2025-12-01",
        observation_end="2026-08-10",
        acquired_at="2026-08-11T04:30:00Z",
    )

    assert result.row_count == 2
    assert result.series_count == 1
    assert result.vintage_partition_paths
    assert result.rate_feature_paths
    verified = read_fred_alfred_vintage_import(
        result.evidence_directory,
        datastore_root=tmp_path,
    )
    vintages = verified["vintages"]
    assert isinstance(vintages, pd.DataFrame)
    first = vintages.sort_values("realtime_start").iloc[0]
    assert first["release_time_precision"] == "DATE"
    assert first["release_at"] == pd.Timestamp("2026-02-06T06:00:00Z")
    assert first["available_at"] == first["release_at"]
    assert first["fetched_at"] == pd.Timestamp("2026-08-11T04:30:00Z")
    assert first["available_at"] < first["fetched_at"]

    observations, paths = load_point_in_time_rate_observations(tmp_path)
    assert paths
    coverage = rate_coverage_report(
        observations,
        target_snapshot_fors=("2026-02-10T13:45:00Z",),
    )
    assert coverage["status"] == "PASS"
    assert coverage["covered_target_count"] == 1

    manifest = json.loads(
        (result.evidence_directory / FRED_ALFRED_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["current_revised_history_used"] is False
    assert manifest["historical_coverage_status"] == "NOT_EVALUATED"
    assert manifest["provider_output_type"] == 1
    assert all(
        _TEST_KEY.encode("utf-8") not in path.read_bytes()
        for path in result.evidence_directory.iterdir()
        if path.is_file()
    )
    assert session.calls
    assert all(call[1]["api_key"] == _TEST_KEY for call in session.calls)


def test_current_revised_payload_cannot_be_imported_as_alfred_history(
    tmp_path: Path,
) -> None:
    with pytest.raises(FredVintageImportError, match="Current-revised"):
        import_fred_alfred_vintages(
            tmp_path,
            client=FredAlfredClient(
                _TEST_KEY,
                session=_FredSession(current_revised=True),
                sleeper=lambda _: None,
            ),
            series_ids=("FEDFUNDS",),
            realtime_start="2026-02-01",
            realtime_end="2026-08-10",
            observation_start="2025-12-01",
            observation_end="2026-08-10",
            acquired_at="2026-08-11T04:30:00Z",
        )
    assert not list(tmp_path.glob("ml/option-pricing-evidence/**/*"))


def test_fred_client_errors_and_immutable_artifacts_never_expose_key(
    tmp_path: Path,
) -> None:
    class _FailingSession:
        def get(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError(f"request failed?api_key={_TEST_KEY}")

    client = FredAlfredClient(
        _TEST_KEY,
        session=_FailingSession(),  # type: ignore[arg-type]
        maximum_attempts=1,
        sleeper=lambda _: None,
    )
    with pytest.raises(FredVintageImportError) as error:
        client.get_json("series", params={"series_id": "FEDFUNDS"})
    assert _TEST_KEY not in str(error.value)

    result = import_fred_alfred_vintages(
        tmp_path,
        client=FredAlfredClient(
            _TEST_KEY,
            session=_FredSession(),
            sleeper=lambda _: None,
        ),
        series_ids=("FEDFUNDS",),
        realtime_start="2026-02-01",
        realtime_end="2026-08-10",
        observation_start="2025-12-01",
        observation_end="2026-08-10",
        acquired_at="2026-08-11T04:30:00Z",
    )
    raw = result.evidence_directory / FRED_ALFRED_RAW_NAME
    raw.write_text(raw.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(FredVintageImportError, match="checksum mismatch"):
        read_fred_alfred_vintage_import(
            result.evidence_directory,
            datastore_root=tmp_path,
        )


def test_fred_import_cli_requires_environment_key_without_printing_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(option_pricing_fred, "load_repository_environment", lambda: False)
    with pytest.raises(SystemExit) as error:
        option_pricing_fred.main(
            [
                "--datastore",
                str(tmp_path),
                "--realtime-start",
                "2026-02-01",
                "--realtime-end",
                "2026-08-10",
                "--observation-start",
                "2025-12-01",
                "--observation-end",
                "2026-08-10",
            ]
        )
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "FRED_API_KEY is required" in stderr
    assert _TEST_KEY not in stderr


def _page(
    collection: str,
    values: list[object],
    params: Mapping[str, object],
) -> Mapping[str, object]:
    offset = int(params["offset"])
    limit = int(params["limit"])
    return {
        "count": len(values),
        "offset": offset,
        "limit": limit,
        collection: values[offset : offset + limit],
    }
