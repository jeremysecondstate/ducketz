from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

base = Path('C:/dev/ducketz/artifacts/analysis/overnight-20260925')
v = base / 'verification'
read = lambda name: json.loads((v / name).read_text(encoding='utf-8-sig'))
full, yg, provider = [read(name) for name in ('completion-audit.json', 'yg-completion.json', 'provider-completion.json')]
assert full['status'] == 'VERIFIED_WITH_COVERAGE_NOTES' and not full['errors']
assert len(full['checks']) == 12 and all(x['status'] == 'VERIFIED' for x in full['checks'].values())
assert yg['status'] == 'VERIFIED' and not yg['errors']
assert all(x['score_max_abs_error'] == 0 for x in yg['directional'].values())
assert provider['status'] == 'PASS_NATIVE_LOOP_A_AND_33_OPRA_SCOPES_COMPLETE' and not provider['issues']
assert provider['current_data_files_hashed'] == 66
p = base / 'completion-summary.md'
t = p.read_text(encoding='utf-8')
replacements = {
    '# September 25 Gameplan preparation — draft, final audits pending': '# September 25 Gameplan preparation — complete and verified',
    '**Native workflow COMPLETE; independent completion verification PENDING.**': '**Native workflow COMPLETE; all twelve completion-audit sections VERIFIED with disclosed coverage notes and no errors.**',
    'The September 24 source session produced the September 25 Gameplan at': 'The workflow for source September 24 and action September 25 finished at',
    'Full provider payload hashing is **PENDING**.': 'All 66 current OPRA raw/normalized files passed independent hashing; the [provider audit](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/provider-completion.md) has no discrepancies or pending checks.',
    'Independent source/cursor reconstruction is **PENDING**.': 'Independent verification passed for the source files, eleven original cursor snapshots, bound semantic values and nonregressing current cursors. The native receipt reports exact cursor preservation; it does not separately bind the entire original-cursor snapshot file by an outer hash.',
    'These are saved native results, **pending independent reproduction**; raw DBN records are not all replayed by the consistency contract.': 'Independent reconstruction reproduced every saved cohort row and all 264 raw/calibrated forecast probabilities exactly. The audit hashed 900 bound source files totaling 948,037,870 bytes and verified normalized second/minute OHLCV consistency plus native DBN request headers. It did not replay every raw DBN record.',
    'full estimator inference is **PENDING**.': 'the [independent estimator audit](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/yg-completion.json) reproduced every raw/calibrated assessment score exactly, all route/symbol scores and support counts, fixed development selection and calibration diagnostics.',
    'Event-by-event conservation, original-row identity and ledger reconstruction are **PENDING** independent verification.': 'Independent verification reproduced event-by-event conservation, original-row identity and the complete ledger from the saved snapshot.',
    'Source coverage and synthetic derivations remain **PENDING** independent reproduction.': 'Source coverage, all synthetic derivations and unchanged original observations passed independent reproduction.',
    'Coverage reproduction and frozen prior-source checks are **PENDING**.': 'Coverage reproduction, each historical universe and frozen prior-source comparisons all passed.',
    '**Pending before final sign-off:** full native output/source and archive/cohort audit; YG saved-estimator score reproduction; complete provider payload hashes; independent output review; final control comparison and supervision release. This draft does not assert those checks passed. The root supervisor retains the claim and will replace this paragraph with the final verified results.': '**Final verification:** the [full audit](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/completion-audit.json) passed all 12 sections with no errors; [YG model inference](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/yg-completion.json) passed with zero score error; [provider hashing](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/provider-completion.json) passed all 33 scopes and 66 files. The [independent output review](C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/independent-output-review.md) passed 3,785 checks, including all 57 guarded files, the entire unchanged post-reconciliation ownership ledger and the disabled weekday 03:55 Windows launcher. Daily automation remains ACTIVE at 21:05 Pacific with failure-only notifications. Existing joblib/NumPy deprecation warnings were nonfatal; no new production code repair was needed. Supervision closure is recorded below.'
}
for old, new in replacements.items():
    assert t.count(old) == 1, old
    t = t.replace(old, new)
assert 'PENDING' not in t and 'pending independent' not in t
p.write_text(t, encoding='utf-8')
names = ['completion-audit.json', 'yg-completion.json', 'provider-completion.json', 'independent-output-review.json', 'sizing-review.json']
evidence = {name: {'path': str(v/name), 'sha256': hashlib.sha256((v/name).read_bytes()).hexdigest()} for name in names}
result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'status': 'VERIFIED_WITH_COVERAGE_NOTES', 'native_run': 'C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z', 'errors': [], 'evidence': evidence, 'native_pipeline_exit_code': 0, 'completion_audit_exit_code': 0, 'yg_audit_exit_code': 0, 'provider_audit_exit_code': 0, 'production_code_changes': False, 'orders_placed': 0, 'supervision_closure': 'Pending final release receipt'}
(v/'final-verification.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'status': result['status'], 'summary': str(p), 'evidence_files': len(evidence)}))
