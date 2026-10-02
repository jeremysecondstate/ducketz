"""Future fallback integration with temporary ledgers and synthetic brokers only."""
from dataclasses import replace

import pandas as pd
import pytest

from test_gameplan_direction_engine import inputs, build, orders
from test_gameplan_direction_runtime import prepare, run
from test_independent_stock_runtime import environment, _owned_allocation, _decisions
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader import cross_horizon_fallback as fallback


DAY = "2026-10-01"
NOW = pd.Timestamp("2026-10-01T12:01:00Z")  # The 05:00 hourly opportunity.


def future_inputs(probability=.3):
    signals, portfolio = inputs(probability=probability)
    signals = {key: replace(s, prediction_id=f"future-{key[0]}-{key[1]}",
        target_window_start=NOW.floor("h").isoformat(),
        target_window_end=(NOW.floor("h") + pd.Timedelta(hours=1)).isoformat(),
        actionable_until=(NOW.floor("h") + pd.Timedelta(hours=1)).isoformat(),
        source_fingerprint="future-source") for key,s in signals.items()}
    portfolio = replace(portfolio, observed_at=NOW.isoformat(), held_shares={"AAPL":100},
        quotes={s:replace(q, observed_at=NOW.isoformat()) for s,q in portfolio.quotes.items()})
    return signals, portfolio


def donor_plan(quantity=3, horizon="4h"):
    return {"quantity":quantity, "donor_allocation_id":"donor-"+horizon,
            "donor_horizon":horizon, "action_date":DAY, "source_fingerprint":"future-source"}


def test_engine_records_donor_separately_and_keeps_original_forecast_identity():
    signals, portfolio = future_inputs()
    key = ("AAPL","1h")
    decision = build({key:signals[key]}, portfolio, decided_at=NOW,
        fallback_policy=fallback.policy_for_action_date(DAY), fallback_sell_plans={key:donor_plan()})[0]
    assert decision.quantity == 3 and decision.action == "SELL"
    assert decision.prediction["prediction_id"] == signals[key].prediction_id
    assert decision.prediction["primary_horizon"] == "1h"
    assert decision.prediction["position_purpose"] == "FALLBACK_DIRECTION_EXIT"
    assert decision.prediction["cross_horizon_fallback"]["donor_horizon"] == "4h"
    assert decision.enrichment["pending_sales_fund_this_batch"] is False
    assert "normal-bearish-fallback-bearish" in decision.enrichment["ranking_policy"]


def test_engine_prioritizes_normal_owner_sale_over_shorter_fallback():
    signals, portfolio = future_inputs()
    signals = {k:v for k,v in signals.items() if k[1] in {"1h","4h"}}
    decisions = build(signals, portfolio, decided_at=NOW, bearish_sell_capacities={("AAPL","4h"):5},
        fallback_policy=fallback.policy_for_action_date(DAY), fallback_sell_plans={("AAPL","1h"):donor_plan(3,"1d")})
    assert [d.prediction["primary_horizon"] for d in decisions] == ["4h","1h"]
    assert [d.quantity for d in decisions] == [5,3]


@pytest.mark.parametrize("change", ["own_capacity", "shorter_donor", "missing_policy", "premature"])
def test_engine_rejects_unauthorized_fallback_attestations(change):
    signals, portfolio = future_inputs()
    key = "AAPL","1h"
    kwargs = dict(decided_at=NOW, fallback_policy=fallback.policy_for_action_date(DAY),
                  fallback_sell_plans={key:donor_plan()})
    if change == "own_capacity": kwargs["bearish_sell_capacities"] = {key:1}
    if change == "shorter_donor": kwargs["fallback_sell_plans"][key]["donor_horizon"] = "1h"
    if change == "missing_policy": kwargs["fallback_policy"] = None
    if change == "premature": kwargs["decided_at"] = "2026-09-30T12:01:00Z"
    with pytest.raises(ValueError):
        build({key:signals[key]}, portfolio, **kwargs)


@pytest.mark.parametrize("guard", ["stale_quote", "inactive", "unreconciled", "no_cash"])
def test_fallback_retains_quotes_controls_reconciliation_and_never_funds_buys(guard):
    signals, portfolio = future_inputs()
    keys = ("AAPL","1h"), ("AAPL","1d")
    signals = {keys[0]:signals[keys[0]], keys[1]:replace(signals[keys[1]], calibrated_probability=.8)}
    kwargs = {}
    if guard == "stale_quote":
        portfolio = replace(portfolio, quotes={s:replace(q, observed_at=(NOW-pd.Timedelta(minutes=2)).isoformat()) for s,q in portfolio.quotes.items()})
    if guard == "inactive":
        from test_gameplan_direction_engine import ACTIVATION
        kwargs["activation"] = replace(ACTIVATION, active=False)
    if guard == "unreconciled": kwargs["ledger_ready"] = False
    if guard == "no_cash": portfolio = replace(portfolio, available_cash=0.)
    decisions = build(signals, portfolio, decided_at=NOW,
        fallback_policy=fallback.policy_for_action_date(DAY), fallback_sell_plans={keys[0]:donor_plan()}, **kwargs)
    if guard == "no_cash": assert len(orders(decisions,"SELL")) == 1 and not orders(decisions,"BUY")
    else: assert not orders(decisions)


def prepare_future(env, monkeypatch, *, manual=0, donor_horizon="4h", quantity=30):
    prepare(env, monkeypatch)
    ledger = _owned_allocation(env, symbol="AAPL", horizon=donor_horizon, quantity=quantity, manual=manual)
    env.now = NOW
    signals, _ = future_inputs()
    env.signals = {("AAPL","1h"):signals["AAPL","1h"]}
    source = env.root / "ml/nightly-gameplan-runs/future-source"
    source.mkdir(parents=True)
    (source/"receipt.json").write_text('{"fixture":"future-source"}',encoding="utf-8")
    # This suite tests live plumbing; the real immutable-source reader has its
    # own manifest fixtures. No production publication or broker is consulted.
    monkeypatch.setattr(fallback,"read_gameplan_fallback_policy",lambda *args: fallback.policy_for_action_date(DAY))
    return ledger


def test_runtime_reserves_donor_and_never_repeats_forecast(environment, monkeypatch):
    env=environment
    ledger=prepare_future(env,monkeypatch)
    result=run(env)
    assert result.status == "ORDERS_SUBMITTED", result.error
    assert result.submitted_orders == len(env.broker.submissions) == 1
    sells=[r for r in ledger.snapshot().reservations if r.side == "SELL"]
    assert len(sells)==1 and sells[0].quantity == fallback.slot_quota(15,"1h",env.signals["AAPL","1h"].target_window_start,DAY)
    assert sells[0].horizon == "4h" and sells[0].trigger_horizon == "1h"
    assert sells[0].forecast_id == env.signals["AAPL","1h"].prediction_id
    assert ledger.snapshot().allocations[0].filled_shares == 30
    env.now += pd.Timedelta(seconds=1)
    assert run(env).submitted_orders == 0 and len(env.broker.submissions)==1


@pytest.mark.parametrize("block", ["unallocated", "donor_bullish", "pending_sell", "pending_buy"])
def test_runtime_does_not_borrow_for_blocked_or_available_ordinary_inventory(environment, monkeypatch, block):
    env=environment
    ledger=prepare_future(env,monkeypatch,manual=1 if block=="unallocated" else 0)
    if block=="donor_bullish":
        s=env.signals["AAPL","1h"]
        env.signals["AAPL","4h"] = replace(s,primary_horizon="4h",prediction_id="bullish-donor",calibrated_probability=.8)
    if block=="pending_sell": env.pending_sell["AAPL"] = 1
    if block=="pending_buy":
        capture=runtime.capture_portfolio_state
        monkeypatch.setattr(runtime,"capture_portfolio_state",lambda *args,**kwargs:replace(capture(*args,**kwargs),pending_buy_shares={"AAPL":1}))
    result=run(env,execute=False)
    decisions=_decisions(result)["decisions"]
    assert not any(d["quantity"] and d["prediction"]["position_purpose"]=="FALLBACK_DIRECTION_EXIT" for d in decisions)
    if block=="unallocated":
        sale=next(d for d in decisions if d["action"]=="SELL")
        assert sale["prediction"]["position_purpose"]=="DIRECTION_EXIT" and sale["quantity"]==1


def test_policy_change_before_post_rejects_without_releasing_donor_ownership(environment, monkeypatch):
    env=environment
    ledger=prepare_future(env,monkeypatch)
    changed=[False]
    monkeypatch.setattr(fallback,"read_gameplan_fallback_policy",lambda *args:None if changed[0] else fallback.policy_for_action_date(DAY))
    env.broker.before_post_hook=lambda:changed.__setitem__(0,True)
    result=run(env)
    assert result.status=="SUBMISSION_STOPPED_SAFETY_CHECK" and "POLICY_CHANGED" in result.error
    assert env.broker.submissions==[]
    sale=next(r for r in ledger.snapshot().reservations if r.side=="SELL")
    assert sale.status=="REJECTED" and sale.reserved_quantity==0
    assert ledger.snapshot().allocations[0].filled_shares==30


def test_unknown_submission_holds_donor_and_daily_budget(environment, monkeypatch):
    env=environment
    ledger=prepare_future(env,monkeypatch)
    env.broker.unknown_submission=True
    result=run(env)
    assert result.status=="SUBMISSION_STOPPED_AFTER_ERROR"
    sale=next(r for r in ledger.snapshot().reservations if r.side=="SELL")
    assert sale.status=="UNKNOWN" and sale.reserved_quantity==1
    assert ledger.snapshot().allocations[0].filled_shares==30
    env.now += pd.Timedelta(seconds=1)
    assert run(env).submitted_orders==0 and len(env.broker.submissions)==1


def test_legacy_source_does_not_enable_future_fallback(environment, monkeypatch):
    env=environment
    prepare_future(env,monkeypatch)
    monkeypatch.setattr(fallback,"read_gameplan_fallback_policy",lambda *args:None)
    result=run(env)
    assert result.submitted_orders==0 and env.broker.submissions==[]
    assert all("cross_horizon_fallback_policy" not in d["prediction"] for d in _decisions(result)["decisions"])


def test_same_policy_with_changed_receipt_still_stops_submission(environment, monkeypatch):
    env=environment
    ledger=prepare_future(env,monkeypatch)
    path=env.root/"ml/nightly-gameplan-runs/future-source/receipt.json"
    env.broker.before_post_hook=lambda:path.write_text('{"fixture":"changed"}',encoding="utf-8")
    result=run(env)
    assert result.status=="SUBMISSION_STOPPED_SAFETY_CHECK" and "SOURCE_CHANGED" in result.error
    assert env.broker.submissions==[]
    assert ledger.snapshot().allocations[0].filled_shares==30


def test_source_change_during_account_capture_cannot_freeze_wrong_day_baseline(environment, monkeypatch):
    import sqlite3
    env=environment
    ledger=prepare_future(env,monkeypatch)
    path=env.root/"ml/nightly-gameplan-runs/future-source/receipt.json"
    env.after_capture=lambda:path.write_text('{"fixture":"changed-during-capture"}',encoding="utf-8")
    result=run(env)
    assert result.status=="CROSS_HORIZON_FALLBACK_BASELINE_UNAVAILABLE"
    assert env.broker.submissions==[]
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fallback_days").fetchone()[0]==0


def test_dry_run_does_not_freeze_live_day_or_reserve_donor(environment, monkeypatch):
    import sqlite3
    env=environment
    ledger=prepare_future(env,monkeypatch)
    result=run(env,execute=False)
    assert result.selected_orders==1 and result.submitted_orders==0
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fallback_days").fetchone()[0]==0
    assert len(ledger.snapshot().reservations)==1  # Only the synthetic original BUY.


def test_same_clock_weighted_slots_use_disjoint_slices_of_one_donor(environment, monkeypatch):
    env=environment
    ledger=prepare_future(env,monkeypatch,donor_horizon="1w",quantity=60)
    env.now=pd.Timestamp("2026-10-01T11:01:00Z")
    signals,_=future_inputs()
    env.signals={k:replace(s,target_window_start=env.now.floor("h").isoformat(),
        target_window_end=(env.now.floor("h")+pd.Timedelta(hours=1)).isoformat(),
        actionable_until=(env.now.floor("h")+pd.Timedelta(hours=1)).isoformat())
        for k,s in signals.items() if k[1] in {"1h","4h","1d"}}
    result=run(env)
    assert result.status=="ORDERS_SUBMITTED", result.error
    sells=sorted((r for r in ledger.snapshot().reservations if r.side=="SELL"),key=lambda r:{"1h":1,"4h":2,"1d":3}[r.trigger_horizon])
    assert [r.quantity for r in sells]==[1,2,4]
    assert len(env.broker.submissions)==3 and sum(r.reserved_quantity for r in sells)==7
    assert all(r.horizon=="1w" for r in sells)
    assert ledger.snapshot().allocations[0].filled_shares==60


def test_normal_own_sale_donor_is_excluded_before_fallback_reservation(environment, monkeypatch):
    env=environment
    ledger=prepare_future(env,monkeypatch,donor_horizon="1w",quantity=60)
    signals,_=future_inputs()
    env.signals["AAPL","1w"]=signals["AAPL","1w"]
    result=run(env)
    assert result.status=="ORDERS_SUBMITTED", result.error
    sales=[r for r in ledger.snapshot().reservations if r.side=="SELL"]
    assert len(sales)==1 and sales[0].horizon=="1w" and sales[0].fallback_policy_version is None
    assert sales[0].quantity==50  # The existing single-order cap leaves ten protected shares.
