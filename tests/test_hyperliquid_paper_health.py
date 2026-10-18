"""Frequency handoff must not accept fresh-looking artifacts from the old recipe."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.hyperliquid_model_config import load_config
from ml.hyperliquid_paper_health import configured_slot_health
from ml import hyperliquid_paper_health as health
from tests.test_hyperliquid_paper_review import runtime_context
from app.services.hyperliquid_paper_view import SourceState

NOW = datetime(2026, 9, 28, 23, 0, 30, tzinfo=timezone.utc).timestamp()
DATA_ID = '20260928T230005Z-1234abcd'
MODEL_ID = '20260928T225500Z-abcdef12'


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def context(tmp_path):
    root, project = tmp_path / 'data', tmp_path / 'project'
    root.mkdir()
    config = project / 'configs'
    write(config / 'hyperliquid-markets.json', {
        'version': 1, 'symbols': ['BTC', 'ETH'], 'interval': '5m', 'output_root': str(root)})
    write(config / 'hyperliquid-models.json', {
        'version': 1, 'markets_config': 'hyperliquid-markets.json', 'horizons_bars': [1],
        'calibration_c': .1, 'logistic_weight': .4, 'extra_trees_weight': .2,
        'hist_gradient_boosting_weight': .2, 'mlp_weight': .2})
    write(config / 'hyperliquid-paper.json', {
        'version': 1, 'model_config': 'hyperliquid-models.json', 'data_root': str(root),
        'require_qualified_forecasts': True, 'max_forecast_age_seconds': 300})
    recipe = load_config(config / 'hyperliquid-models.json')
    forecasts = []
    for coin in ('BTC', 'ETH'):
        model = root / '_models' / coin / '5m' / 'h1'
        forecast = {'prediction_id': 'forecast-' + coin, 'coin': coin, 'interval': '5m', 'horizon_bars': 1,
                    'p_not_down': .51, 'p_down': .49, 'created_at_utc': utc(NOW-20),
                    'decision_close_utc': utc(NOW-30), 'target_close_utc': utc(NOW+270),
                    'qualified': False, 'role': 'research_candidate', 'model_id': MODEL_ID, 'data_run_id': DATA_ID}
        write(model / 'latest_prediction.json', forecast)
        write(model / 'runs' / MODEL_ID / 'record.json', {
            'coin': coin, 'interval': '5m', 'horizon_bars': 1, 'model_id': MODEL_ID,
            'trained_at_utc': utc(NOW-600), 'eligible': False, 'settings': asdict(recipe.model_settings(1))})
        data = root / coin / '5m' / 'runs' / DATA_ID
        data.mkdir(parents=True)
        pd.DataFrame({'close_time': [pd.Timestamp(utc(NOW-30))], 'close': [100.],
                      'volatility_log_return_20': [.01]}).to_parquet(data / 'features.parquet')
        forecasts.append(forecast)
    snapshot = SimpleNamespace(
        market_recipe={'interval': '5m', 'horizon_bars': 1, 'symbols': ['BTC', 'ETH']},
        runtime={'horizon_bars': 1, 'coordinator': {'interval': '5m', 'symbols': ['BTC', 'ETH'],
                  'markets': {'BTC/5m': {}, 'ETH/5m': {}}},
                 'models': {'horizons_bars': [1], 'markets': {'BTC/5m/h1': {}, 'ETH/5m/h1': {}}}},
        forecasts=forecasts)
    return root, project, snapshot


def test_five_minute_slots_include_research_without_requiring_promotion(context):
    report = configured_slot_health(*context, NOW)
    assert report['result'] == 'passed'
    assert report['configured_recipe']['interval'] == '5m'
    assert report['configured_recipe']['paper_horizon_minutes'] == 5
    assert set(report['slots']) == {'BTC/5m/h1', 'ETH/5m/h1'}
    assert all(row['qualified'] is False and row['valid_until_epoch'] == NOW+270
               for row in report['slots'].values())


@pytest.mark.parametrize('component', ['coordinator', 'models', 'paper', 'workspace', 'coverage'])
def test_rejects_stale_runtime_or_ui_contract_even_when_new_artifacts_are_fresh(context, component):
    root, project, snapshot = context
    if component == 'coordinator':
        snapshot.runtime['coordinator'].update(interval='15m', markets={'BTC/15m': {}, 'ETH/15m': {}})
    elif component == 'models':
        snapshot.runtime['models'].update(horizons_bars=[4], markets={'BTC/15m/h4': {}, 'ETH/15m/h4': {}})
    elif component == 'paper':
        snapshot.runtime['horizon_bars'] = 4
    elif component == 'workspace':
        snapshot.market_recipe.update(interval='15m', horizon_bars=4)
    else:
        snapshot.forecasts.pop()
    report = configured_slot_health(root, project, snapshot, NOW)
    assert report['result'] == 'failed'
    assert report['errors']
    assert all(row['result'] == 'passed' for row in report['slots'].values())


def test_old_forecast_cannot_fill_a_missing_configured_five_minute_slot(context):
    root, project, snapshot = context
    path = root / '_models/ETH/5m/h1/latest_prediction.json'
    old = json.loads(path.read_text())
    old.update(interval='15m', horizon_bars=4)
    write(path, old)
    report = configured_slot_health(root, project, snapshot, NOW)
    assert report['result'] == 'failed'
    assert 'different market or horizon' in report['slots']['ETH/5m/h1']['error']


def test_five_minute_prediction_maturity_is_checked_in_addition_to_publication_freshness(context):
    report = configured_slot_health(*context, NOW+270)
    assert report['result'] == 'failed'
    assert all('already matured' in row['error'] for row in report['slots'].values())


def test_artifact_must_match_selected_calibration_and_weight_recipe(context):
    root, project, snapshot = context
    path = root / '_models/BTC/5m/h1/runs' / MODEL_ID / 'record.json'
    record = json.loads(path.read_text())
    record['settings']['calibration_c'] = 1.0
    write(path, record)
    report = configured_slot_health(root, project, snapshot, NOW)
    assert report['result'] == 'failed'
    assert 'configured settings' in report['slots']['BTC/5m/h1']['error']


def test_different_datastore_cannot_pass_using_current_workspace_snapshot(context):
    root, project, snapshot = context
    path = project / 'configs/hyperliquid-paper.json'
    recipe = json.loads(path.read_text())
    recipe['data_root'] = str(root / 'other')
    write(path, recipe)
    report = configured_slot_health(root, project, snapshot, NOW)
    assert report['result'] == 'failed'
    assert 'datastore differs' in report['errors'][0]


@pytest.mark.parametrize('problem', [None, 'source', 'forecast_source', 'latest_source', 'early_fit'])
def test_strict_health_requires_same_source_fit_as_paper(context, problem):
    root, project, snapshot = context
    path = project / 'configs/hyperliquid-models.json'
    config = json.loads(path.read_text())
    config['retrain_before_predict'] = True
    write(path, config)
    for coin in ('BTC', 'ETH'):
        model = root / '_models' / coin / '5m/h1'
        forecast = json.loads((model / 'latest_prediction.json').read_text())
        forecast['model_source_run_id'] = DATA_ID
        record_path = model / 'runs' / MODEL_ID / 'record.json'
        record = json.loads(record_path.read_text())
        record.update(source_run_id=DATA_ID, trained_at_utc=utc(NOW-25))
        latest = {'run_id': DATA_ID}
        if coin == 'BTC':
            if problem == 'source':
                record['source_run_id'] = 'other-source'
            elif problem == 'forecast_source':
                forecast.pop('model_source_run_id')
            elif problem == 'latest_source':
                latest['run_id'] = 'new-source'
            elif problem == 'early_fit':
                record['trained_at_utc'] = utc(NOW-31)
        write(model / 'latest_prediction.json', forecast)
        write(record_path, record)
        write(root / coin / '5m/latest.json', latest)
    report = configured_slot_health(root, project, snapshot, NOW)
    assert report['result'] == ('passed' if problem is None else 'failed')
    assert report['slots']['ETH/5m/h1']['result'] == 'passed'
    if problem:
        assert report['slots']['BTC/5m/h1']['result'] == 'failed'


@pytest.mark.parametrize('foreign_owner', [False, True])
def test_trading_health_main_verifies_unified_owner_and_default_command(runtime_context, monkeypatch, tmp_path, foreign_owner):
    root, project, states, owner, process, now = runtime_context
    write(project / 'configs/hyperliquid-powder.json', {'enabled': False})
    paper = {**states['paper'], 'lifecycle_phase': 'trading', 'process_alive': True,
             'models': states['models'], 'coordinator': states['coordinator'], 'paper_loop': owner}
    snapshot = SimpleNamespace(runtime=paper, powder=SimpleNamespace(runtime={}, sources={}, warnings=[]),
        observed_at_utc=utc(now), portfolio_observed_at_utc=utc(now),
        seed={'timestamp_utc': utc(now-60), 'baseline_equity': {'alex': 100}},
        pooled={}, sources={}, warnings=[])
    if foreign_owner:
        owner['pid'] += 1
        write(root / '_paper_loop/status.json', owner)
    monkeypatch.setattr(health, 'PROJECT', project)
    monkeypatch.setattr(health.psutil, 'Process', lambda pid: process)
    monkeypatch.setattr(health, 'HyperliquidWorkspaceViewService',
                        lambda *args, **kwargs: SimpleNamespace(load_snapshot=lambda: snapshot))
    monkeypatch.setattr(health, 'configured_slot_health', lambda *args: {'result': 'passed'})
    output = tmp_path / 'reports'
    monkeypatch.setattr(health.sys, 'argv', ['health', '--label', 'unified', '--expect', 'trading',
                        '--root', str(root), '--output-dir', str(output)])
    if foreign_owner:
        with pytest.raises(AssertionError):
            health.main()
        assert not (output / 'unified.json').exists()
    else:
        health.main()
        report = json.loads((output / 'unified.json').read_text())
        assert report['result'] == 'passed'
        assert report['runtime']['paper_loop']['pid'] == 123
        assert all(report['process_identity'][key]['process_owner'] == 'paper_loop'
                   for key in ('paper', 'models', 'coordinator'))


def test_standalone_trading_health_does_not_require_historical_unified_source(runtime_context, monkeypatch, tmp_path):
    root, project, states, owner, process, now = runtime_context
    write(project / 'configs/hyperliquid-powder.json', {'enabled': False})
    owner['status'] = 'stopped'
    write(root / '_paper_loop/status.json', owner)
    processes = {}
    for index, (role, module) in enumerate((('paper', 'ml.hyperliquid_paper_runtime'),
                                          ('models', 'ml.hyperliquid_model_runtime'),
                                          ('coordinator', 'ml.hyperliquid_coordinator')), start=200):
        states[role]['pid'] = index
        processes[index] = SimpleNamespace(pid=index,
            cmdline=lambda module=module: ['python', '-B', '-m', module],
            cwd=lambda: str(project), create_time=lambda: now-100, parents=lambda: [])
    paper = {**states['paper'], 'lifecycle_phase': 'trading', 'process_alive': True,
             'models': states['models'], 'coordinator': states['coordinator'], 'paper_loop': owner}
    snapshot = SimpleNamespace(runtime=paper, powder=SimpleNamespace(runtime={}, sources={}, warnings=[]),
        observed_at_utc=utc(now), portfolio_observed_at_utc=utc(now),
        seed={'timestamp_utc': utc(now-60), 'baseline_equity': {'alex': 100}}, pooled={},
        sources={'paper_loop': SourceState(name='paper_loop', state='stopped')}, warnings=[])
    monkeypatch.setattr(health, 'PROJECT', project)
    monkeypatch.setattr(health.psutil, 'Process', lambda pid: processes[pid])
    monkeypatch.setattr(health, 'HyperliquidWorkspaceViewService',
                        lambda *args, **kwargs: SimpleNamespace(load_snapshot=lambda: snapshot))
    monkeypatch.setattr(health, 'configured_slot_health', lambda *args: {'result': 'passed'})
    output = tmp_path / 'reports'
    monkeypatch.setattr(health.sys, 'argv', ['health', '--label', 'standalone', '--expect', 'trading',
                        '--root', str(root), '--output-dir', str(output)])
    health.main()
    report = json.loads((output / 'standalone.json').read_text())
    assert report['result'] == 'passed'
    assert report['sources']['paper_loop']['state'] == 'stopped'
    assert all(report['process_identity'][key]['process_owner'] == 'standalone'
               for key in ('paper', 'models', 'coordinator'))
