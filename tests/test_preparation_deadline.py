import json
from pathlib import Path

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml.preparation_deadline import VERSION, preparation_deadline


def exception_record(root, source, *, session='2026-09-14'):
    opening = pd.Timestamp(session).tz_localize('America/Los_Angeles') + pd.Timedelta(hours=4)
    payload = dict(schema_version=VERSION, scope='PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY',
                   operator_authorized=True, orders_authorized=False,
                   authorization_text='Test operator permits this late preparation only.', authorization_source='test',
                   action_date=session, gameplan_run=source.relative_to(root).as_posix(),
                   gameplan_receipt_sha256=file_checksum(source/'receipt.json'),
                   original_deadline_at=opening.isoformat(), approved_at=(opening+pd.Timedelta(minutes=1)).isoformat(),
                   expires_at=(opening+pd.Timedelta(hours=1)).isoformat())
    record = root/'exception.json'
    record.write_text(json.dumps(payload))
    return record, payload


@pytest.mark.parametrize('damage', ['none', 'source', 'hash', 'expired', 'future-approval', 'next-session', 'orders', 'missing-authority'])
def test_exception_is_bounded_to_approved_source_and_session(tmp_path, damage):
    source = tmp_path/'ml/nightly-gameplan-runs/source'
    source.mkdir(parents=True); (source/'receipt.json').write_text('{"action_date":"2026-09-14"}')
    path, payload = exception_record(tmp_path, source)
    if damage == 'source': payload['gameplan_run'] += '-other'
    if damage == 'hash': payload['gameplan_receipt_sha256'] = 'changed'
    if damage == 'expired': payload['expires_at'] = '2026-09-14T11:10:00Z'
    if damage == 'future-approval': payload['approved_at'] = '2026-09-14T11:50:00Z'
    if damage == 'next-session': payload['expires_at'] = '2026-09-15T12:00:00Z'
    if damage == 'orders': payload['orders_authorized'] = True
    if damage == 'missing-authority': payload['operator_authorized'] = False
    path.write_text(json.dumps(payload))
    args = (tmp_path, source, '2026-09-14T11:00:00Z', '2026-09-14T11:20:00Z')
    if damage == 'none':
        deadline, evidence = preparation_deadline(*args, path)
        assert deadline == pd.Timestamp('2026-09-14T12:00:00Z')
        assert evidence['sha256'] == file_checksum(path)
        assert preparation_deadline(*args)[0] == pd.Timestamp('2026-09-14T11:00:00Z')
    else:
        with pytest.raises(ValueError): preparation_deadline(*args, path)


def test_native_late_tail_retains_original_deadline_and_old_evidence(tmp_path, monkeypatch):
    from ml import overnight_runtime as native
    source = tmp_path/'ml/nightly-gameplan-runs/source'
    source.mkdir(parents=True); (source/'receipt.json').write_text('{"action_date":"2026-09-14"}')
    exception, payload = exception_record(tmp_path, source)
    failed = tmp_path/'ml/overnight-runs/failed'; failed.mkdir(parents=True)
    pin = dict(run_path=payload['gameplan_run'], receipt_sha256=payload['gameplan_receipt_sha256'], action_date='2026-09-14')
    report = dict(status='FAILED', failed_stage='gameplan_trade_planning',
                  deadline_at='2026-09-14T11:00:00Z', stage_order=['gameplan_trade_planning','gameplan_actuals_review'],
                  preparation_scope='STOCK_ONLY', stock_only=True, independent_stock_horizons=True,
                  stock_price_source='xnas-itch-archive-v1', enrichment_gameplan=pin, stages=[],
                  completed_stages_from_previous_attempt=['gameplan_publication','stock_enrichment_training'])
    (failed/'stage-report.json').write_text(json.dumps(report))
    receipt = dict(schema_version=native.OVERNIGHT_RUNTIME_VERSION, run_path=failed.relative_to(tmp_path).as_posix(),
                   status='FAILED', orders_placed=0, broker_orders_enabled=False,
                   stage_report_checksum_sha256=file_checksum(failed/'stage-report.json'))
    (failed/'receipt.json').write_text(json.dumps(receipt))
    original = {p.name:p.read_bytes() for p in failed.iterdir()}
    monkeypatch.setattr(native, 'utc_timestamp', lambda value=None: pd.Timestamp(value or '2026-09-14T11:20:00Z'))
    with pytest.raises(RuntimeError, match='deadline has passed'):
        native._resume_configuration(tmp_path, failed)
    monkeypatch.setattr(native, '_pin_stock_gameplan', lambda *a, **kw: pin)
    calls = []
    def complete(command, **kwargs):
        assert kwargs['deadline'] == pd.Timestamp('2026-09-14T12:00:00Z')
        assert command[-2:] == ('--deadline-exception', str(exception.resolve()))
        kwargs['log_path'].write_text('verified test tail')
        calls.append(command[3]); return 0
    monkeypatch.setattr(native, '_run_stage', complete)
    run = native.run_overnight_pipeline(tmp_path, datastore_argument=('--datastore',str(tmp_path)),
               repository_root=tmp_path, reporter=None, resume_run=failed, deadline_exception=exception)
    result = json.loads((run/'stage-report.json').read_text())
    assert result['status'] == 'COMPLETE' and result['deadline_at'] == '2026-09-14T11:00:00+00:00'
    assert calls == ['ml.gameplan_trade_planning','ml.gameplan_actuals_review']
    assert result['enrichment_gameplan'] == pin
    assert {p.name:p.read_bytes() for p in failed.iterdir()} == original
    with pytest.raises(ValueError, match='existing pinned tail'):
        native.run_overnight_pipeline(tmp_path, datastore_argument=('--datastore',str(tmp_path)),
                                     repository_root=tmp_path, deadline_exception=exception)
