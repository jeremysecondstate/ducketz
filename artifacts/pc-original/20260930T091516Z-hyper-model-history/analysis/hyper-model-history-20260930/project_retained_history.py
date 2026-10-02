"""Read an eligible retained archive to verify the UI's historical projection."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.services.hyperliquid_paper_view import HyperliquidPaperViewService, PaperViewSnapshot

archive = Path('C:/DATASTORE/hyperliquid/_paper_archives/20260930-paper-round-13')
asof = '2026-09-30T09:07:33+00:00'
now = datetime.fromisoformat(asof).timestamp()
recipe = {'interval': '5m', 'horizon_bars': 1, 'horizon_minutes': 5,
          'candle_seconds': 300, 'symbols': ['BTC', 'ETH', 'HYPE', 'ZEC']}
snapshot = PaperViewSnapshot(observed_at_utc=asof, market_recipe=recipe)
reader = HyperliquidPaperViewService(archive, clock=lambda: now)
warnings = []
checks = 0
hashes = {}
predictions = {}
for coin in recipe['symbols']:
    model = archive / '_models' / coin / '5m' / 'h1'
    latest = json.loads((model / 'latest_prediction.json').read_text())
    reader._model_history(snapshot, model, latest, coin, recipe, now, warnings)
    predictions[coin] = {row['prediction_id']: row for row in reader._prediction_history(model / 'predictions.parquet', warnings)}
for row in snapshot.model_history:
    model = archive / '_models' / row['coin'] / '5m' / 'h1'
    source = model / 'runs' / row['model_id'] / 'report.json'
    report = json.loads(source.read_text())
    prediction = predictions[row['coin']][row['prediction_id']]
    member = prediction if row['family'] == 'ensemble' else json.loads(prediction['per_model_json'])[row['family']]
    assert row['p_not_down'] == member['p_not_down'] and row['p_down'] == member['p_down']
    assert row['qualified'] is prediction['qualified']
    assert datetime.fromisoformat(row['created_at_utc']).timestamp() <= now
    metrics = report['metrics'][row['family']]
    assert row['brier_score'] == metrics['brier_score']
    assert row['log_loss'] == metrics['log_loss']
    assert row['assessment_rows'] == metrics['rows']
    assert row['metric_source'] == 'exact_model_report'
    for baseline in ('prior_baseline', 'neutral_baseline'):
        assert row['baseline_metrics'][baseline]['brier_score'] == report['metrics'][baseline]['brier_score']
        assert row['baseline_metrics'][baseline]['log_loss'] == report['metrics'][baseline]['log_loss']
    if row['family'] != 'ensemble':
        weights = report['ensemble_weights']
        expected = weights[row['family']] / sum(weights.values())
        assert abs(row['weight'] - expected) < 1e-12
    hashes[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
    checks += 1
policy = json.loads((archive / '_paper/policy.json').read_text())
output = Path(__file__).with_name('retained-model-history.json')
output.write_text(json.dumps({'source': 'retained eligible round12 archive; read-only, not the current runtime',
    'archive': str(archive), 'observed_at_utc': asof, 'market_recipe': recipe,
    'policy': policy, 'model_history': snapshot.model_history, 'warnings': warnings,
    'verified_exact_report_rows': checks, 'report_sha256': hashes}, indent=2, allow_nan=False), encoding='utf-8')
print(json.dumps({'output': str(output), 'rows': len(snapshot.model_history),
                  'latest_rows': sum(row['is_latest'] for row in snapshot.model_history),
                  'exact_report_checks': checks, 'warnings': warnings}))
