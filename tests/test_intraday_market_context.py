"""Offline causal receipt, source-population and feature-contract regressions."""
from copy import deepcopy
from datetime import datetime, timezone
import math

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import Dataset
from ml.intraday.market_context import (
    CONTEXT_FEATURE_NAMES, FEATURE_VERSION, LEGACY_FEATURE_VERSION, VALUE_FEATURE_NAMES,
    augment_dataset, build_market_context, market_context_contract,
    snapshot_digest, validate_market_context_contract,
)
from ml.intraday.prediction import ContractError


CUTOFF = "2026-10-16T14:15:10+00:00"


def sealed(kind="stock_quote", rows=None, **changes):
    value = {
        "schema_version": "intraday-market-snapshot-v1", "snapshot_id": "receipt-1",
        "source_kind": kind, "source_id": "fixture:" + kind,
        "symbol": "DOCU", "provider": "fixture", "dataset": "fixture", "schema": kind,
        "quarter_start": "2026-10-16T14:00:00+00:00", "quarter_end": "2026-10-16T14:15:00+00:00",
        "requested_start": "2026-10-16T14:00:00+00:00", "requested_end": "2026-10-16T14:15:00+00:00",
        "receipt_at": "2026-10-16T14:15:05+00:00", "available_at": "2026-10-16T14:15:05+00:00",
        "status": "COMPLETE", "records": rows if rows is not None else [quote()],
    }
    value.update(changes)
    value["snapshot_sha256"] = snapshot_digest(value)
    return value


def quote(**changes):
    return {"symbol": "DOCU", "event_at": "2026-10-16T14:14:59+00:00",
            "bid": 99.0, "ask": 101.0, "last": 100.5, "bid_size": 30, "ask_size": 10, **changes}


def context(*snapshots, **kwargs):
    return build_market_context(snapshots, symbol="DOCU", cutoff=CUTOFF, **kwargs)


def test_real_quote_calculation_and_missingness_are_separate_from_prices():
    item = context(sealed())
    f = item.features
    assert f["ctx_stock_quote_relative_spread"] == pytest.approx(.02)
    assert f["ctx_stock_quote_depth_imbalance"] == .5
    assert f["ctx_stock_quote_last_mid_return"] == pytest.approx(.005)
    assert f["ctx_stock_quote_source_age_seconds"] == 11
    assert f["ctx_stock_quote_receipt_age_seconds"] == 5
    assert f["ctx_stock_quote_relative_spread_missing"] == 0
    assert f["ctx_stock_trades_price_return_missing"] == 1
    assert all(math.isfinite(x) for x in item.values)
    assert item.feature_names == CONTEXT_FEATURE_NAMES


def test_late_receipt_cannot_be_backdated_to_quarter_or_provider_event():
    late = sealed(receipt_at="2026-10-16T14:16:00+00:00", available_at="2026-10-16T14:16:00+00:00")
    assert context(late) == context()
    late["records"][0]["last"] = 1000000  # ignored future bytes cannot change historical fingerprint
    assert context(late).snapshot_sha256 == context().snapshot_sha256


def test_one_nanosecond_future_event_or_receipt_cannot_cross_cutoff():
    future = "2026-10-16T14:15:10.000000001+00:00"
    item = context(sealed(rows=[quote(event_at=future)]))
    assert item.features["ctx_stock_quote_source_available"] == 0
    assert item.source_evidence[0]["exclusions"] == {"future_row_clock": 1}
    future_receipt = sealed(receipt_at=future, available_at=future)
    assert context(future_receipt).snapshot_sha256 == context().snapshot_sha256
    exact = context(sealed(rows=[quote(event_at=CUTOFF)]))
    assert exact.features["ctx_stock_quote_source_available"] == 1


@pytest.mark.parametrize("row_flags,evidence", [
    ({"is_delayed": True}, {"mode": "current_stream_snapshot"}),
    ({}, {"mode": "current_rest_snapshot", "is_delayed": True}),
    ({}, {"mode": "current_rest_snapshot", "realtime": False}),
])
def test_known_delayed_provider_data_is_unavailable_not_relabelled_realtime(row_flags, evidence):
    item = context(sealed(rows=[quote(**row_flags)], evidence=evidence))
    assert item.features["ctx_stock_quote_source_available"] == 0
    assert item.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert item.source_evidence[0]["exclusions"] == {"provider_delayed": 1}


def test_delayed_update_cannot_make_an_older_success_appear_current():
    older = sealed(snapshot_id="older")
    delayed = sealed(snapshot_id="latest", receipt_at=CUTOFF, available_at=CUTOFF,
                     evidence={"mode": "current_rest_snapshot", "is_delayed": True})
    item = context(older, delayed)
    assert item.features["ctx_stock_quote_source_available"] == 0
    assert item.source_evidence[0]["exclusions"] == {"provider_delayed": 1}


@pytest.mark.parametrize("field", ["event_at", "available_at"])
def test_future_row_clock_is_excluded_independently(field):
    item = context(sealed(rows=[quote(**{field: "2026-10-16T14:16:00+00:00"})]))
    assert item.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert item.source_evidence[0]["exclusions"] == {"future_row_clock": 1}


def test_receipt_bounds_invalid_timezone_and_integrity_fail_closed():
    with pytest.raises(ContractError, match="precede actual receipt"):
        context(sealed(available_at="2026-10-16T14:15:00+00:00"))
    with pytest.raises(ContractError, match="timestamps"):
        context(sealed(receipt_at="2026-10-16T14:15:05"))
    tampered = sealed()
    tampered["records"][0]["bid"] = 97
    with pytest.raises(ContractError, match="hash mismatch"):
        context(tampered)


def test_duplicates_are_idempotent_and_conflicting_source_rows_are_rejected():
    item = sealed()
    assert context(item, deepcopy(item)) == context(item)
    double = context(sealed(rows=[quote(), quote()]))
    assert double.features == context(item).features
    with pytest.raises(ContractError, match="Conflicting market observations"):
        context(sealed(rows=[quote(), quote(bid=98)]))
    native_updates = context(sealed(rows=[quote(bid=98, raw={"sequence": 2}), quote(raw={"sequence": 10})]))
    assert native_updates.features["ctx_stock_quote_relative_spread"] == pytest.approx(.02)


def test_legacy_latest_source_error_and_partial_never_enter_successful_features():
    good = sealed()
    for status in ("ERROR", "PARTIAL", "CAPACITY_BLOCKED", "NOT_AUTHORIZED", "UNPROBED"):
        bad = sealed(snapshot_id="second", receipt_at=CUTOFF, available_at=CUTOFF, status=status)
        item = context(good, bad, contract=market_context_contract(version=LEGACY_FEATURE_VERSION))
        assert item.features["ctx_stock_quote_relative_spread_missing"] == 1
        assert item.source_evidence[0]["status"] == status
        assert item.features["ctx_stock_quote_source_partial"] == float(status in {"PARTIAL", "CAPACITY_BLOCKED"})


def test_current_rest_trade_is_causal_at_receipt_not_reassigned_to_closed_quarter():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:15:03+00:00",
           "price": 100, "size": 5, "trade_id": "rest-trade-1"}
    bounded = sealed("stock_trades", [row])
    assert context(bounded).features["ctx_stock_trades_source_available"] == 0
    current = sealed("stock_trades", [row], evidence={"mode": "current_rest_snapshot"})
    value = context(current)
    assert value.features["ctx_stock_trades_source_available"] == 1
    assert value.features["ctx_stock_trades_log_observed_volume"] == pytest.approx(math.log1p(5))
    assert build_market_context([current], symbol="DOCU", cutoff="2026-10-16T14:15:04+00:00") == build_market_context([], symbol="DOCU", cutoff="2026-10-16T14:15:04+00:00")
    future = sealed("stock_trades", [{**row, "event_at": "2026-10-16T14:16:00+00:00"}], evidence={"mode": "current_rest_snapshot"})
    assert context(future).features["ctx_stock_trades_source_available"] == 0


def test_current_rest_mode_never_relaxes_bar_completion_window():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:15:00+00:00",
           "open": 100, "high": 102, "low": 99, "close": 101, "volume": 5}
    current = sealed("stock_bars", [row], schema="ohlcv-1m", evidence={"mode": "current_rest_snapshot"})
    assert context(current).features["ctx_stock_bars_source_available"] == 0


def test_current_stream_chart_keeps_only_finished_real_bars_at_receipt():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:15:00+00:00",
           "open": 100, "high": 102, "low": 99, "close": 101, "volume": 5, "bar_duration_seconds": 60}
    receipt = sealed("stock_bars", [row], schema="CHART_EQUITY", evidence={"mode": "current_stream_snapshot"})
    assert context(receipt).features["ctx_stock_bars_source_available"] == 0
    finished = sealed("stock_bars", [row], schema="CHART_EQUITY", evidence={"mode": "current_stream_snapshot"},
                      receipt_at="2026-10-16T14:16:02+00:00", available_at="2026-10-16T14:16:02+00:00")
    value = build_market_context([finished], symbol="DOCU", cutoff="2026-10-16T14:16:05+00:00")
    assert value.features["ctx_stock_bars_price_return"] == pytest.approx(.01)


def test_provider_ambiguity_requires_frozen_selector_and_wrong_symbols_are_ignored():
    left, right = sealed(), sealed(source_id="another:quote", snapshot_id="second")
    item = context(left, right)
    assert item.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert item.source_evidence[0]["status"] == "AMBIGUOUS_SOURCE"
    assert context(left, right, source_ids={"stock_quote": left["source_id"]}).features == context(left).features
    assert context(sealed(symbol="PYPL")) == context()
    assert context(sealed(rows=[quote(symbol="PYPL")])).features["ctx_stock_quote_relative_spread_missing"] == 1


def test_stale_events_and_crossed_quotes_are_not_fabricated_current_prices():
    old = sealed(rows=[quote(event_at="2026-10-16T13:00:00+00:00")])
    assert context(old).features["ctx_stock_quote_relative_spread_missing"] == 1
    assert context(sealed(rows=[quote(ask=98)])).features["ctx_stock_quote_relative_spread_missing"] == 1
    absent = context(sealed(rows=[], status="EMPTY"))
    assert absent.features["ctx_stock_quote_source_available"] == 0
    assert absent.features["ctx_stock_quote_last_mid_return_missing"] == 1


def test_source_trades_need_identity_and_distinct_trades_at_same_time_count():
    common = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:00+00:00", "price": 100, "size": 7}
    item = context(sealed("stock_trades", [{**common, "sequence": 1}, {**common, "sequence": 2}, common]))
    assert item.features["ctx_stock_trades_log_observation_count"] == pytest.approx(math.log1p(2))
    assert item.features["ctx_stock_trades_log_observed_volume"] == pytest.approx(math.log1p(14))
    assert item.source_evidence[1]["exclusions"] == {"missing_observation_identity": 1}


def test_options_use_latest_per_contract_and_keep_opra_population_separate():
    base = {"underlying_symbol": "DOCU", "symbol": "DOCU", "event_at": "2026-10-16T14:14:00+00:00"}
    call = {**base, "contract_symbol": "DOCU-C100", "option_type": "CALL", "volume": 10,
            "implied_volatility": .3, "delta": .5, "gamma": .02, "theta": -.1, "vega": .2}
    put = {**base, "contract_symbol": "DOCU-P100", "option_type": "PUT", "volume": 30,
           "implied_volatility": .5, "delta": -.5, "gamma": .04, "theta": -.3, "vega": .4}
    item = context(sealed("options", [call, put]))
    assert item.features["ctx_options_iv_median"] == pytest.approx(.4)
    assert item.features["ctx_options_delta_median"] == 0
    assert item.features["ctx_options_put_call_volume_log_ratio"] == pytest.approx(math.log1p(30)-math.log1p(10))
    assert item.features["ctx_opra_options_iv_median_missing"] == 1


def test_fundamentals_require_publication_and_actual_receipt_not_fiscal_date():
    row = {"symbol": "DOCU", "event_at": "2026-10-01T15:00:00+00:00",
           "published_at": "2026-10-01T15:00:00+00:00", "period_end": "2026-06-30", "pe": 20,
           "debtToEquity": .3, "marketCap": 1_000_000}
    item = context(sealed("fundamentals", [row]))
    assert item.features["ctx_fundamentals_earnings_yield"] == .05
    assert item.features["ctx_fundamentals_log_market_cap"] == pytest.approx(math.log1p(1_000_000))
    future = context(sealed("fundamentals", [{**row, "published_at": "2026-10-17T00:00:00+00:00"}]))
    assert future.features["ctx_fundamentals_earnings_yield_missing"] == 1
    unknown = context(sealed("fundamentals", [{**row, "publication_time_basis": "receipt_only"}]))
    assert unknown.features["ctx_fundamentals_earnings_yield_missing"] == 1
    future_period = context(sealed("fundamentals", [{**row, "period_end": "2027-06-30"}]))
    assert future_period.features["ctx_fundamentals_earnings_yield_missing"] == 1
    old = {**row, "pe": 5, "period_end": "2025-06-30"}
    assert context(sealed("fundamentals", [row, old])).features["ctx_fundamentals_earnings_yield"] == .05


def test_opra_incremental_trades_preserve_same_time_prints_and_sum_window_volume():
    base = {"symbol": "DOCU", "underlying_symbol": "DOCU", "event_at": "2026-10-16T14:14:00+00:00",
            "contract_symbol": "DOCU C", "option_type": "call", "volume": 10, "price": 2}
    rows = [{**base, "trade_id": "a"}, {**base, "trade_id": "b"},
            {**base, "trade_id": "c", "contract_symbol": "DOCU P", "option_type": "put", "volume": 30}]
    value = context(sealed("opra_options", rows + [rows[0]], schema="trades"))
    assert value.features["ctx_opra_options_log_contract_count"] == pytest.approx(math.log1p(2))
    assert value.features["ctx_opra_options_put_call_volume_log_ratio"] == pytest.approx(math.log1p(30)-math.log1p(20))
    outside = context(sealed("opra_options", [{**base, "trade_id": "z", "event_at": "2026-10-16T14:15:02+00:00"}], schema="trades"))
    assert outside.features["ctx_opra_options_source_available"] == 0


def test_opra_bar_close_cannot_enter_context_before_bar_completion():
    row = {"symbol": "DOCU", "underlying_symbol": "DOCU", "contract_symbol": "DOCU C",
           "event_at": "2026-10-16T14:14:00+00:00", "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 5}
    # Event opening alone does not prove the 1h close existed at 14:15.
    assert context(sealed("opra_options", [row], schema="ohlcv-1h")).features["ctx_opra_options_source_available"] == 0


def test_native_projection_preserves_observed_trade_count_return_and_volume():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:59+00:00", "first_event_at": "2026-10-16T14:00:01+00:00",
           "last_event_at": "2026-10-16T14:14:59+00:00", "first_price": 100, "last_price": 102,
           "price": 102, "size": 250000, "observed_trade_count": 120000, "trade_id": "projected-quarter"}
    receipt = sealed("stock_trades", [row], schema="trades",
                     evidence={"projection_version": "intraday-native-context-projection-v1"})
    value = context(receipt)
    assert value.features["ctx_stock_trades_price_return"] == pytest.approx(.02)
    assert value.features["ctx_stock_trades_log_observation_count"] == pytest.approx(math.log1p(120000))
    assert value.features["ctx_stock_trades_log_observed_volume"] == pytest.approx(math.log1p(250000))
    assert context(sealed("stock_trades", [row])).features["ctx_stock_trades_source_available"] == 0
    future_first = sealed("stock_trades", [{**row, "first_event_at": "2026-10-17T14:00:00+00:00"}],
                          evidence=receipt["evidence"])
    assert context(future_first).features["ctx_stock_trades_source_available"] == 0


def test_current_dcf_analyst_estimates_and_news_do_not_backdate_their_availability():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:15:05+00:00", "published_at": "2026-10-16T14:15:05+00:00",
           "publication_time_basis": "receipt_only", "dcf_value": 105, "dcf_upside_fraction": .05,
           "analyst_target_consensus": 110, "analyst_count": 12, "analyst_bullish_fraction": .75,
           "analyst_eps_estimate": -1.2, "analyst_revenue_estimate": 2e9,
           "analyst_estimate_period_end": "2027-12-31"}
    value = context(sealed("fundamentals", [row]))
    assert value.features["ctx_fundamentals_dcf_upside_fraction"] == .05
    assert value.features["ctx_fundamentals_analyst_eps_estimate"] == -1.2
    assert value.features["ctx_fundamentals_analyst_target_consensus"] == 110
    before = build_market_context([sealed("fundamentals", [row])], symbol="DOCU", cutoff="2026-10-16T14:15:04+00:00")
    assert before.features["ctx_fundamentals_dcf_value_missing"] == 1
    events = context(sealed("events", [{**row, "event_type": "stock_news"}, {**row, "event_type": "press_release"}]))
    assert events.features["ctx_events_log_observed_news_count"] == pytest.approx(math.log1p(1))
    assert events.features["ctx_events_log_observed_press_release_count"] == pytest.approx(math.log1p(1))
    nearer = {**row, "analyst_estimate_period_end": "2026-12-31", "analyst_eps_estimate": 2}
    past = {**row, "analyst_estimate_period_end": "2025-12-31", "analyst_eps_estimate": 999}
    chosen = context(sealed("fundamentals", [row, nearer, past]))
    assert chosen.features["ctx_fundamentals_analyst_eps_estimate"] == 2
    assert context(sealed("fundamentals", [past])).features["ctx_fundamentals_analyst_eps_estimate_missing"] == 1


def test_technical_requires_finished_frozen_period_and_preserves_missing_values():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:00:00+00:00", "bar_end_at": "2026-10-16T14:15:00+00:00",
           "technical_name": "rsi", "technical_value": 55, "period_length": 10, "timeframe": "15min"}
    value = context(sealed("technical", [row]))
    assert value.features["ctx_technical_rsi"] == 55
    assert value.features["ctx_technical_sma_missing"] == 1
    for change in ({"period_length": 20}, {"bar_end_at": "2026-10-16T14:30:00+00:00"}, {"technical_value": 101}):
        assert context(sealed("technical", [{**row, **change}])).features["ctx_technical_rsi_missing"] == 1


def test_macro_period_is_not_availability_and_units_must_be_explicit():
    row = {"symbol": "*", "event_at": "2026-10-16T14:15:05+00:00", "published_at": "2026-10-16T14:15:05+00:00",
           "publication_time_basis": "receipt_only", "macro_series": "GDP", "macro_value": 123,
           "macro_unit": "provider_GDP_unit", "macro_period_date": "2026-06-30"}
    value = context(sealed("macro", [row], symbol="*"))
    assert value.features["ctx_macro_gdp_value"] == 123
    for change in ({"macro_period_date": "2027-12-31"}, {"macro_unit": None}):
        assert context(sealed("macro", [{**row, **change}], symbol="*")).features["ctx_macro_gdp_value_missing"] == 1


def test_known_future_event_date_is_allowed_but_future_publication_is_not():
    event = {"id": "earnings-1", "symbol": "DOCU", "event_type": "earnings",
             "event_at": "2026-10-16T14:00:00+00:00", "published_at": "2026-10-16T14:00:00+00:00",
             "event_date": "2026-10-20T14:15:10+00:00"}
    assert context(sealed("events", [event])).features["ctx_events_next_earnings_days"] == 4


def test_cross_asset_returns_do_not_cross_contract_roll_or_use_foreign_equity():
    common = {"event_at": "2026-10-16T14:00:00+00:00", "open": 100, "high": 102, "low": 99,
              "close": 101, "volume": 3}
    good = {**common, "symbol": "ESZ6"}
    item = context(sealed("cross_asset", [good], symbol="*", schema="ohlcv-1m"))
    assert item.features["ctx_cross_asset_ES_return"] == pytest.approx(.01)
    rolled = context(sealed("cross_asset", [good, {**common, "symbol": "ESH7"}], symbol="*", schema="ohlcv-1m"))
    assert rolled.features["ctx_cross_asset_ES_return_missing"] == 1


def test_cross_asset_quotes_use_mid_and_order_or_statistics_prices_never_become_returns():
    rows = [{"symbol": "ESZ6", "event_at": "2026-10-16T14:00:00+00:00", "price": 10,
             "volume": 2, "bid": 99, "ask": 101},
            {"symbol": "ESZ6", "event_at": "2026-10-16T14:14:00+00:00", "price": 20,
             "volume": 3, "bid": 100, "ask": 102}]
    quote = context(sealed("cross_asset", rows, symbol="*", schema="mbp-1"))
    assert quote.features["ctx_cross_asset_ES_return"] == pytest.approx(.01)
    for schema in ("mbo", "statistics", "definition", "status", "commodity-quote"):
        item = context(sealed("cross_asset", rows, symbol="*", schema=schema))
        assert item.features["ctx_cross_asset_ES_return_missing"] == 1


def test_commodity_batch_uses_actual_native_quote_returns_without_futures_aliases():
    rows = [{"symbol": "BZUSD", "event_at": "2026-10-16T14:15:03+00:00", "open": 108,
             "raw": {"price": 109.86, "previousClose": 105.92, "changePercentage": 999}},
            {"symbol": "CLUSD", "event_at": "2026-10-16T14:15:04+00:00",
             "raw": {"price": 104, "previousClose": 100}},
            {"symbol": "FOREIGN", "event_at": "2026-10-16T14:15:04+00:00",
             "raw": {"price": 999, "previousClose": 1}}]
    item = sealed("cross_asset", rows, symbol="*", schema="commodity-quote",
                  evidence={"mode": "current_rest_snapshot", "configured_symbols": ["BZUSD", "CLUSD"]})
    result = context(item)
    returns = [109.86 / 105.92 - 1, .04]
    assert result.features["ctx_cross_asset_commodity_quote_return_median"] == pytest.approx(sum(returns) / 2)
    assert result.features["ctx_cross_asset_commodity_quote_return_dispersion"] == pytest.approx(abs(returns[0] - returns[1]) / 2)
    assert result.features["ctx_cross_asset_commodity_log_instrument_count"] == pytest.approx(math.log1p(2))
    assert result.features["ctx_cross_asset_CL_return_missing"] == 1
    assert build_market_context([item], symbol="DOCU", cutoff="2026-10-16T14:15:04+00:00").features["ctx_cross_asset_commodity_quote_return_median_missing"] == 1
    partial = {**item, "status": "PARTIAL", "snapshot_sha256": snapshot_digest({**item, "status": "PARTIAL"})}
    assert context(partial).features["ctx_cross_asset_commodity_quote_return_median_missing"] == 0
    assert context(partial, contract=market_context_contract(version=LEGACY_FEATURE_VERSION)).features["ctx_cross_asset_commodity_quote_return_median_missing"] == 1


@pytest.mark.parametrize("previous", [0, -1, None, "unavailable"])
def test_commodity_invalid_previous_close_is_missing_without_inferred_price(previous):
    rows = [{"symbol": "BZUSD", "event_at": "2026-10-16T14:15:03+00:00",
             "raw": {"price": 109.86, "previousClose": previous, "changePercentage": 3.71979}}]
    item = context(sealed("cross_asset", rows, symbol="*", schema="commodity-quote",
                  evidence={"mode": "current_rest_snapshot", "configured_symbols": ["BZUSD"]}))
    assert item.features["ctx_cross_asset_commodity_quote_return_median_missing"] == 1
    assert item.features["ctx_cross_asset_commodity_quote_return_dispersion_missing"] == 1


def test_end_stamped_bbo_uses_its_real_final_sample_but_never_a_future_sample():
    first = {"symbol": "ESZ6", "event_at": "2026-10-16T14:01:00+00:00", "interval_start": "2026-10-16T14:00:00+00:00",
             "interval_end": "2026-10-16T14:01:00+00:00", "bid": 99, "ask": 101}
    last = {**first, "event_at": "2026-10-16T14:15:00+00:00", "interval_start": "2026-10-16T14:14:00+00:00",
            "interval_end": "2026-10-16T14:15:00+00:00", "bid": 100, "ask": 102}
    item = context(sealed("cross_asset", [first, last], symbol="*", schema="bbo-1m"))
    assert item.features["ctx_cross_asset_ES_return"] == pytest.approx(.01)
    future = {**last, "event_at": "2026-10-16T14:16:00+00:00", "interval_start": "2026-10-16T14:15:00+00:00",
              "interval_end": "2026-10-16T14:16:00+00:00"}
    bad = context(sealed("cross_asset", [future], symbol="*", schema="bbo-1m"))
    assert bad.features["ctx_cross_asset_ES_return_missing"] == 1


def example_dataset():
    ref = pd.DatetimeIndex(["2026-10-16T13:30:00+00:00", "2026-10-16T13:45:00+00:00"])
    info = {"symbol": "DOCU", "reference_at": "2026-10-16T14:00:00+00:00",
            "features": [1.0], "snapshot": {"feature_version": "base-v1", "features": [1.0]},
            "information_available_at": "2026-10-16T14:15:00+00:00"}
    return Dataset(np.asarray([[1.0], [2.0]]), np.asarray([0, 1]), np.asarray([[1.0]]), ("base",),
                   ref+pd.Timedelta(minutes=45), ref, ref+pd.Timedelta(minutes=45), info,
                   {"feature_version": "base-v1", "feature_policy": "base-policy", "label_provenance": "base-labels"})


def test_augmentation_preserves_base_labels_and_cannot_backfill_training_from_inference_receipts():
    original = example_dataset()
    decisions = {ref: ref+pd.Timedelta(minutes=15) for ref in original.reference_at}
    value = augment_dataset(original, [sealed()], training_decision_at=decisions, inference_cutoff=CUTOFF)
    assert value.feature_names == ("base", *CONTEXT_FEATURE_NAMES)
    assert value.X.shape == (2, len(value.feature_names))
    assert np.array_equal(original.y, value.y)
    assert value.manifest["feature_version"] == "base-v1"
    assert value.manifest["feature_policy"] == "base-policy"
    assert value.manifest["label_provenance"] == "base-labels"
    assert value.manifest["market_context"]["training_rows_with_source"] == 0
    assert value.inference_X[0, value.feature_names.index("ctx_stock_quote_relative_spread")] == pytest.approx(.02)
    assert "market_context" not in original.manifest
    assert original.X.shape == (2, 1)
    with pytest.raises(ContractError, match="own decision"):
        augment_dataset(original, [], training_decision_at={}, inference_cutoff=CUTOFF)
    with pytest.raises(ContractError, match="precede target open"):
        augment_dataset(original, [], training_decision_at=dict(zip(original.reference_at, original.label_available_at)),
                        inference_cutoff=CUTOFF)


def test_frozen_contract_and_future_label_fields_cannot_change_feature_definition():
    contract = market_context_contract({"stock_quote": "fixture:stock_quote"})
    assert validate_market_context_contract(contract) == contract
    contract["feature_names"].reverse()
    with pytest.raises(ContractError, match="exact supported contract"):
        validate_market_context_contract(contract)
    base = context(sealed())
    with_label = context(sealed(rows=[quote(target_close=100000, label=1, outcome_available_at="2026-10-18T00:00:00Z")]))
    assert base.features == with_label.features


def test_multiple_frozen_sources_keep_independent_features_failure_and_clocks():
    sources = {"stock_quote": ["provider-B:quote", "provider-A:quote"]}
    contract = market_context_contract(sources)
    assert contract["source_ids"]["stock_quote"] == ["provider-A:quote", "provider-B:quote"]
    left = sealed(source_id="provider-A:quote")
    right = sealed(source_id="provider-B:quote", snapshot_id="receipt-B", status="ERROR")
    item = context(left, right, source_ids=sources)
    entries = [e for e in item.source_evidence if e["source_kind"] == "stock_quote"]
    assert len(entries) == 2
    assert item.feature_names == tuple(contract["feature_names"])
    assert item.features[entries[0]["feature_prefix"] + "_relative_spread"] == pytest.approx(.02)
    assert item.features[entries[1]["feature_prefix"] + "_relative_spread_missing"] == 1
    assert item == context(right, left, source_ids={"stock_quote": list(reversed(sources["stock_quote"]))})


def test_source_bar_close_must_be_complete_and_observation_inside_requested_window():
    row = {"symbol": "DOCU", "event_at": "2026-10-16T14:14:30+00:00", "open": 100,
           "high": 101, "low": 99, "close": 100.5, "volume": 10}
    late = context(sealed("stock_bars", [row], schema="ohlcv-1m"))
    assert late.features["ctx_stock_bars_price_return_missing"] == 1
    assert late.source_evidence[2]["exclusions"] == {"unfinished_source_bar": 1}
    outside = context(sealed("stock_bars", [{**row, "event_at": "2026-10-16T13:59:00+00:00"}], schema="ohlcv-1m"))
    assert outside.source_evidence[2]["exclusions"] == {"outside_requested_interval": 1}
