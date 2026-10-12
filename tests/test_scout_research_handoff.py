"""Scout prepares its own plan without account reads; joint synthesis owns sizing."""
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum, verify_manifest, write_manifest
from ml.gameplan_trade_planning import publish_trade_plan, RESEARCH_PRODUCER_MODE
from test_account_gameplan_producer_prices import producer_case


def research_plan(case, **changes):
    kwargs = dict(gameplan_run=case.source, clock=lambda: pd.Timestamp('2026-10-05T06:00Z'),
                  price_loader=lambda *a, **k: (pd.DataFrame(), (), {}), research_producer_only=True)
    kwargs.update(changes)
    return publish_trade_plan(case.root, **kwargs)


def test_scout_plan_never_reads_account_or_ownership_and_preserves_forecasts(producer_case, monkeypatch):
    from ml.account_gameplan import config
    from ml.joint_capital_handoff import export_owner_package
    case = producer_case
    # Complete the minimal account-producer fixture with the real handoff fields.
    from ml.gameplan_probability_target import probability_target_metadata
    metadata = probability_target_metadata('raw-price-direction-v1')
    frame = pd.read_parquet(case.source / 'forecasts.parquet')
    for key, value in metadata.items():
        frame[key] = value
    frame.to_parquet(case.source / 'forecasts.parquet', index=False)
    manifest = json.loads((case.source / 'manifest.json').read_text())
    manifest['configuration'].update(metadata)
    write_manifest(case.source, run_timestamp=manifest['run_timestamp'], input_files=[],
        output_files=list(manifest['output_files']), configuration=manifest['configuration'], datastore_root=case.root)
    receipt = json.loads((case.source / 'receipt.json').read_text())
    receipt.update(metadata, schema_version='immutable-overnight-gameplan-receipt-v1',
        run_timestamp=manifest['run_timestamp'], execution_authority='ADVISORY_PAPER_ONLY',
        broker_orders_enabled=False, orders_placed=0, manifest_checksum_sha256=file_checksum(case.source / 'manifest.json'))
    (case.source / 'receipt.json').write_text(json.dumps(receipt))
    (case.root / 'handoff').mkdir()
    before = {p: file_checksum(p) for p in case.source.iterdir()}
    original_load = config.load_account_config
    monkeypatch.setattr(config, 'load_account_config', lambda *a: pytest.fail('Scout needs no account binding'))
    run = research_plan(case)
    manifest = verify_manifest(run)
    assert manifest['configuration']['publication_mode'] == RESEARCH_PRODUCER_MODE
    assert manifest['configuration']['producer_id'] == 'scout'
    assert not (run / 'account-snapshot.json').exists()
    frame = pd.read_parquet(run / 'trade-plan.parquet')
    assert len(frame) == 24 and frame.trade_quantity.isna().all()
    assert frame.loc[frame.execution_eligible, 'trade_price_mid'].eq(10).all()
    monkeypatch.setattr(config, 'load_account_config', original_load)
    from app.ui.gameplan_data import load_gameplan
    view = load_gameplan(case.root)
    assert not view.projection_available and len(view.forecasts) == 24
    assert all(row.quantity is None for row in view.forecasts)
    package = export_owner_package(case.root, gameplan_run=case.source, trade_plan_run=run,
        owner_id='scout', output_root=case.root / 'handoff', created_at='2026-10-05T06:01Z')
    assert json.loads(package.read_text())['owner_id'] == 'scout'
    assert {p: file_checksum(p) for p in before} == before


@pytest.mark.parametrize('extra', [{'snapshot_loader': lambda: None}, {'account_producer_only': True},
    {'expected_account_config': 'a' * 64}, {'refresh_plan': Path('unrelated')}])
def test_research_mode_cannot_accidentally_capture_account_or_mix_modes(producer_case, extra):
    with pytest.raises(ValueError, match='Research-only'):
        research_plan(producer_case, **extra)


def test_research_readiness_accepts_no_cash_projection_but_exact_stats_and_plan(producer_case):
    from ml.nightly_workflow import _research_display
    case = producer_case
    run = research_plan(case)
    stats = case.root / 'ml/gameplan-actuals-review-runs/history'
    stats.mkdir(parents=True)
    (stats / 'receipt.json').write_text('{}')
    reviewed = {'stats': {'run_path': stats.relative_to(case.root).as_posix(),
                         'receipt_sha256': file_checksum(stats / 'receipt.json')}}
    state = {'action_date': '2026-10-05', 'source_session': '2026-10-02', 'symbols': ['AAPL']}
    pinned = {'receipt_sha256': file_checksum(case.source / 'receipt.json')}
    view = SimpleNamespace(session='2026-10-02', symbols=('AAPL',), run_directory=stats)
    output = _research_display(case.root.resolve(), state, case.source.resolve(), pinned, reviewed, view)
    assert output['plan_run'] == str(run)
    assert output['local_research_ready'] and output['joint_projection_pending']
    (stats / 'receipt.json').write_text('{"changed":true}')
    with pytest.raises(ValueError, match='Stats'):
        _research_display(case.root.resolve(), state, case.source.resolve(), pinned, reviewed, view)


def test_workflow_routes_only_scout_planning_to_research_mode(tmp_path, monkeypatch):
    from ml.nightly_workflow import _run_native
    from test_nightly_workflow import _write_native
    calls = []
    def native(root, **kwargs):
        calls.append(kwargs)
        run = root / 'ml/overnight-runs' / str(len(calls)); _write_native(run); return run
    monkeypatch.setattr('ml.overnight_runtime.run_overnight_pipeline', native)
    for actor in ('Scout', 'Atlas'):
        state = {'deadline_at': '2026-10-06T11:00Z', 'source_session': '2026-10-05',
                 'steps': {'train_and_plan': {}, 'model_review': {'output': {'proposal': 'fixture'}}}}
        _run_native({'actor': actor, 'repository': str(tmp_path), 'datastore': str(tmp_path)}, state,
                    'train_and_plan', lambda: None)
    assert calls[0]['research_producer_only'] is True
    assert calls[1]['research_producer_only'] is False


def test_native_planning_command_carries_research_role_without_account_flags(tmp_path, monkeypatch):
    from test_overnight_supervision import run, _synthetic_gameplan_pin
    calls = []
    monkeypatch.setattr('ml.overnight_runtime._pin_stock_gameplan', _synthetic_gameplan_pin)
    monkeypatch.setattr('ml.overnight_runtime._run_stage', lambda command, **kw: calls.append(command) or 0)
    result = run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
                 start_at='gameplan_trade_planning', research_producer_only=True)
    assert len(calls) == 1 and '--research-producer-only' in calls[0]
    assert '--account-producer-only' not in calls[0]
    assert json.loads((result / 'stage-report.json').read_text())['research_producer_only'] is True
