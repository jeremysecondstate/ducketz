from __future__ import annotations

import pandas as pd
import pytest

from ml.contracts import MLContractError
from ml.datasets.families import (
    load_bar_shape_features,
    load_cme_context_features,
    load_energy_context_features,
    load_fundamental_features,
    load_lifecycle_features,
    load_macro_features,
    load_option_features,
    load_quote_liquidity_features,
    load_sec_event_features,
)
from ml.datasets.point_in_time import (
    backward_asof_by_symbol,
    conservative_date_only_availability,
    exact_feature_join,
    model_value_projection,
)


def test_exact_join_allows_exact_and_rejects_future_availability() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "provider": "databento",
            "timeframe": "1h",
            "bar_timestamp": pd.to_datetime(
                ["2026-07-29T14:00:00Z", "2026-07-29T15:00:00Z"],
                utc=True,
            ),
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T15:05:00Z", "2026-07-29T16:05:00Z"],
                utc=True,
            ),
        }
    )
    source = decisions.loc[
        :,
        ["symbol", "provider", "timeframe", "bar_timestamp"],
    ].copy()
    source["available_at"] = [
        pd.Timestamp("2026-07-29T15:05:00Z"),
        pd.Timestamp("2026-07-29T16:05:00.000001Z"),
    ]
    source["close_location"] = [0.75, 0.25]

    joined = exact_feature_join(
        decisions,
        source,
        family="bar",
        value_columns={"bar__close_location": "close_location"},
    )

    assert joined.loc[0, "bar__close_location"] == 0.75
    assert pd.isna(joined.loc[1, "bar__close_location"])
    assert joined.loc[0, "bar__join_status"] == "JOINED"
    assert joined.loc[1, "bar__join_status"] == "FUTURE_REJECTED"
    assert joined.loc[1, "bar__available_at"] > joined.loc[
        1, "decision_timestamp"
    ]


def test_exact_join_duplicate_natural_key_fails_closed() -> None:
    decisions = _exact_decision()
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "provider": "databento",
            "timeframe": "1h",
            "bar_timestamp": pd.to_datetime(
                ["2026-07-29T14:00:00Z"] * 2,
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-29T15:05:00Z"] * 2,
                utc=True,
            ),
            "close_location": [0.5, 0.6],
        }
    )
    with pytest.raises(MLContractError, match="duplicate natural keys"):
        exact_feature_join(
            decisions,
            source,
            family="bar",
            value_columns={"bar__close_location": "close_location"},
        )


def test_symbol_asof_has_no_prepublication_fill_and_exact_freshness_boundary() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": "NVDA",
            "decision_timestamp": [
                pd.Timestamp("2026-07-29T09:59:59Z"),
                pd.Timestamp("2026-07-29T10:00:00Z"),
                pd.Timestamp("2026-07-29T10:05:00Z"),
                pd.Timestamp("2026-07-29T10:05:00.000000001Z"),
            ],
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "relative_bid_ask_spread": [0.01],
        }
    )
    joined = backward_asof_by_symbol(
        decisions,
        source,
        family="quote",
        value_columns={
            "quote__relative_bid_ask_spread": "relative_bid_ask_spread"
        },
        freshness=pd.Timedelta(minutes=5),
    )

    assert pd.isna(joined.loc[0, "quote__relative_bid_ask_spread"])
    assert joined.loc[0, "quote__join_status"] == "NO_PRIOR_PUBLICATION"
    assert joined.loc[1, "quote__relative_bid_ask_spread"] == 0.01
    assert joined.loc[2, "quote__relative_bid_ask_spread"] == 0.01
    assert pd.isna(joined.loc[3, "quote__relative_bid_ask_spread"])
    assert joined.loc[3, "quote__join_status"] == "STALE"


def test_later_amendment_changes_only_later_decisions() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": "NVDA",
            "decision_timestamp": pd.to_datetime(
                [
                    "2026-01-01T12:00:00Z",
                    "2026-01-02T12:00:00Z",
                    "2026-01-04T12:00:00Z",
                    "2026-01-05T12:00:00Z",
                ],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "period_type": ["quarterly", "quarterly"],
            "period_end_date": ["2025-12-31", "2025-12-31"],
            "available_at": pd.to_datetime(
                ["2026-01-02T12:00:00Z", "2026-01-05T12:00:00Z"],
                utc=True,
            ),
            "effective_date_estimated": [False, False],
            "constituent_complete": [True, True],
            "operating_margin": [0.20, 0.18],
        }
    )

    joined = load_fundamental_features(
        decisions,
        source,
        value_columns={"fund__operating_margin": "operating_margin"},
    )

    assert pd.isna(joined.loc[0, "fund__operating_margin"])
    assert joined.loc[1, "fund__operating_margin"] == 0.20
    assert joined.loc[2, "fund__operating_margin"] == 0.20
    assert joined.loc[3, "fund__operating_margin"] == 0.18


def test_fundamentals_reject_incomplete_constituents_without_fallback() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-01-02T12:00:00Z", "2026-01-03T12:00:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "period_type": ["quarterly", "quarterly"],
            "period_end_date": ["2025-12-31", "2025-12-31"],
            "available_at": pd.to_datetime(
                ["2026-01-02T12:00:00Z", "2026-01-03T12:00:00Z"],
                utc=True,
            ),
            "effective_date_estimated": [False, False],
            "constituent_complete": [True, False],
            "operating_margin": [0.20, 0.19],
        }
    )

    joined = load_fundamental_features(
        decisions,
        source,
        value_columns={"fund__operating_margin": "operating_margin"},
    )

    assert joined.loc[0, "fund__operating_margin"] == 0.20
    assert pd.isna(joined.loc[1, "fund__operating_margin"])
    assert joined.loc[1, "fund__join_status"] == "QUALITY_REJECTED"
    assert not joined.loc[1, "fund__audit_constituent_complete"]


def test_fundamentals_reject_future_denominator_availability() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-01-02T12:00:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "period_type": ["quarterly"],
            "period_end_date": ["2025-12-31"],
            "available_at": pd.to_datetime(
                ["2026-01-02T12:00:00Z"],
                utc=True,
            ),
            "market_cap_available_at": pd.to_datetime(
                ["2026-01-03T12:00:00Z"],
                utc=True,
            ),
            "effective_date_estimated": [False],
            "constituent_complete": [True],
            "fcf_yield": [0.03],
        }
    )

    with pytest.raises(
        MLContractError,
        match="precedes market_cap_available_at",
    ):
        load_fundamental_features(
            decisions,
            source,
            value_columns={"fund__fcf_yield": "fcf_yield"},
        )


def test_date_only_publication_uses_first_decision_after_entire_date() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-01-02T20:05:00Z", "2026-01-05T20:05:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "publication_date": ["2026-01-02"],
        }
    )
    normalized = conservative_date_only_availability(
        source,
        decisions,
        date_column="publication_date",
    )
    assert normalized.loc[0, "available_at"] == pd.Timestamp(
        "2026-01-05T20:05:00Z"
    )


def test_option_loader_uses_receipts_and_preserves_repeated_aligned_decision() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:01:30Z", "2026-07-29T10:02:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "snapshot_for": pd.to_datetime(
                ["2026-07-29T10:00:00Z"] * 2,
                utc=True,
            ),
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:00:00Z"] * 2,
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-29T10:01:00Z", "2026-07-29T10:02:00Z"],
                utc=True,
            ),
            "quote_cutoff_at": pd.to_datetime(
                ["2026-07-29T09:59:30Z"] * 2,
                utc=True,
            ),
            "underlying_quote_timestamp": pd.to_datetime(
                ["2026-07-29T09:59:00Z"] * 2,
                utc=True,
            ),
            "surface_quality_pass": [True, True],
            "iv_minus_realized_volatility": [0.10, 0.20],
        }
    )
    joined = load_option_features(
        decisions,
        source,
        horizon="1h",
        value_columns={
            "opt__iv_minus_realized": "iv_minus_realized_volatility"
        },
    )
    assert joined["opt__iv_minus_realized"].tolist() == [0.10, 0.20]
    assert joined["opt__available_at"].tolist() == source[
        "available_at"
    ].tolist()

    unsafe = load_option_features(
        decisions,
        source.drop(columns="quote_cutoff_at"),
        horizon="1h",
        value_columns={
            "opt__iv_minus_realized": "iv_minus_realized_volatility"
        },
    )
    assert unsafe["opt__iv_minus_realized"].isna().all()
    assert unsafe["opt__join_status"].eq("QUALITY_REJECTED").all()

    with pytest.raises(MLContractError, match="receipt available_at"):
        load_option_features(
            decisions,
            source.drop(columns="available_at"),
            horizon="1h",
            value_columns={
                "opt__iv_minus_realized": "iv_minus_realized_volatility"
            },
        )


def test_cme_long_context_uses_identical_window_and_max_availability() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T11:03:00Z", "2026-07-29T12:03:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "context_name": ["nq_return_1h", "es_return_1h", "nq_return_1h"],
            "window_start": pd.to_datetime(
                [
                    "2026-07-29T10:00:00Z",
                    "2026-07-29T10:00:00Z",
                    "2026-07-29T11:00:00Z",
                ],
                utc=True,
            ),
            "window_end": pd.to_datetime(
                [
                    "2026-07-29T11:00:00Z",
                    "2026-07-29T11:00:00Z",
                    "2026-07-29T12:00:00Z",
                ],
                utc=True,
            ),
            "available_at": pd.to_datetime(
                [
                    "2026-07-29T11:01:00Z",
                    "2026-07-29T11:03:00Z",
                    "2026-07-29T12:03:00Z",
                ],
                utc=True,
            ),
            "value": [0.01, 0.005, 0.02],
            "constituent_complete": [True, True, True],
        }
    )
    joined = load_cme_context_features(
        decisions,
        source,
        horizon="1h",
        value_columns={
            "cme__nq_return_1h": "nq_return_1h",
            "cme__es_return_1h": "es_return_1h",
        },
    )

    assert joined.loc[0, "cme__available_at"] == pd.Timestamp(
        "2026-07-29T11:03:00Z"
    )
    assert joined.loc[0, "cme__nq_return_1h"] == 0.01
    assert joined.loc[0, "cme__es_return_1h"] == 0.005
    assert joined.loc[1, "cme__join_status"] == "QUALITY_REJECTED"
    assert pd.isna(joined.loc[1, "cme__nq_return_1h"])


def test_cme_mismatched_constituent_window_fails_closed() -> None:
    decisions = pd.DataFrame(
        {
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T11:03:00Z"],
                utc=True,
            )
        }
    )
    source = pd.DataFrame(
        {
            "window_start": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "window_end": pd.to_datetime(
                ["2026-07-29T11:00:00Z"],
                utc=True,
            ),
            "nq_window_start": pd.to_datetime(
                ["2026-07-29T10:01:00Z"],
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-29T11:03:00Z"],
                utc=True,
            ),
            "constituent_complete": [True],
            "nq_return_1h": [0.01],
        }
    )
    with pytest.raises(MLContractError, match="not synchronized"):
        load_cme_context_features(
            decisions,
            source,
            horizon="1h",
            value_columns={"cme__nq_return_1h": "nq_return_1h"},
        )


def test_cme_rejects_availability_before_any_constituent() -> None:
    decisions = pd.DataFrame(
        {
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T11:05:00Z"],
                utc=True,
            )
        }
    )
    source = pd.DataFrame(
        {
            "window_start": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "window_end": pd.to_datetime(
                ["2026-07-29T11:00:00Z"],
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-29T11:03:00Z"],
                utc=True,
            ),
            "nq_available_at": pd.to_datetime(
                ["2026-07-29T11:04:00Z"],
                utc=True,
            ),
            "constituent_complete": [True],
            "nq_return": [0.01],
        }
    )

    with pytest.raises(MLContractError, match="precedes nq_available_at"):
        load_cme_context_features(
            decisions,
            source,
            horizon="1h",
            value_columns={"cme__nq_return_1h": "nq_return"},
        )


def test_revised_fred_without_realtime_identity_is_rejected() -> None:
    decisions = pd.DataFrame(
        {
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T20:05:00Z"],
                utc=True,
            )
        }
    )
    current_revised = pd.DataFrame(
        {
            "series_name": ["CPIAUCSL"],
            "observation_date": ["2026-06-01"],
            "available_at": pd.to_datetime(
                ["2026-07-15T12:00:00Z"],
                utc=True,
            ),
            "cpi_yoy": [0.025],
        }
    )
    with pytest.raises(MLContractError, match="realtime_start"):
        load_macro_features(
            decisions,
            current_revised,
            value_columns={"macro__cpi_yoy": "cpi_yoy"},
        )


def test_macro_context_cannot_precede_its_feature_vintage_lineage() -> None:
    decisions = pd.DataFrame(
        {
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T20:05:00Z"],
                utc=True,
            )
        }
    )
    derived = pd.DataFrame(
        {
            "context_name": ["macro-release-context"],
            "available_at": pd.to_datetime(
                ["2026-07-15T12:00:00Z"],
                utc=True,
            ),
            "cpi_available_at": pd.to_datetime(
                ["2026-07-15T12:01:00Z"],
                utc=True,
            ),
            "calculation_version": ["1.0.0"],
            "macro__cpi_yoy": [0.025],
        }
    )
    vintages = pd.DataFrame(
        {
            "series_name": ["CPIAUCSL"],
            "observation_date": ["2026-06-01"],
            "realtime_start": ["2026-07-15"],
            "realtime_end": ["9999-12-31"],
            "release_at": pd.to_datetime(
                ["2026-07-15T12:00:00Z"],
                utc=True,
            ),
            "fetched_at": pd.to_datetime(
                ["2026-07-15T12:01:00Z"],
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-15T12:01:00Z"],
                utc=True,
            ),
        }
    )
    with pytest.raises(MLContractError, match="exceeds macro context"):
        load_macro_features(
            decisions,
            derived,
            value_columns={"macro__cpi_yoy": "macro__cpi_yoy"},
            vintage_source=vintages,
        )


def test_macro_freshness_uses_each_feature_vintage_clock() -> None:
    cpi_available = pd.Timestamp("2026-06-15T12:00:00Z")
    gdp_available = pd.Timestamp("2026-04-01T12:00:00Z")
    boundary = pd.Timestamp("2026-07-30T12:00:00Z")
    decisions = pd.DataFrame(
        {
            "decision_timestamp": [
                boundary,
                boundary + pd.Timedelta(nanoseconds=1),
            ]
        }
    )
    derived = pd.DataFrame(
        {
            "context_name": ["macro-release-context"],
            "available_at": [cpi_available],
            "calculation_version": ["1.0.0"],
            "cpi_available_at": [cpi_available],
            "gdp_available_at": [gdp_available],
            "macro__cpi_yoy": [0.025],
            "macro__gdp_yoy": [0.03],
        }
    )
    vintages = pd.DataFrame(
        {
            "series_name": ["CPIAUCSL", "GDP"],
            "observation_date": ["2026-05-01", "2026-01-01"],
            "realtime_start": ["2026-06-15", "2026-04-01"],
            "realtime_end": ["9999-12-31", "9999-12-31"],
            "release_at": [
                cpi_available - pd.Timedelta(minutes=1),
                gdp_available - pd.Timedelta(minutes=1),
            ],
            "fetched_at": [cpi_available, gdp_available],
            "available_at": [cpi_available, gdp_available],
        }
    )

    joined = load_macro_features(
        decisions,
        derived,
        value_columns={
            "macro__cpi_yoy": "macro__cpi_yoy",
            "macro__gdp_yoy": "macro__gdp_yoy",
        },
        vintage_source=vintages,
    )

    assert joined.loc[0, "macro__cpi_yoy"] == 0.025
    assert joined.loc[0, "macro__gdp_yoy"] == 0.03
    assert not joined.loc[0, "macro__cpi_yoy__is_stale"]
    assert not joined.loc[0, "macro__gdp_yoy__is_stale"]
    assert pd.isna(joined.loc[1, "macro__cpi_yoy"])
    assert pd.isna(joined.loc[1, "macro__gdp_yoy"])
    assert joined.loc[1, "macro__cpi_yoy__is_stale"]
    assert joined.loc[1, "macro__gdp_yoy__is_stale"]
    assert not joined["macro__is_stale"].any()


def test_sec_acceptance_not_period_end_controls_availability() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-01-02T20:05:00Z", "2026-01-03T20:05:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "period_end_date": ["2025-09-30"],
            "filing_accepted_at": pd.to_datetime(
                ["2026-01-03T19:00:00Z"],
                utc=True,
            ),
            "document_received_at": pd.to_datetime(
                ["2026-01-03T19:01:00Z"],
                utc=True,
            ),
            "extraction_completed_at": pd.to_datetime(
                ["2026-01-03T19:02:00Z"],
                utc=True,
            ),
            "event_type": ["DILUTION"],
            "dilution_event": [1.0],
        }
    )
    joined = load_sec_event_features(
        decisions,
        source,
        value_columns={"sec__dilution_event": "dilution_event"},
    )
    assert pd.isna(joined.loc[0, "sec__dilution_event"])
    assert joined.loc[1, "sec__dilution_event"] == 1.0
    assert joined.loc[1, "sec__available_at"] == pd.Timestamp(
        "2026-01-03T19:02:00Z"
    )


def test_sec_multi_event_filing_aggregates_once_and_does_not_persist_state() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                [
                    "2026-01-02T20:05:00Z",
                    "2026-01-03T20:05:00Z",
                    "2026-01-04T20:05:00Z",
                ],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "filing_accepted_at": pd.to_datetime(
                ["2026-01-03T19:00:00Z"] * 2,
                utc=True,
            ),
            "document_received_at": pd.to_datetime(
                ["2026-01-03T19:01:00Z"] * 2,
                utc=True,
            ),
            "extraction_completed_at": pd.to_datetime(
                ["2026-01-03T19:02:00Z"] * 2,
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-01-03T19:02:00Z"] * 2,
                utc=True,
            ),
            "event_type": ["DILUTION", "OFFERING"],
            "calculation_version": ["1.0.0", "1.0.0"],
            "extraction_quality_pass": [True, True],
            "dilution_event": [1.0, 0.0],
            "offering_size_to_market_cap": [None, 0.05],
            "filing_event_impulse": [0.6, 0.8],
        }
    )
    joined = load_sec_event_features(
        decisions,
        source,
    )

    assert pd.isna(joined.loc[0, "sec__filing_event_impulse"])
    assert joined.loc[1, "sec__dilution_event"] == 1.0
    assert joined.loc[1, "sec__offering_size_to_market_cap"] == 0.05
    assert joined.loc[1, "sec__filing_event_impulse"] == 0.8
    assert joined.loc[1, "sec__audit__sec_event_count"] == 2
    assert joined.loc[1, "sec__audit__sec_event_types"] == "DILUTION|OFFERING"
    assert pd.isna(joined.loc[2, "sec__filing_event_impulse"])
    assert joined.loc[2, "sec__join_status"] == "STALE"


def test_quality_controls_are_retained_but_excluded_from_model_projection() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:05:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "bid": [99.0],
            "ask": [101.0],
            "mid": [100.0],
            "relative_bid_ask_spread": [0.02],
        }
    )
    joined = load_quote_liquidity_features(
        decisions,
        source,
        horizon="1h",
    )
    assert "quote__available_at" in joined
    assert "quote__is_stale" in joined
    projected = model_value_projection(
        joined,
        ("quote__relative_bid_ask_spread",),
    )
    assert projected.columns.tolist() == [
        "quote__relative_bid_ask_spread"
    ]
    joined["target"] = 1.0
    with pytest.raises(MLContractError, match="decision identity"):
        model_value_projection(
            joined,
            ("quote__relative_bid_ask_spread",),
            include_keys=("target",),
        )


def test_quote_loader_rejects_unproven_negative_spread() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:01:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "relative_bid_ask_spread": [-0.01],
        }
    )

    with pytest.raises(MLContractError, match="physical bid/ask/mid"):
        load_quote_liquidity_features(
            decisions,
            source,
            horizon="1h",
        )


def test_lifecycle_rejects_availability_before_calculation_completion() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:05:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z"],
                utc=True,
            ),
            "calculation_completed_at": pd.to_datetime(
                ["2026-07-29T10:10:00Z"],
                utc=True,
            ),
            "timing_score": [0.5],
        }
    )

    with pytest.raises(
        MLContractError,
        match="precedes calculation_completed_at",
    ):
        load_lifecycle_features(
            decisions,
            source,
            horizon="1h",
            value_columns={"life__timing_score": "timing_score"},
        )


def test_lifecycle_later_availability_revision_changes_only_later_decisions() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:05:00Z", "2026-07-29T11:05:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "timestamp": pd.to_datetime(
                ["2026-07-28T20:00:00Z"] * 2,
                utc=True,
            ),
            "constituent_available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z", "2026-07-29T11:00:00Z"],
                utc=True,
            ),
            "calculated_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z", "2026-07-29T11:00:00Z"],
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z", "2026-07-29T11:00:00Z"],
                utc=True,
            ),
            "calculation_version": ["1.0.0", "1.0.0"],
            "provider_policy_version": [
                "canonical-databento-daily-v1",
                "canonical-databento-daily-v1",
            ],
            "constituent_complete": [True, True],
            "timing_score": [0.4, 0.8],
        }
    )

    joined = load_lifecycle_features(
        decisions,
        source,
        horizon="1h",
        value_columns={"life__timing_score": "timing_score"},
    )

    assert joined["life__timing_score"].tolist() == [0.4, 0.8]
    assert joined["life__available_at"].tolist() == list(
        source["available_at"]
    )


def test_energy_context_uses_actual_availability_and_breaks_on_instrument_change() -> None:
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T10:15:00Z", "2026-07-29T10:45:00Z"],
                utc=True,
            ),
        }
    )
    source = pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                ["2026-07-29T10:00:00Z", "2026-07-29T10:30:00Z"],
                utc=True,
            ),
            "canonical_instrument": ["CLUSD", "USO"],
            "instrument_changed": [False, True],
            "return_chain_complete": [True, False],
            "wti_or_proxy_return": [0.01, 0.50],
        }
    )
    joined = load_energy_context_features(
        decisions,
        source,
        horizon="1h",
    )

    assert joined.loc[0, "energy__wti_or_proxy_return"] == 0.01
    assert joined.loc[0, "energy__available_at"] == pd.Timestamp(
        "2026-07-29T10:00:00Z"
    )
    assert pd.isna(joined.loc[1, "energy__wti_or_proxy_return"])
    assert joined.loc[1, "energy__join_status"] == "QUALITY_REJECTED"
    assert (
        joined.loc[1, "energy__audit_canonical_instrument"]
        == "USO"
    )


def test_bar_shape_wrapper_requires_completed_exact_source() -> None:
    decisions = _exact_decision()
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "provider": ["databento"],
            "timeframe": ["1h"],
            "bar_timestamp": pd.to_datetime(
                ["2026-07-29T14:00:00Z"],
                utc=True,
            ),
            "available_at": pd.to_datetime(
                ["2026-07-29T15:05:00Z"],
                utc=True,
            ),
            "bar_complete": [False],
            "calculation": ["bar-shape"],
            "calculation_version": ["1.0.0"],
            "close_location": [0.5],
        }
    )
    joined = load_bar_shape_features(
        decisions,
        source,
        value_columns={"bar__close_location": "close_location"},
    )
    assert joined.loc[0, "bar__join_status"] == "QUALITY_REJECTED"
    assert pd.isna(joined.loc[0, "bar__close_location"])


def test_bar_shape_accepts_minimal_loop_b_decision_key() -> None:
    decision_at = pd.Timestamp("2026-07-29T15:05:00Z")
    decisions = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "horizon": ["1H"],
            "decision_timestamp": [decision_at],
        }
    )
    source = pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "provider": ["databento"],
            "timeframe": ["1h"],
            "bar_timestamp": [pd.Timestamp("2026-07-29T14:00:00Z")],
            "bar_end_timestamp": [pd.Timestamp("2026-07-29T15:00:00Z")],
            "available_at": [decision_at],
            "bar_complete": [True],
            "calculation": ["bar-shape"],
            "calculation_version": ["1.0.0"],
            "close_location": [0.5],
        }
    )

    joined = load_bar_shape_features(
        decisions,
        source,
        value_columns={"bar__close_location": "close_location"},
    )

    assert joined.loc[0, "bar__close_location"] == 0.5
    assert joined.loc[0, "bar__join_status"] == "JOINED"
    assert joined.loc[0, "bar__audit_bar_timestamp"] == pd.Timestamp(
        "2026-07-29T14:00:00Z"
    )


def _exact_decision() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["NVDA"],
            "provider": ["databento"],
            "timeframe": ["1h"],
            "bar_timestamp": pd.to_datetime(
                ["2026-07-29T14:00:00Z"],
                utc=True,
            ),
            "decision_timestamp": pd.to_datetime(
                ["2026-07-29T15:05:00Z"],
                utc=True,
            ),
        }
    )
