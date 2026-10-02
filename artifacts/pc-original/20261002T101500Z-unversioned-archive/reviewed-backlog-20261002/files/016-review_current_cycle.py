"""Bounded read-only check of core audit log selection and completed Loop A cycle.

Does not run the heavy final verifier, reset its baseline, or call providers.
The core verifier audits the selected native log; detailed base-cycle/provider
metadata verification remains separate evidence, explicitly checked here.
"""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import re

OUT = Path(__file__).resolve().parent
ROOT = Path('<LOCAL_DATASTORE>')
RUN = ROOT/'ml/overnight-runs/20261001T040644.667536Z'
read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()

helper = OUT/'verify_completed_run.py'
source = helper.read_text(encoding='utf-8')
tree = ast.parse(source)
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'check_fetch_log')
selected_source = ast.get_source_segment(source, function)
assert 'record = context["completed_stages"]["loop_a_close_fetch"]' in selected_source
assert 'log_path = Path(record["overnight_run"]) / "loop_a_close_fetch.log"' in selected_source
assert not re.search(r'pid\d+|2026\d{4}T\d{6}', selected_source)
report = read(RUN/'stage-report.json')
stages = [item for item in report['stages'] if item['stage'] == 'loop_a_close_fetch']
assert len(stages) == 1 and stages[0]['status'] == 'COMPLETE' and stages[0]['exit_code'] == 0
stage = stages[0]
log_path = RUN/stage['log_path']
lines = log_path.read_text(encoding='utf-8').splitlines()
times = [datetime.fromisoformat(line.removeprefix('CYCLE ')) for line in lines if line.startswith('CYCLE ')]
completed = [m[1] for line in lines if (m := re.fullmatch(r'Loop A datastore cycle (\S+): COMPLETE', line))]
current_path, complete_path = ROOT/'.ducketz-loop-a-cycle.json', ROOT/'.ducketz-loop-a-complete.json'
current, complete = read(current_path), read(complete_path)
assert current == complete and complete['status'] == 'COMPLETE' and complete['failure_count'] == 0
assert len(times) == 1 and times[0] == datetime.fromisoformat(complete['started_at'].replace('Z', '+00:00'))
assert datetime.fromisoformat(stage['started_at']) <= times[0] <= datetime.fromisoformat(stage['finished_at'])
prefix = times[0].astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-pid')
assert re.fullmatch(re.escape(prefix)+r'\d+', complete['generation'])
assert completed == [complete['generation']] == ['20261001T040645.482769Z-pid64892']
maintenance = [line for line in lines if line.startswith('Options history maintenance finished:')]
assert len(maintenance) == 1
stats = dict(re.findall(r'(\w+)=([^; ]+)', maintenance[0]))
assert stats['requested_scopes'] == stats['completed_scopes'] == stats['preflighted_scopes'] == '33'
assert all(int(stats[key]) == 0 for key in ('failed_scopes', 'capacity_blocked_scopes', 'deferred_scopes',
                                          'bootstrap_required_scopes', 'live_replay_completed_scopes', 'live_replay_bytes'))
assert float(stats['selected_estimated_cost_usd']) == 0
record = {
    'checked_at': datetime.now(timezone.utc).isoformat(), 'status': 'CURRENT_LOG_SELECTION_AND_CYCLE_VERIFIED',
    'native_run': str(RUN), 'native_status': report['status'], 'completed_loop_a_stage': stage,
    'core_helper': {'path': str(helper), 'sha256': sha(helper), 'selection':
        'completed_stages[loop_a_close_fetch].overnight_run / loop_a_close_fetch.log',
        'stale_cycle_or_pid_constant_in_function': False},
    'verified_cycle': complete['generation'], 'exact_completed_log_identity': completed,
    'current_and_complete_metadata_equal': True, 'opra_summary': stats,
    'evidence': {str(path): {'sha256': sha(path), 'bytes': path.stat().st_size}
                 for path in (log_path, current_path, complete_path)},
    'scope': 'Exact completed native log and compact base-cycle receipt linkage only; no provider/source-archive audit.',
    'core_helper_changes': 0, 'baseline_reset': False, 'heavy_checks_started': False,
    'production_mutations': 0, 'provider_calls': 0, 'orders_placed': 0}
destination = OUT/'current-cycle-binding-review.json'
assert not destination.exists(), 'Refusing to overwrite prior cycle evidence'
destination.write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': record['status'], 'verified_cycle': record['verified_cycle'],
                  'opra_complete': stats['completed_scopes'], 'core_helper_changes': 0,
                  'output': str(destination)}))
