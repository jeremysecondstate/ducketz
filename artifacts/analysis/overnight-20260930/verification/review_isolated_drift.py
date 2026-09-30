"""Review exact unrelated concurrent cadence bytes; preserve initial failure."""
from pathlib import Path
from datetime import datetime, timezone
import difflib
import hashlib
import json
import subprocess

OUT=Path(__file__).resolve().parent
REPO=Path('C:/dev/ducketz')
sha=lambda value:hashlib.sha256(value).hexdigest()
baseline_bytes=(OUT/'audit-environment-baseline.json').read_bytes()
baseline=json.loads(baseline_bytes)
name='ml/hyperliquid_paper_cadence.py'
changes={n for n,s in baseline['files'].items() if not (REPO/n).is_file() or sha((REPO/n).read_bytes())!=s}
current={p.relative_to(REPO).as_posix():p for scope in ('ml','datafetching') for p in (REPO/scope).rglob('*.py') if '__pycache__' not in p.parts}
changes.update(set(current)-set(baseline['files']))
assert changes=={name},changes
old=subprocess.check_output(['git','show',baseline['git_head']+':'+name],cwd=REPO)
new=(REPO/name).read_bytes()
assert sha(old)==baseline['files'][name]=='58bd8e1858f22aead3de3fc7762cbbcd88e640d90839dfd13dc65ce3fedb0427'
assert sha(new)=='2d28b6eb7ff14fc680ca3d820a4fbd67a64a5511aad14ca6c405806d72ee8544'
refs=[]
examined=[]
for relative,path in current.items():
    if path.name.startswith('hyperliquid'):
        continue
    examined.append(relative)
    refs += [{'path':relative,'line':number,'text':line.strip()} for number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1) if 'hyperliquid' in line.lower()]
assert not refs,refs
peer_path=OUT.parent/'preflight/cadence-isolation-peer-review.json'
assert peer_path.is_file(),'Wait for independent preflight dependency closure review'
peer=json.loads(peer_path.read_text(encoding='utf-8-sig'))
assert peer['status']=='PASS' and not peer['issues'] and len(peer['checks'])==9 and all(peer['checks'].values())
assert peer['changed_baseline_sources']==[{'path':name,'baseline_sha256':sha(old),'current_sha256':sha(new)}]
assert 'ml.hyperliquid_paper_cadence' not in peer['local_static_closure']
preserve=OUT/'first-audit-attempt'
preserve.mkdir(exist_ok=False)
data=(OUT/'review-watch.json').read_bytes()
(preserve/'review-watch.json').write_bytes(data)
watch=json.loads(data)
assert watch['status']=='STOPPED_REQUIRES_REVIEW'
assert watch['checks'][-1]['script']=='run_final_checks.py' and watch['checks'][-1]['exit_code']==1
assert not (OUT/'audit-runner.json').exists() and not (OUT/'completion-audit.json').exists()
diff=''.join(difflib.unified_diff(old.decode().splitlines(True),new.decode().splitlines(True),fromfile='baseline/'+name,tofile='reviewed/'+name))
(OUT/'reviewed-isolated-code-drift.diff').write_text(diff,encoding='utf-8')
result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'REVIEWED_ISOLATED_FROM_NATIVE_STOCK_PIPELINE',
    'authorization':'Root explicitly authorized this single reviewed-hash exception after independent PASS9 closure review; preserve baseline/refusal and run only pending offline verification.',
    'scope':'Exact unrelated concurrent Hyperliquid cadence source review for offline verification; no native restart, source rebaseline or Hyperliquid execution.',
    'baseline_preserved':True,'baseline_file_sha256':sha(baseline_bytes),'baseline_git_head':baseline['git_head'],
    'changes':[{'path':name,'baseline_sha256':sha(old),'reviewed_sha256':sha(new),'baseline_exact_git_object_verified':True,
        'reason':'Separate Hyperliquid paper experiment-duration rule: versioned loss-minus-two/floor-one policy and explicit amendment CLI. No non-Hyperliquid ml/datafetching source reference; independent preflight native dependency closure reviewed. All other captured source/docs/watchlist bytes match baseline.'}],
    'reference_scan':{'files':sorted(examined),'reference_count':0,'references':refs},
    'independent_dependency_review':{'path':str(peer_path),'sha256':sha(peer_path.read_bytes()),'payload':peer},
    'source_diff':{'path':str(OUT/'reviewed-isolated-code-drift.diff'),'sha256':sha(diff.encode())},
    'preserved_failed_attempt':{'path':str(preserve/'review-watch.json'),'sha256':sha(data),
        'failure':'AUDIT_IMPLEMENTATION_DRIFT_REQUIRES_REVIEW before runner initialization and before any heavy audit.',
        'audit_runner_created':False,'heavy_checks_started':False},
    'limits':['Any later byte change fails the guard again.','This does not validate or execute the unrelated Hyperliquid change.','Original baseline and first stopped observer remain preserved.'],
    'production_mutations':0,'provider_calls':0,'orders_placed':0}
assert (OUT/'audit-environment-baseline.json').read_bytes()==baseline_bytes
(OUT/'reviewed-isolated-code-drift.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'changes':list(changes),'heavy_checks_started':False}))
