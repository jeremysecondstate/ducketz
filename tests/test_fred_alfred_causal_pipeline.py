from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd
import pytest

import datafetching.fred_alfred_readiness as readiness_module
import datafetching.fred_alfred_runtime as runtime_module
from datafetching.fred_alfred_readiness import (
    FRED_ALFRED_MODEL_HORIZONS,
    derive_fred_alfred_backfill_plan,
    derive_fred_alfred_incremental_plan,
    read_verified_macro_evidence,
    verify_and_publish_fred_alfred_readiness,
)
from datafetching.fred_vintage_import import (
    FRED_ALFRED_SUPPORTED_SERIES,
    FredAlfredClient,
    FredVintageImportResult,
    import_fred_alfred_vintages,
)
from datafetching.fred_vintages import (
    ALFRED_VINTAGE_AVAILABILITY_BASIS,
    LOCAL_RECEIPT_AVAILABILITY_BASIS,
    alfred_provider_available_at,
    derive_macro_release_features,
    normalize_fred_vintage_rows,
    persist_fred_vintages,
    persist_macro_release_features,
    read_persisted_fred_vintages,
)
from ml.contracts import MLContractError
from ml.datasets.families import MACRO_VALUES, load_macro_features


_TEST_KEY = "b" * 32


class _Response:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.status_code = 200
        self._payload = dict(payload)

    def json(self) -> Mapping[str, object]:
        return self._payload


class _CompleteFredSession:
    def __init__(self) -> None:
        self.rows = _provider_rows()
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        timeout: float,
    ) -> _Response:
        del timeout
        endpoint = url.removeprefix("https://api.stlouisfed.org/fred/")
        self.calls.append((endpoint, dict(params)))
        series = str(params["series_id"])
        if endpoint == "series":
            return _Response(
                {
                    "seriess": [
                        {
                            "id": series,
                            "frequency": (
                                "Quarterly" if series == "GDP" else "Monthly"
                            ),
                            "units": "Index" if series == "CPIAUCSL" else "Percent",
                            "last_updated": "2024-05-01 00:00:00-05",
                        }
                    ]
                }
            )
        values = [row for row in self.rows if row["series_name"] == series]
        if endpoint == "series/vintagedates":
            dates = sorted({str(row["realtime_start"]) for row in values})
            return _Response(_page("vintage_dates", dates, params))
        if endpoint == "series/observations":
            request_start = pd.Timestamp(str(params["realtime_start"])).date()
            request_end = pd.Timestamp(str(params["realtime_end"])).date()
            observations = [
                {
                    "date": row["observation_date"],
                    # ALFRED output_type=1 clips active intervals to the
                    # requested real-time bounds.
                    "realtime_start": max(
                        pd.Timestamp(str(row["realtime_start"])).date(),
                        request_start,
                    ).isoformat(),
                    "realtime_end": min(
                        pd.Timestamp(str(row["realtime_end"])).date(),
                        request_end,
                    ).isoformat(),
                    "value": str(row["value"]),
                }
                for row in values
                if pd.Timestamp(str(row["realtime_end"])).date() >= request_start
                and pd.Timestamp(str(row["realtime_start"])).date() <= request_end
            ]
            return _Response(
                {
                    **_page("observations", observations, params),
                    "output_type": 1,
                }
            )
        raise AssertionError(endpoint)


def test_complete_alfred_context_is_causal_revision_aware_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _CompleteFredSession()
    client = FredAlfredClient(_TEST_KEY, session=session, sleeper=lambda _: None)
    first_import = import_fred_alfred_vintages(
        tmp_path,
        client=client,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        realtime_start="2023-01-01",
        realtime_end="2024-05-31",
        observation_start="2022-01-01",
        observation_end="2024-05-31",
        acquired_at="2024-06-01T12:00:00Z",
    )
    assert first_import.series_count == 4
    assert first_import.release_feature_paths
    assert all(
        "alfred-release-context" in path.parts
        for path in first_import.release_feature_paths
    )

    vintages, vintage_paths = read_persisted_fred_vintages(tmp_path)
    release = derive_macro_release_features(vintages)
    decisions = _decisions(
        "2024-02-15T05:59:59.999999999Z",
        "2024-02-15T06:00:00Z",
        "2024-03-21T05:00:00Z",
    )
    joined = load_macro_features(
        decisions,
        release,
        value_columns=MACRO_VALUES,
        freshness=None,
        vintage_source=vintages,
    )
    assert pd.isna(joined.loc[0, "macro__cpi_yoy"])
    assert joined.loc[1, "macro__cpi_yoy"] == pytest.approx(0.10)
    assert joined.loc[2, "macro__cpi_yoy"] == pytest.approx(0.12)
    assert joined.loc[1, "macro__unemployment_change"] == pytest.approx(0.3)
    assert joined.loc[2, "macro__unemployment_change"] == pytest.approx(-0.1)
    assert joined.loc[1, "macro__gdp_yoy"] == pytest.approx(0.05)
    assert joined.loc[2, "macro__fed_funds_level"] == pytest.approx(5.5)
    assert joined.loc[2, "macro__cpi_yoy__available_at"] == pd.Timestamp(
        "2024-03-21T05:00:00Z"
    )
    assert joined.loc[2, "macro__gdp_yoy__available_at"] == pd.Timestamp(
        "2024-01-26T06:00:00Z"
    )
    assert (
        joined.loc[1:, "macro__cpi_yoy__available_at"]
        <= joined.loc[1:, "decision_timestamp"]
    ).all()
    unemployment_boundary = pd.Timestamp("2024-03-08T06:00:00Z") + pd.Timedelta(
        days=56
    )
    unemployment_freshness = load_macro_features(
        pd.DataFrame(
            {
                "decision_timestamp": [
                    unemployment_boundary,
                    unemployment_boundary + pd.Timedelta(nanoseconds=1),
                ]
            }
        ),
        release,
        value_columns={
            "macro__unemployment_change": "macro__unemployment_change"
        },
        freshness=None,
        vintage_source=vintages,
    )
    assert unemployment_freshness.loc[
        0, "macro__unemployment_change"
    ] == pytest.approx(-0.1)
    assert pd.isna(
        unemployment_freshness.loc[1, "macro__unemployment_change"]
    )

    first_receipts = vintages.set_index("revision_identity")["fetched_at"].copy()
    second_import = import_fred_alfred_vintages(
        tmp_path,
        client=client,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        realtime_start="2023-01-01",
        realtime_end="2024-05-31",
        observation_start="2022-01-01",
        observation_end="2024-05-31",
        acquired_at="2024-06-02T12:00:00Z",
    )
    replayed, replay_paths = read_persisted_fred_vintages(tmp_path)
    assert replay_paths == vintage_paths
    assert len(replayed) == len(vintages)
    assert not replayed["revision_identity"].duplicated().any()
    pd.testing.assert_series_equal(
        replayed.set_index("revision_identity")["fetched_at"].sort_index(),
        first_receipts.sort_index(),
    )
    for path in second_import.release_feature_paths:
        stored = pd.read_parquet(path)
        assert not stored.duplicated(
            ["context_name", "available_at", "calculation_version"]
        ).any()

    boundary_import = import_fred_alfred_vintages(
        tmp_path,
        client=client,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        realtime_start="2024-03-10",
        realtime_end="2024-06-01",
        observation_start="2022-01-01",
        observation_end="2024-06-01",
        acquired_at="2024-06-02T12:00:00Z",
    )
    boundary_manifest = (
        boundary_import.evidence_directory / "manifest.json"
    ).read_text(encoding="utf-8")
    assert '"request_boundary_realtime_start_reconstruction_count": 9' in (
        boundary_manifest
    )
    boundary_release = pd.concat(
        [
            pd.read_parquet(path).drop(columns="id")
            for path in boundary_import.release_feature_paths
        ],
        ignore_index=True,
    )
    false_boundary = alfred_provider_available_at("2024-03-10")
    assert not boundary_release["available_at"].eq(false_boundary).any()
    before_boundary_rerun, _ = read_persisted_fred_vintages(tmp_path)
    import_fred_alfred_vintages(
        tmp_path,
        client=client,
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        realtime_start="2024-03-10",
        realtime_end="2024-06-01",
        observation_start="2022-01-01",
        observation_end="2024-06-01",
        acquired_at="2024-06-03T12:00:00Z",
    )
    after_boundary_rerun, _ = read_persisted_fred_vintages(tmp_path)
    assert len(after_boundary_rerun) == len(before_boundary_rerun)

    decision_source = tmp_path / "eligible-decisions.parquet"
    coverage_decisions = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "horizon": horizon,
                "decision_timestamp": decision_at,
            }
            for horizon in FRED_ALFRED_MODEL_HORIZONS
            for decision_at in pd.to_datetime(
                ["2024-02-15T06:00:00Z", "2024-03-21T05:00:00Z"],
                utc=True,
            )
        ]
    )
    coverage_decisions.to_parquet(decision_source, index=False)
    monkeypatch.setattr(
        readiness_module,
        "_eligible_decisions",
        lambda _root: (decision_source, coverage_decisions.copy()),
    )
    readiness = verify_and_publish_fred_alfred_readiness(
        tmp_path,
        import_result=boundary_import,
        verified_at="2024-06-02T12:01:00Z",
    )
    assert readiness.coverage["status"] == "PASS"
    assert readiness.coverage["lookahead_violation_count"] == 0
    evidence = read_verified_macro_evidence(tmp_path)
    assert len(evidence.vintages) == len(before_boundary_rerun)
    assert set(evidence.vintages["series_name"]) == set(
        FRED_ALFRED_SUPPORTED_SERIES
    )
    assert all(
        feature["coverage"] == 1.0
        for horizon in readiness.coverage["horizons"].values()
        for feature in horizon["features"].values()
    )

    assert all(
        _TEST_KEY.encode("utf-8") not in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_backfill_and_incremental_bounds_come_from_decisions_lags_and_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = tmp_path / "samples.parquet"
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"] * len(FRED_ALFRED_MODEL_HORIZONS),
            "horizon": list(FRED_ALFRED_MODEL_HORIZONS),
            "decision_timestamp": pd.to_datetime(
                ["2023-06-07T20:05:00Z"] * len(FRED_ALFRED_MODEL_HORIZONS),
                utc=True,
            ),
        }
    )
    decisions.to_parquet(samples, index=False)
    monkeypatch.setattr(
        readiness_module,
        "resolve_current_output",
        lambda _root, _name: samples,
    )
    backfill = derive_fred_alfred_backfill_plan(
        tmp_path,
        as_of="2024-06-01T12:00:00Z",
    )
    assert backfill.observation_start.isoformat() == "2022-01-01"
    assert backfill.realtime_start == backfill.observation_start
    assert backfill.earliest_eligible_decision == pd.Timestamp(
        "2023-06-07T20:05:00Z"
    )

    normalized = normalize_fred_vintage_rows(_normalized_rows())
    persist_fred_vintages(tmp_path, normalized)
    incremental = derive_fred_alfred_incremental_plan(
        tmp_path,
        as_of="2024-06-01T12:00:00Z",
    )
    assert incremental.mode == "INCREMENTAL"
    assert incremental.realtime_start >= pd.Timestamp(
        "2024-06-01"
    ).date() - pd.Timedelta(days=130)
    assert incremental.observation_start == backfill.observation_start


def test_current_revised_receipt_basis_is_rejected_as_historical_evidence() -> None:
    vintages = normalize_fred_vintage_rows(_normalized_rows())
    release = derive_macro_release_features(vintages)
    current = vintages.loc[vintages["series_name"].eq("CPIAUCSL")].head(2).copy()
    current["availability_basis"] = LOCAL_RECEIPT_AVAILABILITY_BASIS
    current["available_at"] = current["fetched_at"]
    with pytest.raises(MLContractError, match="Current-revised or local-receipt"):
        load_macro_features(
            _decisions("2024-03-21T05:00:00Z"),
            release,
            value_columns={"macro__cpi_yoy": "macro__cpi_yoy"},
            vintage_source=current,
        )


def test_daily_owner_skips_a_second_successful_run_on_the_same_utc_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_directory = (
        tmp_path
        / "ml"
        / "option-pricing-evidence"
        / "fred-alfred-vintages"
        / "20240602T120000.000000Z"
    )
    import_directory.mkdir(parents=True)
    (import_directory / "receipt.json").write_text("{}\n", encoding="utf-8")
    readiness_directory = (
        tmp_path / "ml" / "macro-readiness" / "fred-alfred" / "run"
    )
    readiness_directory.mkdir(parents=True)
    readiness_report = readiness_directory / "readiness.json"
    readiness_receipt = readiness_directory / "receipt.json"
    readiness_report.write_text("{}\n", encoding="utf-8")
    readiness_receipt.write_text("{}\n", encoding="utf-8")
    fake_import = FredVintageImportResult(
        evidence_directory=import_directory,
        row_count=10,
        series_count=4,
        vintage_partition_paths=(),
        release_feature_paths=(),
    )
    fake_readiness = readiness_module.FredAlfredReadiness(
        directory=readiness_directory,
        report_path=readiness_report,
        receipt_path=readiness_receipt,
        verified_at=pd.Timestamp("2024-06-02T12:00:00Z"),
        coverage={"status": "PASS"},
    )
    plan = readiness_module.FredAlfredRequestPlan(
        mode="INCREMENTAL",
        realtime_start=pd.Timestamp("2024-01-24").date(),
        realtime_end=pd.Timestamp("2024-06-02").date(),
        observation_start=pd.Timestamp("2022-01-01").date(),
        observation_end=pd.Timestamp("2024-06-02").date(),
        earliest_eligible_decision=pd.Timestamp("2023-06-07T20:05:00Z"),
        decision_source=tmp_path / "samples.parquet",
    )
    calls = {"import": 0, "verify": 0}
    monkeypatch.setattr(
        runtime_module,
        "derive_fred_alfred_incremental_plan",
        lambda *_args, **_kwargs: plan,
    )

    def fake_importer(*_args: object, **_kwargs: object) -> FredVintageImportResult:
        calls["import"] += 1
        return fake_import

    def fake_verifier(*_args: object, **_kwargs: object):
        calls["verify"] += 1
        return fake_readiness

    monkeypatch.setattr(runtime_module, "import_fred_alfred_vintages", fake_importer)
    monkeypatch.setattr(
        runtime_module,
        "verify_and_publish_fred_alfred_readiness",
        fake_verifier,
    )
    client = FredAlfredClient(_TEST_KEY, session=_CompleteFredSession())
    first = runtime_module.run_fred_alfred_incremental_once(
        tmp_path,
        client=client,
        as_of="2024-06-02T12:00:00Z",
    )
    second = runtime_module.run_fred_alfred_incremental_once(
        tmp_path,
        client=client,
        as_of="2024-06-02T23:59:59Z",
    )
    assert first.status == "COMPLETE"
    assert second.status == "ALREADY_COMPLETE_TODAY"
    assert second.receipt_path == first.receipt_path
    assert calls == {"import": 1, "verify": 1}
    assert _TEST_KEY.encode("utf-8") not in first.receipt_path.read_bytes()


def _provider_rows() -> list[dict[str, object]]:
    return [
        _provider_row("FEDFUNDS", "2024-01-01", "2024-02-01", "9999-12-31", 5.25),
        _provider_row("FEDFUNDS", "2024-02-01", "2024-03-01", "9999-12-31", 5.50),
        _provider_row("CPIAUCSL", "2023-01-01", "2023-02-14", "9999-12-31", 100.0),
        _provider_row("CPIAUCSL", "2024-01-01", "2024-02-14", "2024-03-19", 110.0),
        _provider_row("CPIAUCSL", "2024-01-01", "2024-03-20", "9999-12-31", 112.0),
        _provider_row("UNRATE", "2023-12-01", "2024-01-04", "9999-12-31", 4.0),
        _provider_row("UNRATE", "2024-01-01", "2024-02-01", "9999-12-31", 4.3),
        _provider_row("UNRATE", "2024-02-01", "2024-03-07", "9999-12-31", 4.2),
        _provider_row("GDP", "2022-10-01", "2023-01-25", "9999-12-31", 100.0),
        _provider_row("GDP", "2023-10-01", "2024-01-25", "9999-12-31", 105.0),
    ]


def _provider_row(
    series: str,
    observation: str,
    realtime_start: str,
    realtime_end: str,
    value: float,
) -> dict[str, object]:
    return {
        "series_name": series,
        "observation_date": observation,
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
        "value": value,
    }


def _normalized_rows() -> list[dict[str, object]]:
    acquired = pd.Timestamp("2024-06-01T12:00:00Z")
    return [
        {
            **row,
            "release_at": alfred_provider_available_at(row["realtime_start"]),
            "release_time_precision": "DATE",
            "fetched_at": acquired,
            "availability_basis": ALFRED_VINTAGE_AVAILABILITY_BASIS,
            "unit": "test",
            "frequency": "Quarterly" if row["series_name"] == "GDP" else "Monthly",
        }
        for row in _provider_rows()
    ]


def _decisions(*values: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "decision_timestamp": pd.to_datetime(
                list(values), utc=True, format="mixed"
            )
        }
    )


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
