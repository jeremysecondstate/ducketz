"""Adapt bounded optional Pricing evidence audit into completed native sequence."""
from pathlib import Path
import ast
import hashlib
import json

OUT=Path(__file__).resolve().parent
source=OUT.parent.parent/'overnight-20260926/preflight/review_pricing_gate.py'
target=OUT/'audit_optional_pricing.py'
if target.exists():
    raise RuntimeError('Refusing to replace existing prepared helper')
value=source.read_text(encoding='utf-8')
value=value.replace('20260926T040723.834511Z','20260929T040727.779758Z')
value=value.replace('20260926T040724.729021Z-pid46624','20260929T040728.646690Z-pid62304')
value=value.replace('overnight-20260925/preflight/pricing-gate-review.json','overnight-20260926/preflight/pricing-gate-review.json')
value=value.replace('"""Bounded Pricing-source/gate review while native fitting runs.',
                    '"""Bounded Pricing-source/gate audit after native COMPLETE.')
old="    prior=read(PRIOR)"
new="""    native=read(RUN/'stage-report.json')
    receipt=read(RUN/'receipt.json') if (RUN/'receipt.json').is_file() else {}
    if native.get('status')!='COMPLETE' or receipt.get('status')!='COMPLETE':
        print(json.dumps({'status':'NOT_READY_FOR_FINAL_VERIFICATION','heavy_checks_started':False}))
        raise SystemExit(2)
    from audit_environment import verify_environment
    verify_environment()
    check(receipt.get('stage_report_checksum_sha256')==sha(RUN/'stage-report.json'),'Native report receipt binding differs')
    prior=read(PRIOR)"""
assert value.count(old)==1
value=value.replace(old,new)
old="    gate=None"
new="""    gameplan=ROOT/native['enrichment_gameplan']['run_path']
    gameplan_manifest=read(gameplan/'manifest.json')
    check((ROOT/gameplan_manifest['configuration']['source_loop_b_run']).resolve()==currentrun.resolve(),
          'Pinned Gameplan used a different Loop B generation')
    check(sha(gameplan/'receipt.json')==native['enrichment_gameplan']['receipt_sha256'],
          'Pinned Gameplan receipt changed')
    gate=None"""
assert value.count(old)==1
value=value.replace(old,new)
old="        check(sorted(gate['failed_routes'])==sorted(routes),'Current saved gate routes differ')"
new=old+"\n        check(set(gate['route_gates'])==set(routes) and len(gate['route_gates'])==99,'Saved route gate inventory differs')"
assert value.count(old)==1
value=value.replace(old,new)
old="    stage=read(RUN/'stage-report.json')"
new="    check(gate is not None and compact_bound==51,'Completed Loop B lacks manifest-bound optional Pricing gates')\n"+old
assert value.count(old)==1
value=value.replace(old,new)
value=value.replace("'current_loop_b_completion_verified':False", "'current_loop_b_completion_verified':not issues")
needle="\n\n\nif __name__=='__main__':main()"
assert value.count(needle)==1
value=value.replace(needle,"\n    if issues:\n        raise SystemExit(1)"+needle)
ast.parse(value)
target.write_text(value,encoding='utf-8')
preparation=json.loads((OUT/'preparation.json').read_text(encoding='utf-8'))
preparation['files'].append({'name':target.name,'copied_from':str(source),
    'original_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
    'adapted_sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
(OUT/'preparation.json').write_text(json.dumps(preparation,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'prepared':target.name,'provider_calls':0,'production_mutations':0}))
