"""Record this UI-only follow-up without changing the concurrent lifecycle."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os

repo = Path('C:/dev/ducketz')
out = repo / 'artifacts/analysis/hyper-model-history-20260930'
now = datetime.now(timezone.utc).isoformat()
maintenance = json.loads(Path('C:/DATASTORE/hyperliquid/_operations/paper-maintenance.json').read_text())
owned = ['app/services/hyperliquid_paper_view.py', 'app/services/hyperliquid_powder_view.py',
         'app/ui/hyper_workspace.py', 'tests/test_hyperliquid_paper_view.py', 'tests/test_hyper_workspace.py']
receipt = {
    'recorded_at_utc': now, 'scope': 'read-only HYPER model history and saved decision diagnostics',
    'tests': {'ui_passed': 60, 'paper_and_powder_service_passed': 109, 'total_passed': 169},
    'retained_data_verification': {'model_rows': 336, 'latest_rows': 24,
        'source': str(out / 'retained-model-history.json'), 'as_of_utc': '2026-09-30T09:07:33+00:00'},
    'visuals': ['peer-retained-1706.png', 'peer-retained-880.png'],
    'source_sha256': {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in owned},
    'changes': ['Per-model five-minute probability and exact-report validation history',
        'Weights, ensemble qualification, expiry, baselines and saved failure reasons',
        'Latest/history and independent asset filters; stable selection/detail scrolling',
        'Decision reasons distinguish recorded stale timestamps and expiry during book collection'],
    'no_mutations_by_this_followup': ['runtime lifecycle', 'model recipe', 'Paper policy', 'accepted opening',
        'cadence assessment or advance', 'automation schedule', 'real accounts or Powder execution'],
    'concurrent_maintenance_observed': maintenance,
    'model_quality_issue_resolved': False,
    'reload_instruction': 'Reopen the Duckets GUI to load Python UI code; Refresh reloads data only.',
}
(out / 'ui-verification.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
note = f'''\n\n## Model history UI follow-up — {now}

Added a separate HYPER model-scores table with per-five-minute saved ensemble/member probabilities, exact model-report weights, Brier/log loss, both baselines, qualification failures and current/expired/invalid state. Latest-per-asset and recent-history views, independent asset filter, and scrollable details retain historical provenance. Decisions now name saved stale-timestamp / expired-during-book-fetch errors; absent historical probabilities remain absent. 60 UI + 109 service/Powder tests passed; 336 retained round12 rows verified against exact reports/predictions, wide/narrow previews checked. Evidence: C:/dev/ducketz/artifacts/analysis/hyper-model-history-20260930/ui-verification.json and diagnosis.md. Reopen Duckets GUI to load code; Refresh only reloads data.

Diagnosis snapshot 09:07:33UTC: all56 published ensembles Research, probabilities47.23–54.88%, none crossed57.5/42.5 entry thresholds; all8 fills startup risk reductions, fees$28.8592049075. Both baseline quality gates fail. Blank02:00:03 decisions fall between prior expiry02:00:00 and next publicationabout02:00:07. Precision/minimum skips were tiny risk-cap trims. Qualification/model-quality issue remains unresolved; no forced trades or softened gates.

This UI follow-up did not score, tune, stop/start, archive, reseed, or reschedule. A separate scheduled owner began round13 at09:08:35.983924UTC; latest observed phase={maintenance.get('phase')}, status={maintenance.get('status')}. Honor that owner; do not recover its intentional maintenance or duplicate assessment. A timely read-only round12 endpoint was retained at artifacts/analysis/hyper-model-history-20260930/round12-endpoint-20260930T090600Z.json; the separate owner already has a pending native round12 assessment. This entry supplements, rather than replaces, its lifecycle handoff. Watch remains observational and cannot score/promote/tune/reseed. Run recorded {now}.
'''
codex_dir = Path(os.environ.get('CODEX_HOME', 'C:/Users/7980X/.codex'))
for automation in ('hyperliquid-paper-improvement', 'hyperliquid-operations-watch'):
    memory = codex_dir / 'automations' / automation / 'memory.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    with memory.open('a', encoding='utf-8') as handle:
        handle.write(note)
with (out / 'diagnosis.md').open('a', encoding='utf-8') as handle:
    handle.write(f'\n\n## UI verification — {now}\n\n'
        'Added the model-history table and exact saved expiry/staleness reasons. '
        '60 UI and 109 service tests passed; all336 retained model rows were matched to their own forecast/report. '
        'Wide and narrow previews are retained beside this audit. Existing Duckets GUI must be reopened to load the code. '
        f'A separate automation currently owns round13 maintenance ({maintenance.get("phase")}); '
        'this follow-up did not change its models, lifecycle or schedule. Model quality remains unresolved.\n')
print(json.dumps({'receipt': str(out / 'ui-verification.json'), 'recorded_at_utc': now,
                  'maintenance_phase': maintenance.get('phase'), 'tests_passed': 169}, indent=2))
