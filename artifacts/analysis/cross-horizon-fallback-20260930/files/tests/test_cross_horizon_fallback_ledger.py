from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ml.stock_trader.cross_horizon_fallback import policy_for_action_date, slot_quota
from ml.stock_trader.horizon_ledger import (
    FillEvidence, HorizonLedger, LedgerError, OrderEvidence, PortfolioEvidence,
)


ACCOUNT = 'a' * 64
SOURCE = 'c' * 64
DAY = '2026-10-01'
START = DAY + 'T11:00:00+00:00'
END = DAY + 'T12:00:00+00:00'
NOW = DAY + 'T11:00:10+00:00'


def portfolio(identity, at, held):
    return PortfolioEvidence(identity, ACCOUNT, at, {'COST': held}, {'COST': 100},
                             {'COST': 1000000}, 'b' * 64)


def evidence(order, *, at, filled=None, status='FILLED', identity=None, fills=None):
    filled = order.quantity if filled is None else filled
    if fills is None:
        fills = (FillEvidence('fill-' + order.reservation_id, filled, 100, at),) if filled else ()
    return OrderEvidence(identity or 'evidence-' + order.reservation_id, order.reservation_id,
        ACCOUNT, at, 'broker-' + order.reservation_id, status, order.quantity, filled,
        0 if status in {'FILLED','CANCELLED','REJECTED'} else order.quantity-filled, fills)


def prepare(tmp_path, *, donors=None, free=0, own=0, freeze=True):
    donors = donors or {'4h':100, '1d':100, '1w':100}
    ledger = HorizonLedger(tmp_path/'ledger.sqlite3', ACCOUNT)
    assert ledger.reconcile(portfolio('empty', '2026-09-30T11:00:00+00:00', free)).ready
    entries = []
    for horizon, count in {**donors, **({'1h':own} if own else {})}.items():
        entries.append(ledger.reserve_entry(symbol='COST', horizon=horizon, forecast_id='owner-'+horizon,
            target_start='2026-09-30T11:00:00+00:00', target_end='2026-10-07T00:00:00+00:00',
            quantity=count, limit_price=100, snapshot_id='empty', idempotency_key='buy-'+horizon,
            batch_id='buys', as_of='2026-09-30T11:00:10+00:00'))
    held = sum(donors.values())+free+own
    assert ledger.reconcile(portfolio('bought', '2026-09-30T11:00:21+00:00', held),
        order_evidence=tuple(evidence(r,at='2026-09-30T11:00:20+00:00') for r in entries)).ready
    assert ledger.reconcile(portfolio('opening', START, held)).ready
    if freeze:
        ledger.freeze_fallback_day(action_date=DAY, policy=policy_for_action_date(DAY),
            source_fingerprint=SOURCE, snapshot_id='opening', as_of=NOW)
    return ledger


def plan(ledger, **kwargs):
    values = dict(symbol='COST', horizon='1h', forecast_id='trigger-1h-04', target_start=START,
        target_end=END, action_date=DAY, policy=policy_for_action_date(DAY), source_fingerprint=SOURCE,
        snapshot_id='opening', as_of=NOW, batch_id='sells')
    values.update(kwargs)
    return ledger.fallback_direction_plan(**values)


def sell(ledger, **kwargs):
    p = plan(ledger)
    values = dict(symbol='COST', horizon='1h', forecast_id='trigger-1h-04', target_start=START,
        target_end=END, action_date=DAY, policy=policy_for_action_date(DAY), source_fingerprint=SOURCE,
        snapshot_id='opening', as_of=NOW, batch_id='sells', idempotency_key='fallback-1h-04',
        donor_allocation_id=p['donor_allocation_id'], quantity=p['maximum_quantity'], limit_price=100)
    values.update(kwargs)
    return ledger.reserve_fallback_direction_exit(**values)


def test_freeze_initial_owned_only_is_immutable_and_preview_does_not_create_it(tmp_path):
    ledger = prepare(tmp_path, free=20, own=40, freeze=False)
    ephemeral = ledger.freeze_fallback_day(action_date=DAY, policy=policy_for_action_date(DAY),
        source_fingerprint=SOURCE,snapshot_id='opening',as_of=NOW,persist=False)
    assert ephemeral['symbols']['COST']['daily_cap'] == 150
    assert not [e for e in ledger.history() if e.kind=='fallback-day-baseline']
    with pytest.raises(LedgerError, match='BASELINE_REQUIRED'):
        plan(ledger)
    assert plan(ledger,preview=True)['reason']=='FALLBACK_REQUIRES_GENUINELY_ZERO_OWN_INVENTORY'
    baseline = ledger.freeze_fallback_day(action_date=DAY, policy=policy_for_action_date(DAY),
        source_fingerprint=SOURCE,snapshot_id='opening',as_of=NOW)
    assert baseline == ephemeral
    assert len([e for e in ledger.history() if e.kind=='fallback-day-baseline']) == 1
    assert ledger.reconcile(portfolio('later', DAY+'T11:00:20+00:00', 360)).ready
    again = ledger.freeze_fallback_day(action_date=DAY,policy=policy_for_action_date(DAY),
        source_fingerprint=SOURCE,snapshot_id='later',as_of=DAY+'T11:00:21+00:00')
    assert again==baseline


def test_fallback_keeps_donor_owner_and_real_trigger_and_survives_restart(tmp_path):
    ledger=prepare(tmp_path)
    p=plan(ledger)
    assert p['eligible'] and p['donor_horizon']=='4h'
    assert p['symbol_daily_cap']==150 and p['donor_daily_cap']==50
    assert p['maximum_quantity']==slot_quota(150,'1h',START,DAY)==6
    reservation=sell(ledger)
    assert reservation.horizon=='4h' and reservation.forecast_id=='trigger-1h-04'
    assert reservation.owner_forecast_id=='owner-4h'
    assert reservation.trigger_horizon=='1h' and reservation.trigger_forecast_id=='trigger-1h-04'
    assert reservation.fallback_action_date==DAY
    reopened=HorizonLedger(ledger.path,ACCOUNT)
    # Exact retry returns its existing reservation; no new order or assignment.
    assert sell(reopened,donor_allocation_id=reservation.allocation_id,quantity=reservation.quantity)==reservation
    assert len(reopened.snapshot().allocations)==3
    assert not [e for e in reopened.history() if e.kind=='gameplan-existing-stock-assignment']
    assert plan(reopened)['reason']=='FORECAST_ALREADY_RESERVED_NO_REENTRY'
    with pytest.raises(LedgerError,match='FORECAST_ALREADY_RESERVED'):
        reopened.reserve_entry(symbol='COST',horizon='1h',forecast_id='trigger-1h-04',
            target_start=START,target_end=END,quantity=1,limit_price=100,snapshot_id='opening',
            as_of=NOW,idempotency_key='same-trigger-buy',batch_id='sells')


def test_partial_fill_and_terminal_cancel_charge_only_proven_fill_and_never_replay_slot(tmp_path):
    ledger=prepare(tmp_path)
    reservation=sell(ledger)
    partial=evidence(reservation,at=DAY+'T11:00:20+00:00',filled=2,status='PARTIAL')
    assert ledger.reconcile(portfolio('partial',DAY+'T11:00:21+00:00',298),order_evidence=(partial,)).ready
    fresh=dict(snapshot_id='partial',as_of=DAY+'T11:00:22+00:00')
    p=plan(ledger,forecast_id='another-id-same-slot',**fresh)
    assert p['symbol_used']==6 and p['reason']=='FALLBACK_SLOT_ALREADY_RESERVED_NO_ROLLOVER'
    donor=next(a for a in ledger.snapshot().allocations if a.horizon=='4h')
    assert donor.filled_shares==98 and donor.reserved_sell_shares==4
    cancelled=replace(partial,evidence_id='terminal-cancel',observed_at=DAY+'T11:00:23+00:00',
                      status='CANCELLED',remaining_quantity=0)
    assert ledger.reconcile(portfolio('cancelled',DAY+'T11:00:24+00:00',298),order_evidence=(cancelled,)).ready
    p=plan(ledger,forecast_id='changed-id-after-cancel',snapshot_id='cancelled',as_of=DAY+'T11:00:25+00:00')
    assert p['symbol_used']==2 and p['reason']=='FALLBACK_SLOT_ALREADY_RESERVED_NO_ROLLOVER'
    assert sum(a.filled_shares for a in ledger.snapshot().allocations)==298


def test_unknown_keeps_all_budget_and_does_not_enable_other_fallback(tmp_path):
    ledger=prepare(tmp_path); reservation=sell(ledger)
    ledger.mark_submission(reservation.reservation_id,status='UNKNOWN',observed_at=DAY+'T11:00:12+00:00',evidence_id='unknown')
    assert ledger.lookup_reservation(reservation.reservation_id).reserved_quantity==6
    with pytest.raises(LedgerError,match='UNKNOWN_SUBMISSION'):
        plan(ledger,forecast_id='other')
    with pytest.raises(LedgerError,match='BROKER_RECONCILIATION'):
        ledger.mark_submission(reservation.reservation_id,status='REJECTED',observed_at=DAY+'T11:00:13+00:00',evidence_id='guessed')


@pytest.mark.parametrize('kwargs,reason', [({'own':1},'ZERO_OWN'),({'free':1},'ZERO_ELIGIBLE_UNALLOCATED')])
def test_real_own_or_unallocated_shares_prevent_fallback(tmp_path,kwargs,reason):
    ledger=prepare(tmp_path,**kwargs)
    assert reason in plan(ledger)['reason']


def test_pending_own_buy_and_external_orders_cannot_manufacture_eligibility(tmp_path):
    ledger=prepare(tmp_path)
    assert plan(ledger,pending_buy_shares=1)['reason']=='FALLBACK_EXTERNAL_PENDING_ORDER'
    assert plan(ledger,pending_sell_shares=1)['reason']=='FALLBACK_EXTERNAL_PENDING_ORDER'
    ledger.reserve_entry(symbol='COST',horizon='1h',forecast_id='own-pending-buy',target_start=START,
        target_end=END,quantity=1,limit_price=100,snapshot_id='opening',idempotency_key='own-buy',batch_id='sells',as_of=NOW)
    assert plan(ledger,pending_buy_shares=1)['reason']=='FALLBACK_TRIGGER_HAS_PENDING_ORDER'


def test_nearest_eligible_donor_hierarchy_and_normal_sale_drafts(tmp_path):
    ledger=prepare(tmp_path)
    assert plan(ledger,excluded_donor_horizons=('4h',))['donor_horizon']=='1d'
    assert plan(ledger,reserved_donor_shares={'4h':100})['donor_horizon']=='1d'
    assert plan(ledger,excluded_donor_horizons=('4h','1d','1w'))['maximum_quantity']==0
    assert plan(ledger,horizon='1w')['maximum_quantity']==0
    farther=next(a for a in ledger.snapshot().allocations if a.horizon=='1d')
    with pytest.raises(LedgerError,match='NOT_NEAREST'):
        sell(ledger,donor_allocation_id=farther.allocation_id)
    with pytest.raises(LedgerError,match='EXCEEDS_SLOT'):
        sell(ledger,quantity=7)


def test_concurrent_same_slot_different_ids_cannot_multiply_quota(tmp_path):
    ledger=prepare(tmp_path); p=plan(ledger)
    def attempt(index):
        local=HorizonLedger(ledger.path,ACCOUNT)
        try:
            return sell(local,forecast_id=f'candidate-{index}',idempotency_key=f'key-{index}',
                        donor_allocation_id=p['donor_allocation_id'],quantity=p['maximum_quantity']).reservation_id
        except LedgerError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,range(2)))
    assert sum(len(r)==64 for r in results)==1
    assert 'FALLBACK_SLOT_ALREADY_RESERVED_NO_ROLLOVER' in results
    assert len([r for r in ledger.snapshot().reservations if r.fallback_policy_version])==1


def test_binding_date_source_and_policy_cannot_reset_a_day(tmp_path):
    ledger=prepare(tmp_path)
    with pytest.raises(LedgerError,match='SOURCE_OR_POLICY_MISMATCH'):
        plan(ledger,source_fingerprint='d'*64)
    with pytest.raises(LedgerError,match='ACTION_DATE_MISMATCH'):
        plan(ledger,action_date='2026-10-02',policy=policy_for_action_date('2026-10-02'))
    changed={**policy_for_action_date(DAY),'daily_cap_numerator':2}
    with pytest.raises(ValueError,match='Unsupported'):
        plan(ledger,policy=changed)
    with pytest.raises(LedgerError,match='NOT_ENABLED'):
        plan(ledger,policy=None)
    with pytest.raises(LedgerError,match='ACCOUNT_FINGERPRINT'):
        HorizonLedger(ledger.path,'e'*64)


def test_new_day_uses_remaining_inventory_and_prior_pending_carries_cap(tmp_path):
    ledger=prepare(tmp_path); reservation=sell(ledger)
    filled=evidence(reservation,at=DAY+'T11:00:20+00:00',filled=2,status='PARTIAL')
    assert ledger.reconcile(portfolio('partial',DAY+'T11:00:21+00:00',298),order_evidence=(filled,)).ready
    nextday='2026-10-02'; start=nextday+'T11:00:00+00:00'
    working=replace(filled,evidence_id='nextday-working',observed_at=start)
    assert ledger.reconcile(portfolio('nextday',nextday+'T11:00:01+00:00',298),order_evidence=(working,)).ready
    baseline=ledger.freeze_fallback_day(action_date=nextday,policy=policy_for_action_date(nextday),
        source_fingerprint='f'*64,snapshot_id='nextday',as_of=nextday+'T11:00:02+00:00')
    assert baseline['symbols']['COST']['daily_cap']==149
    p=plan(ledger,action_date=nextday,policy=policy_for_action_date(nextday),source_fingerprint='f'*64,
        snapshot_id='nextday',as_of=nextday+'T11:00:02+00:00',target_start=start,
        target_end=nextday+'T12:00:00+00:00',forecast_id='nextday-1h')
    assert p['symbol_used']==4
    assert p['reason']=='FALLBACK_TRIGGER_HAS_PENDING_ORDER'
    terminal=replace(working,evidence_id='nextday-cancel',observed_at=nextday+'T11:00:03+00:00',
                     status='CANCELLED',remaining_quantity=0)
    assert ledger.reconcile(portfolio('nextday-cancelled',nextday+'T11:00:04+00:00',298),order_evidence=(terminal,)).ready
    p=plan(ledger,action_date=nextday,policy=policy_for_action_date(nextday),source_fingerprint='f'*64,
        snapshot_id='nextday-cancelled',as_of=nextday+'T11:00:05+00:00',target_start=start,
        target_end=nextday+'T12:00:00+00:00',forecast_id='nextday-1h')
    assert p['eligible'] and p['symbol_used']==0 and p['donor_daily_cap']==49


def test_new_allocations_after_baseline_do_not_add_donor_budget(tmp_path):
    ledger=prepare(tmp_path,donors={'4h':100})
    baseline=ledger.freeze_fallback_day(action_date=DAY,policy=policy_for_action_date(DAY),
        source_fingerprint=SOURCE,snapshot_id='opening',as_of=NOW)
    bought=ledger.reserve_entry(symbol='COST',horizon='1d',forecast_id='new-daily-owner',target_start=START,
        target_end=DAY+'T23:00:00+00:00',quantity=100,limit_price=100,snapshot_id='opening',
        idempotency_key='new-daily-buy',batch_id='sells',as_of=NOW)
    assert ledger.reconcile(portfolio('newbuy',DAY+'T11:00:21+00:00',200),
        order_evidence=(evidence(bought,at=DAY+'T11:00:20+00:00'),)).ready
    p=plan(ledger,snapshot_id='newbuy',as_of=DAY+'T11:00:22+00:00',excluded_donor_horizons=('4h',))
    assert p['baseline_id']==baseline['baseline_id'] and p['symbol_daily_cap']==50 and not p['eligible']


def test_prior_batch_working_donor_sell_is_excluded_even_with_unreserved_shares(tmp_path):
    ledger=prepare(tmp_path)
    ordinary=ledger.reserve_direction_exit(symbol='COST',horizon='4h',forecast_id='normal-4h',
        target_start=START,target_end=DAY+'T15:00:00+00:00',quantity=10,limit_price=100,
        snapshot_id='opening',idempotency_key='normal-sale',batch_id='earlier-batch',as_of=NOW)
    assert ledger.reconcile(portfolio('working-donor',DAY+'T11:00:21+00:00',300),
        order_evidence=(evidence(ordinary,at=DAY+'T11:00:20+00:00',filled=0,status='WORKING'),)).ready
    args=dict(snapshot_id='working-donor',as_of=DAY+'T11:00:22+00:00',pending_sell_shares=10)
    assert plan(ledger,**args)['donor_horizon']=='1d'
    assert plan(ledger,batch_id=None,preview=True,**args)['donor_horizon']=='1d'
    assert plan(ledger,batch_id='earlier-batch',**args)['donor_horizon']=='1d'


def test_all_weighted_slots_share_exact_cap_and_donor_cap_without_rollover(tmp_path):
    ledger=prepare(tmp_path,donors={'1w':300})
    slots=[(hour,h) for hour in range(4,17) for h in ['1h','4h','1d']
           if h=='1h' or h=='4h' and hour in [4,8,12,16] or h=='1d' and hour==4]
    held=300; quantities=[]
    for i,(hour,horizon) in enumerate(slots):
        start=datetime.fromisoformat(START)+timedelta(hours=hour-4)
        snapshot_at=start+timedelta(seconds=3*i+1)
        now=snapshot_at+timedelta(seconds=1)
        fill_at=now+timedelta(seconds=1)
        assert ledger.reconcile(portfolio(f'slot-{i}',snapshot_at.isoformat(),held)).ready
        args=dict(symbol='COST',horizon=horizon,forecast_id=f'trigger-{i}',target_start=start.isoformat(),
            target_end=(start+timedelta(hours=1)).isoformat(),action_date=DAY,policy=policy_for_action_date(DAY),
            source_fingerprint=SOURCE,snapshot_id=f'slot-{i}',as_of=now.isoformat(),batch_id=f'batch-{i}')
        p=ledger.fallback_direction_plan(**args)
        assert p['eligible'] and p['donor_horizon']=='1w'
        quantity=p['maximum_quantity']; quantities.append(quantity)
        reservation=ledger.reserve_fallback_direction_exit(**args,donor_allocation_id=p['donor_allocation_id'],
            quantity=quantity,limit_price=100,idempotency_key=f'sell-{i}')
        held-=quantity
        assert ledger.reconcile(portfolio(f'filled-{i}',(fill_at+timedelta(microseconds=1)).isoformat(),held),
            order_evidence=(evidence(reservation,at=fill_at.isoformat()),)).ready
    assert len(quantities)==18 and sum(quantities)==150 and held==150
    assert all(q in {6,7} for q,(_,h) in zip(quantities,slots) if h=='1h')
    assert all(q in {12,13} for q,(_,h) in zip(quantities,slots) if h=='4h')
    assert next(q for q,(_,h) in zip(quantities,slots) if h=='1d') in {18,19}
    assert sum(r.filled_quantity for r in ledger.snapshot().reservations if r.fallback_policy_version)==150


def test_small_nearest_donor_is_not_topped_up_from_a_second_allocation(tmp_path):
    ledger=prepare(tmp_path,donors={'4h':2,'1d':100,'1w':100})
    p=plan(ledger)
    assert p['slot_quota']==4 and p['donor_horizon']=='4h' and p['maximum_quantity']==1
    reservation=sell(ledger)
    assert reservation.quantity==1
    with pytest.raises(LedgerError,match='FORECAST_ALREADY_RESERVED'):
        sell(ledger,donor_allocation_id=next(a.allocation_id for a in ledger.snapshot().allocations if a.horizon=='1d'),
             quantity=1,idempotency_key='second-donor')


def test_late_previous_day_fill_charges_actual_fill_date_without_counting_old_fills_again(tmp_path):
    ledger=prepare(tmp_path); reservation=sell(ledger)
    partial=evidence(reservation,at=DAY+'T11:00:20+00:00',filled=2,status='PARTIAL')
    assert ledger.reconcile(portfolio('partial',DAY+'T11:00:21+00:00',298),order_evidence=(partial,)).ready
    nextday='2026-10-02'; start=nextday+'T11:00:00+00:00'
    working=replace(partial,evidence_id='working-tomorrow',observed_at=start)
    assert ledger.reconcile(portfolio('opening-tomorrow',nextday+'T11:00:01+00:00',298),order_evidence=(working,)).ready
    ledger.freeze_fallback_day(action_date=nextday,policy=policy_for_action_date(nextday),source_fingerprint='f'*64,
        snapshot_id='opening-tomorrow',as_of=nextday+'T11:00:02+00:00')
    late_fill=FillEvidence('later-fill',4,99,nextday+'T11:00:03+00:00')
    completed=replace(working,evidence_id='late-complete',observed_at=nextday+'T11:00:03+00:00',status='FILLED',
        cumulative_filled_quantity=6,remaining_quantity=0,fills=partial.fills+(late_fill,))
    assert ledger.reconcile(portfolio('late-filled',nextday+'T11:00:04+00:00',294),order_evidence=(completed,)).ready
    p=plan(ledger,action_date=nextday,policy=policy_for_action_date(nextday),source_fingerprint='f'*64,
        snapshot_id='late-filled',as_of=nextday+'T11:00:05+00:00',target_start=start,
        target_end=nextday+'T12:00:00+00:00',forecast_id='nextday-1h')
    assert p['eligible'] and p['symbol_daily_cap']==149 and p['symbol_used']==4
    assert p['donor_used']==4 and p['donor_remaining']==45
