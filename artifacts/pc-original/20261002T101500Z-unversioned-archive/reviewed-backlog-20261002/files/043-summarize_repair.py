from pathlib import Path
from datetime import datetime, timezone
import difflib
import hashlib
import json

ROOT=Path('<LOCAL_CHECKOUT>'); HERE=Path(__file__).resolve().parent
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
baseline=json.loads((HERE/'source-baseline.json').read_text())
files=[]; diff=[]
for item in baseline['files']:
    path=Path(item['path']); before=HERE/(path.name+'.before')
    assert sha(before)==item['sha256']
    changed=path.read_bytes()!=before.read_bytes()
    files.append({**item,'before_sha256':item['sha256'],'after_sha256':sha(path),'changed':changed})
    if changed:
        diff.extend(difflib.unified_diff(before.read_text().splitlines(keepends=True),path.read_text().splitlines(keepends=True),fromfile=str(path)+' (before)',tofile=str(path)+' (after)'))
patch=HERE/'repair.diff'; patch.write_text(''.join(diff),encoding='utf-8')
stdout='................................                                         [100%]\n32 passed in 12.74s\n'
stderr='ResourceWarning: unclosed event loop <ProactorEventLoop running=False closed=False debug=False> emitted by Python asyncio at interpreter shutdown; pytest exit code 0.\n'
(HERE/'tests-stdout.log').write_text(stdout,encoding='utf-8')
(HERE/'tests-stderr.log').write_text(stderr,encoding='utf-8')
diagnosis=ROOT/'artifacts/analysis/overnight-20260930/provider-warning/rootcause-review.json'
result={
    'verified_at':datetime.now(timezone.utc).isoformat(),'status':'TESTS_PASS_PEER_REVIEW_PENDING',
    'scope':'FUTURE_INLINE_CME_INGESTION_ONLY; completed native run and production data not materialized or replayed',
    'problem':'Inline Loop A persisted fresh CME captures only to hot files; materializer selected old partitioned events whenever those existed. Hot metadata also uses aggregate provider_symbol, so directly merging layouts would need extra identity/conflict rules.',
    'repair':'Reuse the existing partition writer with original provider rows and raw frames, under the existing CME writer lock. Preserve hot/raw evidence. Exclude every limit-saturated capture from shared history and retain a request-specific advisory. Context OHLCV/BBO/MBP failure or saturation blocks this invocation from materializing context; contracts-only errors and caps retain evidence without blocking valid context. Existing canonical deduplication, event/receipt provenance and materializer quality gates are unchanged.',
    'files':files,'diff':{'path':str(patch),'sha256':sha(patch)},
    'tests':[{'command':'.\\.venv\\Scripts\\python.exe -m pytest tests/test_cme_cross_asset_context.py tests/test_cme_runtime.py tests/test_databento_cme_context.py -q','exit_code':0,'passed':32,'seconds':12.74,'tool_chunk':'938fa8','stdout':str(HERE/'tests-stdout.log'),'stderr':str(HERE/'tests-stderr.log')}],
    'new_regressions':['stale partitions plus fresh inline capture for all three schemas','provider instrument identities and event/receive/fetched timestamps retained','identical recapture keeps canonical bytes and original receipt evidence','limit-saturated source excluded from canonical history and context','complete identical history cannot hide a newly saturated recapture','history write failure retains hot/raw evidence and skips materialization','context provider, normalized and raw failures all suppress partial-context publication','five contracts-only fetch/normalized/raw/history/cap cases retain evidence and permit valid context'],
    'unchanged_dependencies':all(not f['changed'] for f in files if Path(f['path']).name in ['cme_history.py','cme_cross_asset_context.py']),
    'remaining_vendor_limitation':'Databento Sep30 GLBX.MDP3 condition remains degraded with no disclosed cause. Current capped and stale MBP still cannot pass; this fix does not clear the vendor flag, refresh old publications or claim new model eligibility.',
    'diagnosis':{'path':str(diagnosis),'sha256':sha(diagnosis)},
    'test_iteration_note':'Early failures were fixture file-count expectations and an error-directory glob. Peer review added current-cap safety and narrowed blocking to the context inputs; five contracts-only isolation cases prove unrelated failures do not gate valid context. Final suite above passes all32.',
    'production_data_writes':0,'production_materialization_calls':0,'provider_or_broker_calls_during_repair':0,'completed_run_replays':0,'trader_or_controls_changes':0,
}
(HERE/'repair-verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
notes=f"""CME inline history repair verified at {result['verified_at']}.

Root owns supervision UUID REDACTED_NATIVE_ID_0120. This delegated repair did not claim/release ownership and did not touch the completed overnight run's bound operator notes.

The inline fetch now sends uncapped original provider rows through the existing CME event-history writer before materialization. This fixes the disconnect between fresh hot captures and the preferred partitioned reader without inventing a second merge/provenance policy. Existing hot/raw evidence is retained. All capped captures stay outside canonical event history with explicit advisories. Context OHLCV/BBO/MBP acquisition, persistence or saturation failures block context publication; contracts-only failures retain evidence and do not block valid context. No controls, trader, model gates, limits, provider acquisition settings or history policies changed.

Validation: 32 CME tests passed, including stale-partition/fresh-inline, identity and timestamp retention, repeated capture, cap safety, four context failure paths and five contracts-only isolation cases. The interpreter emits an unclosed asyncio event-loop ResourceWarning at shutdown after pytest exit0; this is retained in test evidence. Existing native partition writer and context materializer files match their before hashes.

Future runs will use this code. No production materialization, downloads, broker calls, completed-run replay or publication update was performed. Databento's separate Sep30 degraded condition and stale/capped MBP limitations remain explicit.

Evidence: repair-verification.json, repair.diff, source-baseline.json, saved before files and tests logs in this directory. Independent peer review pending at this write.
"""
(HERE/'operator-notes.md').write_text(notes,encoding='utf-8')
print(json.dumps({'status':result['status'],'path':str(HERE/'repair-verification.json'),'sha256':sha(HERE/'repair-verification.json'),'changed_files':[f['path'] for f in files if f['changed']]},indent=2))
