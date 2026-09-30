"""Independent exact-hash stock-pipeline isolation audit; no module execution."""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import subprocess

REPO=Path('C:/dev/ducketz')
OUT=Path(__file__).resolve().parent
VERIFY=OUT.parent/'verification'
TARGET='ml.hyperliquid_paper_cadence'
FILE='ml/hyperliquid_paper_cadence.py'

def sha(raw):return hashlib.sha256(raw).hexdigest()

def main():
    baseline=json.loads((VERIFY/'audit-environment-baseline.json').read_text())
    original=subprocess.check_output(['git','show','HEAD:'+FILE],cwd=REPO)
    current=(REPO/FILE).read_bytes()
    paths=[p for scope in ['ml','datafetching','app'] for p in (REPO/scope).rglob('*.py') if '__pycache__' not in p.parts]
    modules={p.relative_to(REPO).with_suffix('').as_posix().replace('/','.').removesuffix('.__init__'):p for p in paths}
    graph={name:set() for name in modules}
    errors=[]
    inbound=[]
    for name,path in modules.items():
        try:tree=ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
        except SyntaxError as exc:
            errors.append({'path':str(path),'error':str(exc)});continue
        package=name if path.name=='__init__.py' else name.rpartition('.')[0]
        for node in ast.walk(tree):
            candidates=[]
            if isinstance(node,ast.Import):candidates=[alias.name for alias in node.names]
            elif isinstance(node,ast.ImportFrom):
                if node.level:
                    parts=package.split('.')
                    base='.'.join(parts[:len(parts)-node.level+1])
                    module='.'.join(x for x in [base,node.module or ''] if x)
                else:module=node.module or ''
                candidates=[module,*[module+'.'+alias.name for alias in node.names if alias.name!='*']]
            for candidate in candidates:
                pieces=candidate.split('.')
                for end in range(1,len(pieces)+1):
                    mod='.'.join(pieces[:end])
                    if mod in modules:
                        graph[name].add(mod)
                        if mod==TARGET:inbound.append({'module':name,'line':node.lineno})
    roots=['ml.overnight_runtime','datafetching.orchestrate','ml.prediction_runtime','ml.stock_target_history',
        'ml.gameplan_evaluation','ml.nightly_gameplan','ml.stock_trader.independent_training',
        'ml.gameplan_trade_planning','ml.gameplan_actuals_review','ml.runtime_pipeline']
    pending=list(roots);closure=set()
    while pending:
        module=pending.pop()
        if module in closure:continue
        closure.add(module);pending.extend(graph.get(module,set())-closure)
    references=[]
    for path in paths:
        if path==REPO/FILE:continue
        for number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
            if 'hyperliquid_paper_cadence' in line:references.append({'file':path.relative_to(REPO).as_posix(),'line':number,'text':line.strip()})
    changed=[]
    for rel,expected in baseline['files'].items():
        path=REPO/rel
        actual=sha(path.read_bytes()) if path.is_file() else None
        if actual!=expected:changed.append({'path':rel,'baseline_sha256':expected,'current_sha256':actual})
    diff=subprocess.check_output(['git','diff','--no-ext-diff','--',FILE],cwd=REPO)
    (OUT/'cadence-isolation-reviewed.diff').write_bytes(diff)
    checks={
        'original_git_bytes_match_saved_baseline':sha(original)==baseline['files'][FILE]=='58bd8e1858f22aead3de3fc7762cbbcd88e640d90839dfd13dc65ce3fedb0427',
        'exact_current_reviewed_hash':sha(current)=='2d28b6eb7ff14fc680ca3d820a4fbd67a64a5511aad14ca6c405806d72ee8544',
        'only_cadence_changed_in_native_baseline':len(changed)==1 and changed[0]['path']==FILE,
        'static_source_parse_success':not errors,
        'all_native_roots_exist':all(name in modules for name in roots),
        'cadence_not_reachable_from_native_roots':TARGET not in closure,
        'no_source_imports_cadence':not inbound,
        'no_other_source_text_references':not references,
        'cadence_main_is_explicit_cli':b"if __name__ == \"__main__\":" in current or b"if __name__ == '__main__':" in current,
    }
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'REVIEW_REQUIRED',
        'scope':'INDEPENDENT_EXACT_HASH_STATIC_IMPORT_AND_DIFF_REVIEW_NO_NATIVE_EXECUTION',
        'checks':checks,'issues':[key for key,value in checks.items() if not value],
        'changed_baseline_sources':changed,'native_entrypoints':roots,'local_static_closure_count':len(closure),
        'local_static_closure':sorted(closure),'cadence_inbound_imports':inbound,'other_source_text_references':references,
        'parsed_source_count':len(modules),'parse_errors':errors,
        'reviewed_diff':{'path':str(OUT/'cadence-isolation-reviewed.diff'),'sha256':sha(diff)},
        'conclusion':'Concurrent change is confined to separate Hyperliquid paper experiment duration policy/CLI. Exact module is not imported or text-referenced by native stock source closure. A single exact-hash exception is justified; original baseline and audit refusal must remain preserved. This does not certify the separate cadence policy or authorize invoking it.',
        'static_analysis_limit':'Checks local Python import graph including package initializers and direct source references; no arbitrary external-package dynamic import proof is claimed.',
        'production_writes':0,'provider_calls':0,'broker_calls':0,'order_actions':0}
    (OUT/'cadence-isolation-peer-review.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'checks':len(checks),'issues':result['issues'],'closure':len(closure),'sources':len(modules)}))
    if result['status']!='PASS':raise SystemExit(1)

if __name__=='__main__':main()
