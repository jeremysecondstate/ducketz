"""Saved instructions execute without research artifacts, with all-symbol price comparisons."""
import json
from dataclasses import replace

import pandas as pd
import pytest

from test_independent_stock_runtime import environment, _decisions
from test_gameplan_direction_runtime import prepare
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS
from ml.stock_trader.gameplan_execution import load_execution_signals, execution_preflight
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


def test_saved_instructions_do_not_need_receipts_models_contract_hashes_or_planning_prices(tmp_path):
    run = tmp_path / 'ml/nightly-gameplan-runs/20260908T090000.000000Z'
    run.mkdir(parents=True)
    pointer = tmp_path / 'ml/nightly-gameplan-latest/run.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'current':{'run_path':run.relative_to(tmp_path).as_posix(), 'receipt_checksum_sha256':'obsolete'}}))
    rows = [dict(id=f'2026-09-08:{symbol}:1h@06:00', symbol=symbol, model_group='1h', direction='BULLISH',
                 calibrated_probability=.7, target_window_start='2026-09-08T13:00:00Z',
                 target_window_end='2026-09-08T14:00:00Z', execution_eligible=True, action_date='2026-09-08',
                 frozen_at='2026-09-08T09:00:00Z', target_contract_version='older-metadata') for symbol in STOCK_TRADER_SYMBOLS]
    pd.DataFrame(rows).to_parquet(run/'forecasts.parquet')
    assert execution_preflight(tmp_path, action_date=pd.Timestamp('2026-09-08').date())['status'] == 'READY'
    signals, sources = load_execution_signals(tmp_path, as_of='2026-09-08T13:50:00Z')
    assert set(symbol for symbol,_ in signals) == set(STOCK_TRADER_SYMBOLS)
    assert sources == ()
    assert all(pd.Timestamp(s.actionable_until) == pd.Timestamp('2026-09-08T14:00:00Z') for s in signals.values())
    assert load_execution_signals(tmp_path, as_of='2026-09-08T14:01:00Z')[0] == {}


@pytest.mark.parametrize('probability,side', [(.7,'BUY'),(.3,'SELL')])
def test_all_eleven_symbols_submit_live_prices_and_log_estimates_without_using_them_as_limits(environment, monkeypatch, probability, side):
    env = environment
    prepare(env, monkeypatch, probability=probability)
    prototype = env.signals['AAPL','1h']
    env.signals = {(s,'1h'):replace(prototype, symbol=s, prediction_id=f'{s}-instruction',
        actionable_until='2026-09-08T12:00:00Z', target_definition_version='old-metadata') for s in STOCK_TRADER_SYMBOLS}
    env.held = {s:1. for s in STOCK_TRADER_SYMBOLS}
    original = runtime.capture_portfolio_state
    def capture(*args, **kwargs):
        portfolio = original(*args, **kwargs)
        q = portfolio.quotes['AAPL']
        return replace(portfolio, quotes={s:replace(q, symbol=s, bid=90., ask=110.,
            observed_at=(env.now-pd.Timedelta(minutes=13)).isoformat(), received_at=env.now.isoformat(),
            realtime=True, quote_type='NBBO') for s in STOCK_TRADER_SYMBOLS})
    monkeypatch.setattr(runtime, 'capture_portfolio_state', capture)
    plan = env.root/'ml/gameplan-trade-plan-runs/planning'
    plan.mkdir(parents=True)
    pointer=env.root/'ml/gameplan-trade-plan-latest/run.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'current':{'run_path':plan.relative_to(env.root).as_posix()}}))
    pd.DataFrame([{'id':s.prediction_id,'trade_price_mid':10.,'cash_available_at_planning':0.,
                   'direction_based_trade_quantity':500.} for s in env.signals.values()]).to_parquet(plan/'trade-plan.parquet')
    options=dict(execute=True, session=env.broker, runtime_clock=lambda:env.now,
                 session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY)
    result=runtime.run_independent_stock_trader_once(env.root, **options)
    assert result.status == 'ORDERS_SUBMITTED', result.error
    assert result.submitted_orders == len(STOCK_TRADER_SYMBOLS) == 11
    for payload in env.broker.submissions:
        assert payload['orderLegCollection'][0]['instruction'] == side
        assert float(payload['price']) == 100.
    for decision in _decisions(result)['decisions']:
        comparison=decision['enrichment']['price_comparison']
        assert comparison['planned_price'] == 10.
        assert comparison['live_midpoint'] == 100.
        assert comparison['midpoint_minus_planned'] == 90.
        assert comparison['planned_cash'] == 0.
        assert comparison['quote_received_at'] == env.now.isoformat()
        assert comparison['comparison_affects_execution'] is False
    assert runtime.run_independent_stock_trader_once(env.root, **options).submitted_orders == 0
    assert len(env.broker.submissions) == 11


def test_quote_failure_retries_next_wake_without_recovery_flags_or_clearing_old_claims(environment, monkeypatch):
    from test_gameplan_quote_recovery import setup_quotes, execute
    env=environment
    setup_quotes(env, monkeypatch)
    env.signals={k:replace(s, actionable_until='2026-09-08T12:00:00Z') for k,s in env.signals.items()}
    old=env.root/'state/independent-stock-trader/entry-slots/20260908T110000Z.json'
    old.parent.mkdir(parents=True)
    old.write_text('{"previous_attempt":true}')
    assert execute(env, broker_state_retry_max_seconds=0).submitted_orders == 0
    env.now += pd.Timedelta(minutes=29)
    env.live_quote=True
    result=execute(env)
    assert result.status == 'ORDERS_SUBMITTED', result.error
    assert result.submitted_orders == 1
    assert execute(env).submitted_orders == 0
    assert old.read_text() == '{"previous_attempt":true}'


def test_consecutive_bullish_hours_close_filled_position_then_open_next_saved_forecast(environment, monkeypatch):
    """Exercise the saved loader, live pricing, reservations and fill reconciliation together."""
    from test_independent_stock_runtime import ACCOUNT, BROKER_ID
    from ml.stock_trader.contracts import PortfolioState, QuoteState
    from ml.stock_trader.horizon_ledger import HorizonLedger, OrderEvidence, FillEvidence
    from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals

    env = environment
    env.held = {symbol: 0. for symbol in STOCK_TRADER_SYMBOLS}
    env.cash = 100_000.
    run = env.root / 'ml/nightly-gameplan-runs/saved'
    run.mkdir(parents=True)
    pointer = env.root / 'ml/nightly-gameplan-latest/run.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'current': {'run_path': 'ml/nightly-gameplan-runs/saved'}}))
    rows = []
    for hour in (6, 7, 8, 9):
        start = pd.Timestamp(f'2026-09-15 {hour:02d}:00', tz='America/Los_Angeles')
        for symbol in STOCK_TRADER_SYMBOLS:
            bullish = symbol == 'TWST' and hour < 9
            rows.append(dict(id=f'2026-09-15:{symbol}:1h@{hour:02d}:00', symbol=symbol,
                model_group='1h', direction='BULLISH' if bullish else 'BEARISH',
                calibrated_probability=.6 if bullish else .4, target_window_start=start,
                target_window_end=start + pd.Timedelta(hours=1), execution_eligible=True,
                action_date='2026-09-15', frozen_at='2026-09-15T09:00:00Z'))
    pd.DataFrame(rows).to_parquet(run / 'forecasts.parquet')
    monkeypatch.setattr(runtime, 'load_current_independent_gameplan_signals', load_current_independent_gameplan_signals)
    monkeypatch.setattr(runtime, 'load_current_enrichment_model', lambda *_: pytest.fail('No research revalidation during execution'))

    def fills(broker, ledger, **kwargs):
        evidence = []
        for order in ledger.snapshot().reservations:
            if order.status != 'SUBMITTED':
                continue
            sign = 1 if order.side == 'BUY' else -1
            env.held[order.symbol] += sign * order.quantity
            env.cash -= sign * order.quantity * float(order.limit_price)
            evidence.append(OrderEvidence('evidence-' + order.reservation_id, order.reservation_id,
                ACCOUNT, env.now.isoformat(), order.broker_order_id, 'FILLED', order.quantity,
                order.quantity, 0, (FillEvidence('fill-' + order.reservation_id, order.quantity,
                    float(order.limit_price), env.now.isoformat()),)))
        return tuple(evidence)

    def capture(*args, **kwargs):
        return PortfolioState(env.now.isoformat(), 100_000., env.cash,
            sum(env.held.values()) * 100., 0., dict(env.held),
            {s: q * 100. for s, q in env.held.items()}, {}, {}, 0,
            {s: QuoteState(s, 99., 101., 100., 100., 1000.,
                (env.now - pd.Timedelta(minutes=13)).isoformat(), received_at=env.now.isoformat(),
                realtime=True, quote_type='NBBO') for s in STOCK_TRADER_SYMBOLS},
            'snapshot-' + env.now.isoformat(), BROKER_ID)

    monkeypatch.setattr('ml.stock_trader.horizon_broker.capture_order_evidence', fills)
    monkeypatch.setattr(runtime, 'capture_portfolio_state', capture)

    def wake(local_time):
        env.now = pd.Timestamp('2026-09-15 ' + local_time, tz='America/Los_Angeles').tz_convert('UTC')
        result = runtime.run_independent_stock_trader_once(env.root, execute=True, session=env.broker,
            runtime_clock=lambda: env.now, session_managed=True, sizing_policy=GAMEPLAN_SIZING_POLICY)
        assert not result.error, result.error
        return result

    assert wake('06:50:00').submitted_orders == 1  # Same saved hour, across the PRE/core transition.
    assert wake('06:50:30').submitted_orders == 0  # Fill recorded; no duplicate entry.
    for hour in (7, 8):
        result = wake(f'{hour:02d}:01:00')
        assert result.submitted_orders == 1
        assert env.broker.submissions[-1]['orderLegCollection'][0]['instruction'] == 'SELL'
        assert any(d['decision_reason_code'] == 'HORIZON_ALLOCATION_ALREADY_ACTIVE'
                   for d in _decisions(result)['decisions'])
        assert wake(f'{hour:02d}:01:30').submitted_orders == 1  # Prior exit now filled.
        assert env.broker.submissions[-1]['orderLegCollection'][0]['instruction'] == 'BUY'
        assert wake(f'{hour:02d}:02:00').submitted_orders == 0
    assert wake('09:01:00').submitted_orders == 1
    assert wake('09:01:30').submitted_orders == 0
    legs = [p['orderLegCollection'][0] for p in env.broker.submissions]
    assert [leg['instruction'] for leg in legs] == ['BUY', 'SELL'] * 3
    assert all(leg['instrument']['symbol'] == 'TWST' and leg['quantity'] == 14 for leg in legs)
    assert [float(p['price']) for p in env.broker.submissions] == [100., 99.] * 3
    assert env.held['TWST'] == 0 and env.cash == 100_000. - 3 * 14.
    state = HorizonLedger(env.root / runtime.LEDGER_RELATIVE_PATH, ACCOUNT).snapshot()
    assert len(state.allocations) == 3 and all(a.status == 'CLOSED' for a in state.allocations)
    assert [pd.Timestamp(a.target_end).tz_convert('America/Los_Angeles').hour
            for a in sorted(state.allocations, key=lambda a: a.target_start)] == [7, 8, 9]
