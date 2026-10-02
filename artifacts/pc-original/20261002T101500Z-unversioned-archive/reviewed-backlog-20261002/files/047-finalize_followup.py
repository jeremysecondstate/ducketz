from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import tomllib

ROOT = Path(__file__).resolve().parent
NIGHT = ROOT.parent
REPO = Path('<LOCAL_CHECKOUT>')

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def binding(path):
    content = path.read_bytes()
    return {'path': path.as_posix(), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}

prior = read(NIGHT/'final-verification.json')
for item in prior['evidence']:
    actual = binding(Path(item['path']))
    assert actual['sha256'] == item['sha256'] and actual['bytes'] == item['bytes'], item['path']

preservation = read(NIGHT/'preflight/final-preservation.json')
checks = {}
for key in ('control_hashes', 'source_hashes', 'claim_hashes'):
    matches = []
    for path, expected in preservation[key].items():
        p = Path(path)
        if isinstance(expected, dict):
            matches.append(p.exists() == expected['exists'] and (not p.exists() or binding(p)['sha256'] == expected['sha256']))
        else:
            matches.append(binding(p)['sha256'] == expected)
    checks[key] = {'pass': all(matches), 'files': len(matches)}
    assert all(matches), key

before = tomllib.loads((ROOT/'automation-before.toml').read_text(encoding='utf-8'))
after = tomllib.loads(Path('<LOCAL_USER>/.codex/automations/REDACTED_NATIVE_ID_0121/automation.toml').read_text(encoding='utf-8'))
proposal = read(ROOT/'automation-proposal-v2.json')
assert after['prompt'] == proposal['prompt']
assert all(before.get(k) == after.get(k) for k in set(before)|set(after) if k not in ('prompt', 'updated_at'))
checks['automation'] = {'pass': True, 'changed_fields': ['prompt', 'updated_at']}

repair = read(ROOT/'cme-inline-history/repair-verification.json')
peer = read(ROOT/'cme-inline-history/peer-review.json')
assert peer['status'] == 'PASS', peer['status']
assert all(test['exit_code'] == 0 for test in repair['tests'])
for item in repair['files']:
    assert binding(Path(item['path']))['sha256'] == item['after_sha256'], item['path']
checks['cme_repair'] = {'pass': True, 'tests': repair['tests'], 'peer_status': peer['status']}

paths = [
    ROOT/'automation-update-verification.json',
    NIGHT/'preflight/automation-sync-peer-review.json',
    NIGHT/'preflight/policy-provenance-followup.json',
    NIGHT/'provider-warning/rootcause-review.json',
    NIGHT/'provider-warning/quality-metadata-20260930T072037Z.json',
    NIGHT/'verification/limits-review/limits-review.json',
    ROOT/'cme-inline-history/repair-verification.json',
    REPO/'datafetching/databento_fetch.py',
    REPO/'tests/test_cme_cross_asset_context.py',
]
for optional in ('cme-inline-history/peer-review.json', 'supervision-release.json'):
    if (ROOT/optional).exists():
        paths.append(ROOT/optional)
result = {
    'reviewed_at': datetime.now(timezone.utc).isoformat(),
    'status': 'VERIFIED_WITH_EXPLICIT_PROVIDER_AND_MODEL_LIMITATIONS',
    'scope': 'User-requested investigation and focused non-trading repairs after completed overnight publication',
    'checks': checks,
    'completed_publication_evidence_unchanged': len(prior['evidence']),
    'claim_token': 'REDACTED_NATIVE_ID_0120',
    'supervision': read(ROOT/'supervision-release.json') if (ROOT/'supervision-release.json').exists() else 'HELD_FOR_REPAIR_VERIFICATION',
    'changes': [
        'Existing overnight automation now reflects documented September14/16 direction, sparse planning and manual-policy precedence; schedule/settings preserved.',
        'Existing overnight warning triage now requires bounded provider condition/range/source-use evidence.',
        'Fresh eligible inline CME captures enter the shared canonical event history used by feature construction; failed or limit-saturated context captures cannot authorize materialization. Separate contract-capture issues retain their diagnostics without blocking otherwise valid context.',
    ],
    'retained_limits': [
        'Databento September30 GLBX.MDP3 degraded cause remains undisclosed; schema availability lag is a distinct observation.',
        'No current Gameplan model admits CME features. Future corrected routing still depends on source quality, capture completeness and freshness.',
        'All four learned sizing models remain unqualified and unused by the selected manual Gameplan policy.',
        'Directional tolerance qualification is not demonstrated predictive edge; only4h beats both baselines.',
        '35 mature missing forecast rows lack qualifying observed boundaries despite complete acquisition windows;66 later-ending rows remain pending in the saved review.',
        'Synthetic carry-forward remains planning-only and explicitly labeled; actuals, training and live quotes use their own observed inputs.',
    ],
    'native_restarts': 0, 'production_materialization': False, 'trader_starts': 0, 'order_actions': 0,
    'evidence': [binding(p) for p in paths],
}
(ROOT/'followup-verification.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'preservation': 'PASS', 'evidence_files': len(paths), 'supervision': result['supervision']}))
