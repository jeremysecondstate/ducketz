"""Late provenance round trips without backdating or future input admission."""
import json

import pandas as pd
import pytest

from ml.stock_trader.independent_signals import _validated_independent_forecasts, late_publication_time
from test_independent_stock_signals import _frame, SYMBOLS, ACTION_DATE
from test_joint_capital_handoff import saved, rebind, digest
from ml.joint_capital_handoff import export_owner_package
from ml.joint_capital_plan import load_owner_package, build_owner_package


def validate(frame, published='2026-09-08T16:30:00Z'):
    return _validated_independent_forecasts(frame, action_date=ACTION_DATE.isoformat(),
        symbols=SYMBOLS, late_publication_at=published)


def test_late_publication_preserves_exact_windows_and_input_times():
    frame = _frame()
    frame['frozen_at'] = pd.Timestamp('2026-09-08T16:00Z')
    before = frame.copy(deep=True)
    with pytest.raises(ValueError, match='future information'):
        validate(frame, None)
    result = validate(frame)
    for column in ['id', 'decision_timestamp', 'information_available_at', 'frozen_at', 'target_window_start', 'target_window_end']:
        pd.testing.assert_series_equal(result[column], before[column])
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize('fault', ['input_at_open', 'decision_after_open', 'future_freeze',
    'next_day', 'before_open', 'naive', 'invalid'])
def test_late_context_does_not_allow_future_inputs_or_fabricated_times(fault):
    frame = _frame()
    frame['frozen_at'] = pd.Timestamp('2026-09-08T16:00Z')
    published = '2026-09-08T16:30Z'
    if fault == 'input_at_open': frame['information_available_at'] = pd.Timestamp('2026-09-08T11:00Z')
    if fault == 'decision_after_open': frame['decision_timestamp'] = pd.Timestamp('2026-09-08T11:01Z')
    if fault == 'future_freeze': frame['frozen_at'] = pd.Timestamp('2026-09-08T16:31Z')
    if fault == 'next_day': published = '2026-09-09T00:00Z'
    if fault == 'before_open': published = '2026-09-08T10:59Z'
    if fault == 'naive': published = '2026-09-08T16:30'
    if fault == 'invalid': published = 'NaT'
    with pytest.raises(ValueError): validate(frame, published)


def test_only_explicit_matching_late_source_supplies_context():
    receipt = {'action_date': '2026-09-08', 'published_at': '2026-09-08T16:30Z'}
    assert late_publication_time({'publication_mode': 'NIGHTLY'}, receipt) is None
    config = {'publication_mode': 'LATE_RECOVERY', 'action_date': '2026-09-08', 'late_action_date': '2026-09-08'}
    assert late_publication_time(config, receipt) == receipt['published_at']
    for changed in [dict(config, late_action_date='2026-09-09'), dict(config, action_date='2026-09-09')]:
        with pytest.raises(ValueError): late_publication_time(changed, receipt)
    with pytest.raises(ValueError): late_publication_time(config, {'action_date': '2026-09-08'})


def test_late_native_export_and_package_reload_preserve_provenance(saved):
    c = saved
    c.frame['frozen_at'] = '2026-09-09T16:00:00Z'
    c.frame.to_parquet(c.game / 'forecasts.parquet', index=False)
    c.gm['configuration'].update(publication_mode='LATE_RECOVERY', late_action_date='2026-09-09')
    c.gr['published_at'] = '2026-09-09T16:20:00Z'
    c.tr['completed_at'] = '2026-09-09T16:30:00Z'
    rebind(c)
    before = {p: digest(p) for folder in (c.game, c.trade) for p in folder.iterdir()}
    output = export_owner_package(c.root, gameplan_run=c.game, trade_plan_run=c.trade,
        owner_id='atlas', output_root=c.out, created_at='2026-09-09T16:40:00Z')
    package = load_owner_package(c.out, output, expected_sha256=output.stem)
    assert package['late_publication_at'] == c.gr['published_at']
    assert {r['id'] for r in package['forecasts']} == set(c.frame.id)
    assert {r['frozen_at'] for r in package['forecasts']} == {'2026-09-09T16:00:00+00:00'}
    assert {p: digest(p) for p in before} == before
    kwargs = {k: package[k] for k in ['owner_id','run_id','source_revision','action_date','frozen_symbols',
        'created_at','source_hashes','forecasts','price_path']}
    with pytest.raises(ValueError, match='future information'):
        build_owner_package(**kwargs)
    kwargs['late_publication_at'] = package['late_publication_at']
    kwargs['created_at'] = '2026-09-09T16:10:00Z'
    with pytest.raises(ValueError, match='postdates'):
        build_owner_package(**kwargs)
