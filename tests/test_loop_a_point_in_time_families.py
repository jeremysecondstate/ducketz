from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from datafetching import sec_fetch
from datafetching.calculated_features import write_immutable_feature_partition
from datafetching.fred_vintages import (
    derive_current_fred_rate_receipt,
    derive_macro_release_features,
    normalize_fred_vintage_rows,
    persist_current_fred_rate_receipt,
    persist_fred_vintages,
)
from datafetching.parquet_store import ParquetStore
from datafetching.sec_events import (
    normalize_sec_event_rows,
    persist_sec_events,
)
from fundamentals.point_in_time import (
    calculate_point_in_time_fundamentals,
    persist_point_in_time_fundamentals,
)
from signals.technical_lifecycle import (
    calculate_technical_lifecycle_snapshot,
    persist_technical_lifecycle,
)


def test_immutable_calculated_writer_fails_closed_on_concurrent_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "features.parquet"
    lock = path.with_name(f".{path.name}.write.lock")
    lock.write_text("pid=someone-else\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Another writer"):
        write_immutable_feature_partition(
            path,
            pd.DataFrame(
                {
                    "symbol": ["NVDA"],
                    "available_at": ["2025-01-01T00:00:00Z"],
                    "value": [1.0],
                }
            ),
            columns=("symbol", "available_at", "value"),
            natural_key=("symbol", "available_at"),
        )
    assert not path.exists()


def test_fundamental_amendment_appends_only_a_later_version(
    tmp_path: Path,
) -> None:
    income, balance, cash = _statement_frames()
    first = calculate_point_in_time_fundamentals(
        income,
        balance,
        cash,
        symbol="NVDA",
        period_type="quarterly",
        calculated_at="2025-05-06T12:00:00Z",
    )
    path = persist_point_in_time_fundamentals(
        tmp_path,
        first,
        symbol="NVDA",
        period_type="quarterly",
    )
    revised = income.copy()
    amendment = revised.iloc[-1].copy()
    amendment["revenue"] = 175.0
    amendment["accepted_date"] = "2025-05-07T18:30:00Z"
    amendment["fetched_at"] = "2025-05-07T18:31:00Z"
    amendment["available_at"] = "2025-05-07T18:31:00Z"
    revised = pd.concat([revised, amendment.to_frame().T], ignore_index=True)
    second = calculate_point_in_time_fundamentals(
        revised,
        balance,
        cash,
        symbol="NVDA",
        period_type="quarterly",
        calculated_at="2025-05-07T18:32:00Z",
    )
    persist_point_in_time_fundamentals(
        tmp_path,
        second,
        symbol="NVDA",
        period_type="quarterly",
    )

    stored = pd.read_parquet(path)
    assert list(stored.columns)[0] == "id"
    assert stored.columns.tolist().count("id") == 1
    latest_period = pd.Timestamp("2025-03-31", tz="UTC")
    versions = stored.loc[stored["period_end_date"].eq(latest_period)]
    assert len(versions) == 2
    assert versions["available_at"].is_monotonic_increasing
    assert versions.iloc[0]["revenue_growth_yoy"] != versions.iloc[1][
        "revenue_growth_yoy"
    ]


def test_fundamental_growth_keeps_lagged_decision_version() -> None:
    income, balance, cash = _statement_frames()
    original = calculate_point_in_time_fundamentals(
        income,
        balance,
        cash,
        symbol="NVDA",
        period_type="quarterly",
        calculated_at="2025-05-06T12:00:00Z",
    )
    amended = income.copy()
    prior = amended.iloc[0].copy()
    prior["revenue"] = 50.0
    prior["accepted_date"] = "2025-05-07T18:30:00Z"
    prior["fetched_at"] = "2025-05-07T18:31:00Z"
    prior["available_at"] = "2025-05-07T18:31:00Z"
    amended = pd.concat([amended, prior.to_frame().T], ignore_index=True)

    later = calculate_point_in_time_fundamentals(
        amended,
        balance,
        cash,
        symbol="NVDA",
        period_type="quarterly",
        calculated_at="2025-05-07T18:32:00Z",
    )

    assert original.iloc[-1]["revenue_growth_yoy"] == pytest.approx(0.5)
    assert later.iloc[-1]["revenue_growth_yoy"] == pytest.approx(0.5)
    assert later.iloc[-1]["lagged_comparison_available_at"] == pd.Timestamp(
        "2024-05-01T12:01:00Z"
    )


def test_fundamental_version_upgrade_appends_and_partial_rows_are_quarantined(
    tmp_path: Path,
) -> None:
    income, balance, cash = _statement_frames()
    first = calculate_point_in_time_fundamentals(
        income,
        balance,
        cash,
        symbol="MU",
        period_type="quarterly",
        calculated_at="2025-05-06T12:00:00Z",
    )
    path = persist_point_in_time_fundamentals(
        tmp_path,
        first,
        symbol="MU",
        period_type="quarterly",
    )
    upgraded = first.copy()
    upgraded["calculation_version"] = "2.0.0"
    upgraded["calculated_at"] = pd.Timestamp("2025-05-07T12:00:00Z")
    upgraded["available_at"] = upgraded["calculated_at"]
    persist_point_in_time_fundamentals(
        tmp_path,
        upgraded,
        symbol="MU",
        period_type="quarterly",
    )
    stored = pd.read_parquet(path)
    assert set(stored["calculation_version"]) == {"1.0.0", "2.0.0"}

    partial = calculate_point_in_time_fundamentals(
        income,
        pd.DataFrame(),
        pd.DataFrame(),
        symbol="MU",
        period_type="quarterly",
        calculated_at="2025-05-06T12:00:00Z",
    )
    assert not partial["constituent_complete"].any()
    assert partial["missing_statement_families"].eq("balance,cash").all()


def test_market_cap_denominator_requires_availability_provenance() -> None:
    income, balance, cash = _statement_frames()
    with pytest.raises(ValueError, match="requires period_end_date"):
        calculate_point_in_time_fundamentals(
            income,
            balance,
            cash,
            symbol="NVDA",
            period_type="quarterly",
            calculated_at="2025-05-06T12:00:00Z",
            market_cap_by_period=pd.Series(
                [1_000.0],
                index=[pd.Timestamp("2025-03-31T00:00:00Z")],
            ),
        )


def test_date_only_fundamental_publication_is_conservative_and_estimates_quarantine() -> None:
    income, balance, cash = _statement_frames()
    income.loc[income.index[-1], "accepted_date"] = "2025-05-05"
    income.loc[income.index[-1], "available_at"] = "2025-05-06T00:00:00Z"
    output = calculate_point_in_time_fundamentals(
        income,
        balance,
        cash,
        symbol="NVDA",
        period_type="quarterly",
        calculated_at="2025-05-06T00:01:00Z",
    )
    latest = output.iloc[-1]
    assert latest["accepted_at"] >= pd.Timestamp("2025-05-06T00:00:00Z")
    assert latest["available_at"] >= latest["accepted_at"]

    no_publication = income.copy()
    no_publication["accepted_date"] = None
    no_publication["published_at"] = None
    no_publication["effective_date_estimated"] = True
    estimated = calculate_point_in_time_fundamentals(
        no_publication,
        balance.assign(accepted_date=None, effective_date_estimated=True),
        cash.assign(accepted_date=None, effective_date_estimated=True),
        symbol="NVDA",
        period_type="quarterly",
        calculated_at="2025-05-06T00:01:00Z",
    )
    assert bool(estimated.iloc[-1]["effective_date_estimated"])


def test_current_revised_fred_rows_are_rejected_without_vintage_identity() -> None:
    with pytest.raises(ValueError, match="not a point-in-time vintage"):
        normalize_fred_vintage_rows(
            pd.DataFrame(
                {
                    "series": ["CPIAUCSL"],
                    "date": ["2025-01-01"],
                    "fetched_at": ["2025-02-01T12:00:00Z"],
                    "value": [310.0],
                }
            )
        )


def test_current_fred_rate_receipt_is_causal_only_after_local_fetch(
    tmp_path: Path,
) -> None:
    rows = pd.DataFrame(
        {
            "series": ["FEDFUNDS", "FEDFUNDS"],
            "source": ["fred", "fred"],
            "date": ["2026-06-01", "2026-07-01"],
            "fetched_at": [
                "2026-08-10T23:30:00Z",
                "2026-08-10T23:30:00Z",
            ],
            "value": [3.65, 3.63],
        }
    )

    derived = derive_current_fred_rate_receipt(rows)
    assert derived.iloc[0]["available_at"] == pd.Timestamp(
        "2026-08-10T23:30:00Z"
    )
    assert derived.iloc[0]["fed_funds_available_at"] == pd.Timestamp(
        "2026-08-10T23:30:00Z"
    )
    assert derived.iloc[0]["macro__fed_funds_level"] == 3.63

    path = persist_current_fred_rate_receipt(tmp_path, rows)[0]
    stored = pd.read_parquet(path)
    assert stored.iloc[0]["context_name"] == "fred-current-receipt-rate"
    assert stored.iloc[0]["available_at"] > pd.Timestamp("2026-07-01T00:00:00Z")


def test_fred_vintages_persist_and_derived_values_use_only_released_rows(
    tmp_path: Path,
) -> None:
    rows = _fred_vintage_rows()
    normalized = normalize_fred_vintage_rows(rows)
    paths = persist_fred_vintages(tmp_path, normalized)
    assert paths
    assert all(pd.read_parquet(path).columns[0] == "id" for path in paths)

    derived = derive_macro_release_features(normalized)
    first_complete = derived.dropna(
        subset=[
            "macro__fed_funds_level",
            "macro__cpi_yoy",
            "macro__unemployment_change",
            "macro__gdp_yoy",
        ]
    ).iloc[0]
    release = first_complete["available_at"]
    assert normalized.loc[normalized["available_at"].gt(release)].shape[0] > 0


def test_fred_vintage_replay_preserves_first_local_receipt(
    tmp_path: Path,
) -> None:
    row = _fred_vintage_rows()[0]
    first = normalize_fred_vintage_rows([row])
    path = persist_fred_vintages(tmp_path, first)[0]
    replay = dict(row)
    replay["fetched_at"] = pd.Timestamp(row["fetched_at"]) + pd.Timedelta(days=1)

    persist_fred_vintages(
        tmp_path,
        normalize_fred_vintage_rows([replay]),
    )

    stored = pd.read_parquet(path)
    assert len(stored) == 1
    assert stored.iloc[0]["fetched_at"] == first.iloc[0]["fetched_at"]
    assert stored.iloc[0]["schema_version"] == "fred-vintage-v1"


def test_macro_lag_requires_exact_calendar_cadence() -> None:
    rows = _fred_vintage_rows()
    rows = [
        row
        for row in rows
        if not (
            row["series_name"] == "CPIAUCSL"
            and pd.Timestamp(row["observation_date"])
            == pd.Timestamp("2023-01-01T00:00:00Z")
        )
    ]
    derived = derive_macro_release_features(normalize_fred_vintage_rows(rows))
    latest = derived.iloc[-1]
    assert pd.isna(latest["macro__cpi_yoy"])
    assert pd.isna(latest["cpi_available_at"])


def test_sec_availability_uses_acceptance_receipt_and_extraction_and_keeps_gates(
    tmp_path: Path,
) -> None:
    frame = normalize_sec_event_rows(
        [
            {
                "symbol": "MU",
                "accepted_date": "2025-02-03T21:15:00Z",
                "form_type": "8-K",
                "instrument_event": "Securities offering",
                "evidence_state": "pending",
                "evidence_quality": "Confirmed terms",
                "offering_size": 100_000_000.0,
                "quantity": 5_000_000.0,
                "offering_price": 20.0,
            }
        ],
        document_received_at="2025-02-03T21:16:00Z",
        extraction_completed_at="2025-02-03T21:16:30Z",
    )
    assert frame.iloc[0]["available_at"] == pd.Timestamp(
        "2025-02-03T21:16:30Z"
    )
    assert frame.iloc[0]["filing_event_impulse"] == 1.0
    assert pd.isna(frame.iloc[0]["offering_size_to_market_cap"])
    path = persist_sec_events(tmp_path, frame)[0]
    stored = pd.read_parquet(path)
    assert stored.columns[0] == "id"
    assert stored.columns.tolist().count("id") == 1


def test_sec_denominators_require_causal_availability() -> None:
    row = {
        "symbol": "MU",
        "accepted_date": "2025-02-03T21:15:00Z",
        "form_type": "8-K",
        "instrument_event": "Securities offering",
        "evidence_state": "pending",
        "evidence_quality": "Confirmed terms",
        "offering_size": 100_000_000.0,
    }
    with pytest.raises(ValueError, match="denominator_available_at"):
        normalize_sec_event_rows(
            [row],
            document_received_at="2025-02-03T21:16:00Z",
            extraction_completed_at="2025-02-03T21:16:30Z",
            market_cap=1_000_000_000.0,
        )


def test_sec_refetch_does_not_reissue_an_already_scanned_filing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = "2025-05-05T20:15:00Z"
    existing = normalize_sec_event_rows(
        [
            {
                "symbol": "NVDA",
                "accepted_date": accepted,
                "form_type": "424B5",
                "instrument_event": "Securities offering",
                "evidence_state": "completed",
                "evidence_quality": "confirmed terms",
                "accession_number": "0000000000-25-000001",
                "document_url": "https://www.sec.gov/example.htm",
            }
        ],
        document_received_at="2025-05-05T20:16:00Z",
        extraction_completed_at="2025-05-05T20:17:00Z",
    )
    paths = persist_sec_events(tmp_path, existing)

    class FakeScanner:
        text_fetches = 0

        def _fetch_fmp_filings(self, _symbol: str) -> list[dict[str, object]]:
            return [
                {
                    "type": "424B5",
                    "acceptedDate": accepted,
                    "finalLink": "https://www.sec.gov/example.htm",
                }
            ]

        def _fetch_text(self, _url: str) -> str:
            type(self).text_fetches += 1
            return "offering"

    monkeypatch.setattr(sec_fetch, "SecCapitalStructureScanner", FakeScanner)
    result = sec_fetch.fetch("NVDA", ParquetStore(tmp_path))

    assert result.error_files == 0
    assert FakeScanner.text_fetches == 0
    assert len(pd.read_parquet(paths[0])) == 1


def test_technical_lifecycle_is_forward_going_and_canonical_provider_only(
    tmp_path: Path,
) -> None:
    timestamps = pd.date_range("2025-01-02", periods=30, freq="B", tz="UTC")
    daily = pd.DataFrame(
        {
            "timestamp": timestamps,
            "bar_end_timestamp": timestamps + pd.Timedelta(hours=21),
            "bar_complete": True,
            "technical_score": range(40, 70),
        }
    )
    output = calculate_technical_lifecycle_snapshot(
        {
            ("databento", "1d"): daily,
            ("schwab", "1d"): daily.assign(technical_score=0.0),
        },
        symbol="GOOG",
        calculated_at="2025-02-20T22:00:00Z",
    )
    assert len(output) == 1
    assert output.iloc[0]["technical_consensus_score"] == 69.0
    assert output.iloc[0]["available_at"] == pd.Timestamp(
        "2025-02-20T22:00:00Z"
    )
    path = persist_technical_lifecycle(tmp_path, output)
    assert pd.read_parquet(path).columns[0] == "id"


def test_technical_lifecycle_uses_all_constituent_clocks_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    timestamps = pd.date_range("2025-01-02", periods=30, freq="B", tz="UTC")
    availability = timestamps + pd.Timedelta(hours=21, minutes=5)
    availability = pd.Series(availability)
    availability.iloc[-10] = pd.Timestamp("2025-02-20T21:30:00Z")
    daily = pd.DataFrame(
        {
            "timestamp": timestamps,
            "bar_end_timestamp": timestamps + pd.Timedelta(hours=21),
            "available_at": availability,
            "bar_complete": True,
            "technical_score": range(40, 70),
        }
    )
    output = calculate_technical_lifecycle_snapshot(
        {("databento", "1d"): daily},
        symbol="NVDA",
        calculated_at="2025-02-20T22:00:00Z",
    )
    assert output.iloc[0]["constituent_available_at"] == pd.Timestamp(
        "2025-02-20T21:30:00Z"
    )
    path = persist_technical_lifecycle(tmp_path, output)

    conflicting = output.copy()
    conflicting.loc[0, "timing_score"] = 999.0
    with pytest.raises(ValueError, match="conflict"):
        persist_technical_lifecycle(tmp_path, conflicting)

    later = output.copy()
    later.loc[0, "available_at"] = pd.Timestamp("2025-02-20T22:05:00Z")
    later.loc[0, "calculated_at"] = later.loc[0, "available_at"]
    later.loc[0, "timing_score"] = 75.0
    persist_technical_lifecycle(tmp_path, later)
    assert len(pd.read_parquet(path)) == 2


def _statement_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.to_datetime(
        [
            "2024-03-31",
            "2024-06-30",
            "2024-09-30",
            "2024-12-31",
            "2025-03-31",
        ],
        utc=True,
    )
    accepted = pd.to_datetime(
        [
            "2024-05-01T12:00:00Z",
            "2024-08-01T12:00:00Z",
            "2024-11-01T12:00:00Z",
            "2025-02-01T12:00:00Z",
            "2025-05-05T12:00:00Z",
        ],
        utc=True,
    )
    base = pd.DataFrame(
        {
            "date": dates,
            "period": "Q1",
            "accepted_date": accepted,
            "fetched_at": accepted + pd.Timedelta(minutes=1),
            "available_at": accepted + pd.Timedelta(minutes=1),
            "effective_date_estimated": False,
        }
    )
    income = base.assign(
        revenue=[100.0, 110.0, 120.0, 130.0, 150.0],
        operating_income=[20.0, 21.0, 24.0, 25.0, 33.0],
        net_income=[15.0, 16.0, 18.0, 19.0, 24.0],
        weighted_average_shs_out_dil=[10.0, 10.1, 10.2, 10.3, 10.5],
        income_before_tax=[18.0, 19.0, 21.0, 22.0, 28.0],
        income_tax_expense=[3.0, 3.0, 3.0, 3.0, 4.0],
    )
    balance = base.assign(
        cash_and_cash_equivalents=50.0,
        total_debt=25.0,
        total_current_assets=100.0,
        total_current_liabilities=50.0,
        total_stockholders_equity=200.0,
    )
    cash = base.assign(
        operating_cash_flow=[18.0, 19.0, 22.0, 23.0, 30.0],
        capital_expenditure=-5.0,
        stock_based_compensation=2.0,
        common_stock_issuance=1.0,
        common_stock_repurchased=-3.0,
    )
    return income, balance, cash


def _fred_vintage_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    specs = {
        "CPIAUCSL": (13, "MS", 300.0),
        "UNRATE": (2, "MS", 4.0),
        "GDP": (5, "QS", 25_000.0),
        "FEDFUNDS": (2, "MS", 5.0),
    }
    release_counter = 0
    for series, (periods, frequency, base) in specs.items():
        for index, observation in enumerate(
            pd.date_range("2023-01-01", periods=periods, freq=frequency, tz="UTC")
        ):
            release = pd.Timestamp("2025-01-01T12:00:00Z") + pd.Timedelta(
                days=release_counter
            )
            release_counter += 1
            rows.append(
                {
                    "series_name": series,
                    "observation_date": observation,
                    "realtime_start": release.normalize(),
                    "realtime_end": None,
                    "release_at": release,
                    "fetched_at": release + pd.Timedelta(minutes=1),
                    "value": base + index,
                    "unit": "test",
                    "frequency": frequency,
                }
            )
    return rows
