"""Offline field-state, quiet-session and reconnect regressions."""
from copy import deepcopy
import json

import pytest

from ml.intraday.market_context import (build_market_context, market_context_contract,
    PREVIOUS_FEATURE_VERSION, LEGACY_FEATURE_VERSION, snapshot_digest)
from ml.intraday.market_state import confirm_snapshot, StreamState, session_status
from ml.intraday.market_collection_schwab_stream import normalize

NOW = "2026-10-16T14:15:10+00:00"
OLD = "2026-10-16T12:00:00+00:00"


def quote(**changes):
    return {"symbol": "DOCU", "event_at": OLD, "quote_timestamp": OLD,
        "trade_timestamp": "2026-10-15T18:00:00+00:00", "bid": 99., "ask": 101.,
        "last": 100.5, "bid_size": 3, "ask_size": 1, **changes}


def snapshot(rows, **changes):
    value = {"schema_version": "intraday-market-snapshot-v1", "snapshot_id": "test",
        "provider": "schwab", "source_id": "test:quotes", "source_kind": "stock_quote",
        "symbol": "DOCU", "schema": "quotes", "receipt_at": NOW, "available_at": NOW,
        "status": "COMPLETE", "records": rows, **changes}
    value["snapshot_sha256"] = snapshot_digest(value)
    return value


def context(row, **kwargs):
    return build_market_context([snapshot([row])], symbol="DOCU", cutoff=NOW, **kwargs)


def test_authoritative_unchanged_quote_is_current_and_trade_remains_old():
    row = confirm_snapshot(quote(), confirmed_at=NOW)
    result = context(row)
    assert result.features["ctx_stock_quote_relative_spread"] == .02
    assert result.features["ctx_stock_quote_quote_change_age_seconds"] == 8110
    assert result.features["ctx_stock_quote_last_trade_age_seconds"] == 72910
    assert result.features["ctx_stock_quote_last_mid_return"] == pytest.approx(.005)
    assert result.features["ctx_stock_quote_state_confirmation_age_seconds"] == 0
    assert result.features["ctx_stock_quote_market_session_open"] == 1
    assert row["event_at"] == row["quote_timestamp"] == OLD


@pytest.mark.parametrize("version", [LEGACY_FEATURE_VERSION, PREVIOUS_FEATURE_VERSION])
def test_previous_contracts_keep_previous_replay_semantics(version):
    result = context(confirm_snapshot(quote(), confirmed_at=NOW), contract=market_context_contract(version=version))
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert "ctx_stock_quote_state_confirmation_age_seconds" not in result.features


def test_recent_trade_does_not_refresh_old_quote_and_old_trade_does_not_stale_quote():
    row = quote(event_at=NOW, trade_timestamp=NOW)
    result = context(row)
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert result.features["ctx_stock_quote_last_trade_age_seconds"] == 0
    fresh = context(quote(event_at=NOW, quote_timestamp=NOW))
    assert fresh.features["ctx_stock_quote_relative_spread_missing"] == 0
    assert fresh.features["ctx_stock_quote_last_trade_age_seconds"] > 72000


def test_future_trade_field_does_not_discard_valid_confirmed_quote():
    result = context(confirm_snapshot(quote(trade_timestamp="2026-10-16T15:00:00Z"), confirmed_at=NOW))
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 0
    assert result.features["ctx_stock_quote_last_mid_return_missing"] == 1
    assert result.features["ctx_stock_quote_last_trade_age_seconds_missing"] == 1


@pytest.mark.parametrize("state", [["invalid"], {"version": "intraday-field-market-state-v1",
    "basis": "authoritative_current_snapshot", "confirmed_at": NOW, "field_confirmed_at": ["invalid"]}])
def test_malformed_state_is_explicitly_excluded_without_aborting_other_sources(state):
    result = context(quote(market_state=state))
    assert result.features["ctx_stock_quote_source_available"] == 0
    assert result.source_evidence[0]["exclusions"] == {"invalid_market_state": 1}


@pytest.mark.parametrize("underlying_clock", ["2026-10-16T15:00:00Z", "unknown"])
def test_future_underlying_clock_removes_greeks_but_retains_option_quote(underlying_clock):
    row = confirm_snapshot(quote(underlying_symbol="DOCU", contract_symbol="CONTRACT",
        implied_volatility=.5, delta=.6, underlying_quote_timestamp=underlying_clock), confirmed_at=NOW)
    value = snapshot([row], source_kind="options", schema="chain-greeks-full")
    result = build_market_context([value], symbol="DOCU", cutoff=NOW)
    assert result.features["ctx_options_relative_spread_median_missing"] == 0
    assert result.features["ctx_options_delta_median_missing"] == 1
    assert result.features["ctx_options_iv_median_missing"] == 1


def test_confirmation_is_field_scoped_and_future_or_stale_confirmation_is_not_current():
    row = confirm_snapshot(quote(), confirmed_at=NOW, fields=["ask"])
    assert context(row).features["ctx_stock_quote_relative_spread_missing"] == 1
    for when in (OLD, "2026-10-16T14:16:00Z"):
        assert context(confirm_snapshot(quote(), confirmed_at=when)).features["ctx_stock_quote_relative_spread_missing"] == 1


def test_missing_event_timestamp_is_not_invented_for_current_state():
    row = confirm_snapshot(quote(event_at=None, quote_timestamp=None, trade_timestamp=None), confirmed_at=NOW)
    result = context(row)
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 0
    assert result.features["ctx_stock_quote_source_age_seconds_missing"] == 1
    assert result.features["ctx_stock_quote_last_trade_age_seconds_missing"] == 1
    assert row["event_at"] is None


def test_closed_session_current_confirmation_and_empty_result_are_different():
    saturday = "2026-10-17T14:15:10+00:00"
    row = confirm_snapshot(quote(), confirmed_at=saturday)
    snap = snapshot([row], receipt_at=saturday, available_at=saturday)
    result = build_market_context([snap], symbol="DOCU", cutoff=saturday)
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 0
    assert result.features["ctx_stock_quote_market_session_open"] == 0
    assert result.source_evidence[0]["field_state"]["session"]["status"] == "CLOSED"
    empty = build_market_context([snapshot([], status="EMPTY")], symbol="DOCU", cutoff=NOW)
    assert empty.features["ctx_stock_quote_source_available"] == 0
    assert session_status(NOW, kind="cross_asset", schema="quote")["status"] == "UNKNOWN"


def native(fields, *, receipt=NOW, service="LEVELONE_EQUITIES"):
    return normalize(service, {"key": "DOCU", **fields}, receipt_at=receipt, scope={"DOCU": "DOCU"})


def test_stream_ack_or_one_sided_delta_never_establishes_baseline():
    state = StreamState("one")
    assert state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=True) == {}
    state.observe("LEVELONE_EQUITIES", native({"1": 99}))
    state.observe("LEVELONE_EQUITIES", native({"2": 101}))
    assert state.states == {}


def test_incomplete_updates_merge_only_after_baseline_and_reconnect_requires_new_baseline():
    state = StreamState("one")
    state.observe("LEVELONE_EQUITIES", native({"1": 99, "2": 101, "34": 1792152000000}, receipt=OLD))
    state.observe("LEVELONE_EQUITIES", native({"2": 102}))
    saved = state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=True)["LEVELONE_EQUITIES"][0]
    assert saved["bid"] == 99 and saved["ask"] == 102
    assert saved["market_state"]["field_confirmed_at"]["bid"] == OLD
    assert saved["market_state"]["field_confirmed_at"]["ask"] == NOW
    assert saved["market_state"]["exchange_sequence_verified"] is False
    assert context(saved).features["ctx_stock_quote_relative_spread_missing"] == 0
    assert context(saved).features["ctx_stock_quote_state_confirmation_age_seconds"] == 8110
    assert context(saved).features["ctx_stock_quote_state_continuity_age_seconds"] == 0
    restarted = StreamState("two")
    restarted.observe("LEVELONE_EQUITIES", native({"2": 102}))
    assert restarted.states == {}
    restarted.observe("LEVELONE_EQUITIES", native({"1": 99, "2": 102}))
    assert restarted.states


def test_unchanged_known_baseline_can_continue_but_disconnect_does_not_extend_it():
    state = StreamState("one")
    state.observe("LEVELONE_EQUITIES", native({"1": 99, "2": 101}, receipt=OLD))
    unproven = state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=True)["LEVELONE_EQUITIES"][0]
    assert context(unproven).features["ctx_stock_quote_relative_spread_missing"] == 1
    established = state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=True,
        transport_confirmed_at=NOW)["LEVELONE_EQUITIES"][0]
    assert context(established).features["ctx_stock_quote_relative_spread_missing"] == 0
    legacy = deepcopy(established)
    legacy['market_state'].pop('last_validated_transport_at')
    assert context(legacy).features["ctx_stock_quote_relative_spread_missing"] == 1
    interrupted = state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=False)["LEVELONE_EQUITIES"][0]
    assert context(interrupted).features["ctx_stock_quote_relative_spread_missing"] == 1


def test_option_native_and_state_projection_are_counted_once_and_state_wins():
    from math import log1p
    base = quote(underlying_symbol="DOCU", contract_symbol="CONTRACT", option_type="call",
        event_at=NOW, quote_timestamp=NOW, trade_timestamp=OLD, sequence=2)
    projected = confirm_snapshot({**base, "ask": 102}, confirmed_at=NOW)
    value = snapshot([projected, base], source_kind="options", schema="LEVELONE_OPTIONS")
    result = build_market_context([value], symbol="DOCU", cutoff=NOW)
    assert result.features["ctx_options_log_contract_count"] == log1p(1)
    assert result.features["ctx_options_relative_spread_median"] == pytest.approx(3/100.5)


def test_book_side_clear_is_explicit_and_unknown_side_does_not_erase_known_side():
    state = StreamState("one")
    state.observe("NASDAQ_BOOK", native({"1": 1792152000000, "2": [{"0": 99, "1": 10}],
        "3": [{"0": 101, "1": 12}]}, service="NASDAQ_BOOK"))
    state.observe("NASDAQ_BOOK", native({"1": 1792152000001, "2": []}, service="NASDAQ_BOOK"))
    row = state.checkpoint({"NASDAQ_BOOK": ["DOCU"]}, ended_at=NOW, healthy=False)["NASDAQ_BOOK"][0]
    assert "bid" not in row and row["bid_depth_levels"] == 0 and row["ask"] == 101
    assert row["market_state"]["connection_health"] == "interrupted"


def test_explicit_invalid_field_clears_previous_value_without_erasing_other_side():
    state = StreamState("one")
    state.observe("LEVELONE_EQUITIES", native({"1": 99, "2": 101}))
    state.observe("LEVELONE_EQUITIES", native({"2": None}))
    row = state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=True)["LEVELONE_EQUITIES"][0]
    assert row["bid"] == 99 and "ask" not in row
    assert "ask" not in row["market_state"]["field_confirmed_at"]


def test_final_cleared_state_does_not_fall_back_to_old_complete_native_quote():
    state = StreamState("one")
    baseline = native({"1": 99, "2": 101})
    state.observe("LEVELONE_EQUITIES", baseline)
    cleared = native({"2": None})
    state.observe("LEVELONE_EQUITIES", cleared)
    row = state.checkpoint({"LEVELONE_EQUITIES": ["DOCU"]}, ended_at=NOW, healthy=True)["LEVELONE_EQUITIES"][0]
    result = build_market_context([snapshot([baseline, cleared, row])], symbol="DOCU", cutoff=NOW)
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert result.features["ctx_stock_quote_quote_state_current"] == 0
    assert result.source_evidence[0]["records_used"] == 1


def test_option_null_estimates_and_volume_clear_while_absent_fields_continue():
    state = StreamState("one")
    state.observe("LEVELONE_OPTIONS", native({"2": 99, "3": 101, "10": 50, "28": .5,
        "29": .01, "8": 10, "9": 15}, service="LEVELONE_OPTIONS"))
    state.observe("LEVELONE_OPTIONS", native({"10": None, "28": None, "8": None, "9": None}, service="LEVELONE_OPTIONS"))
    row = state.checkpoint({"LEVELONE_OPTIONS": ["DOCU"]}, ended_at=NOW, healthy=True)["LEVELONE_OPTIONS"][0]
    assert row['gamma'] == .01
    for name in ('implied_volatility', 'delta', 'day_volume', 'open_interest'):
        assert name not in row and name not in row['market_state']['field_confirmed_at']


@pytest.mark.parametrize('bad_clock', ['2026-10-16T15:00:00Z', 'unknown'])
def test_future_quote_clock_suppresses_quote_without_erasing_known_last_trade(bad_clock):
    result = context(confirm_snapshot(quote(event_at=bad_clock,
        quote_timestamp=bad_clock), confirmed_at=NOW))
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 1
    assert result.features["ctx_stock_quote_quote_state_current"] == 0
    assert result.features["ctx_stock_quote_last_trade_age_seconds"] > 72000
    assert result.features["ctx_stock_quote_source_age_seconds_missing"] == 1


@pytest.mark.parametrize('bad_clock', ['2026-10-16T15:00:00Z', 'unknown'])
def test_future_selected_trade_event_retains_independently_confirmed_quote(bad_clock):
    row = confirm_snapshot(quote(event_at=bad_clock,
        trade_timestamp=bad_clock), confirmed_at=NOW)
    result = context(row)
    assert result.features["ctx_stock_quote_relative_spread_missing"] == 0
    assert result.features["ctx_stock_quote_last_mid_return_missing"] == 1
    assert result.features["ctx_stock_quote_source_age_seconds_missing"] == 1


def test_received_market_response_is_not_hidden_by_one_future_field_clock(tmp_path):
    from datetime import datetime, timezone
    from ml.intraday.market_collection import QuarterHourMarketCollector, SourceRequest, CollectionResult
    request = SourceRequest('schwab', 'stock_quote', 'DOCU', 'fixture', 'quote', 'quotes', {})
    row = confirm_snapshot(quote(event_at="2026-10-16T15:00:00Z",
        quote_timestamp="2026-10-16T15:00:00Z"), confirmed_at=NOW)
    collector = QuarterHourMarketCollector(tmp_path, symbols=['DOCU'], requests=[request],
        adapters={'schwab': lambda *args: CollectionResult([row])},
        clock=lambda: datetime(2026,10,16,14,15,10,tzinfo=timezone.utc))
    receipt = collector.collect(max_requests=1)[0]
    assert receipt['available_at'] == receipt['receipt_at'] == NOW
    assert receipt['records'][0]['event_at'] == "2026-10-16T15:00:00Z"
    result = build_market_context([receipt], symbol='DOCU', cutoff=NOW)
    assert result.features['ctx_stock_quote_last_trade_age_seconds_missing'] == 0


def test_derived_cache_recomputes_when_implementation_changes_without_contract_change(tmp_path, monkeypatch):
    import ml.intraday.market_context_cache as caching
    path = tmp_path/'context.sqlite3'
    receipt = snapshot([confirm_snapshot(quote(), confirmed_at=NOW)])
    monkeypatch.setattr(caching, '_implementation_digest', lambda: 'first-implementation')
    with caching.ContextCache(path) as cache:
        first = cache.build([receipt], symbol='DOCU', cutoff=NOW)
    monkeypatch.setattr(caching, '_implementation_digest', lambda: 'corrected-implementation')
    with caching.ContextCache(path) as cache:
        assert cache.build([receipt], symbol='DOCU', cutoff=NOW) == first
        assert (cache.hits, cache.misses) == (0, 1)


def test_derived_cache_aborts_if_implementation_mutates_mid_transaction(tmp_path, monkeypatch):
    import ml.intraday.market_context_cache as caching
    from ml.intraday.prediction import ContractError
    monkeypatch.setattr(caching, '_implementation_digest', lambda: 'first-implementation')
    with pytest.raises(ContractError, match='implementation changed'):
        with caching.ContextCache(tmp_path/'context.sqlite3') as cache:
            cache.build([snapshot([confirm_snapshot(quote(), confirmed_at=NOW)])], symbol='DOCU', cutoff=NOW)
            monkeypatch.setattr(caching, '_implementation_digest', lambda: 'changed-during-build')
