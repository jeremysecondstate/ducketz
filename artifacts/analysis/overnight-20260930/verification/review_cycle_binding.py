"""Correct audit-only launcher/child assumption using completed native evidence."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re

OUT=Path(__file__).resolve().parent
RUN=Path('C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z')
COMPLETE=Path('C:/DATASTORE/.ducketz-loop-a-complete.json')
read=lambda p:json.loads(p.read_text(encoding='utf-8'))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
original=OUT/'preparation-before-cycle-review.json'
assert not original.exists()
original.write_bytes((OUT/'preparation.json').read_bytes())
prep=read(original)
cycle=read(COMPLETE)
log=(RUN/'loop_a_close_fetch.log').read_bytes()
lines=log.decode('utf-8').splitlines()
starts=[datetime.fromisoformat(line.removeprefix('CYCLE ')) for line in lines if line.startswith('CYCLE ')]
completed=[m[1] for line in lines if (m:=re.fullmatch(r'Loop A datastore cycle (\S+): COMPLETE',line))]
assert len(starts)==1 and starts[0]==datetime.fromisoformat(cycle['started_at'].replace('Z','+00:00'))
assert completed==[cycle['generation']] and cycle['status']=='COMPLETE' and cycle['failure_count']==0
assert cycle['generation']=='20260930T040724.527431Z-pid60120'
review={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'CORRECTED_AUDIT_ONLY_CYCLE_BINDING',
    'initial_inferred_cycle':prep['expected_loop_a_cycle'],'verified_completed_cycle':cycle['generation'],
    'cause':'Initial helper preparation used overnight child launcher PID61720; actual Loop A datastore owner is child PID60120. Native completed log and saved receipt identify the owner.',
    'initial_preparation':{'path':str(original),'sha256':sha(original)},
    'saved_base_receipt':{'path':str(COMPLETE),'sha256':sha(COMPLETE),'payload':cycle},
    'native_log_prefix':{'path':str(RUN/'loop_a_close_fetch.log'),'bytes':len(log),'sha256':hashlib.sha256(log).hexdigest()},
    'heavy_audits_started':False,'audit_failures_before_correction':[],
    'implementation':'Provider helper now requires exact COMPLETE-cycle log identity plus timestamp/generation equality. Pricing helper requires the same already-verified provider cycle.',
    'source_baseline_preserved':True,'production_mutations':0,'provider_calls':0,'orders_placed':0}
(OUT/'cycle-binding-review.json').write_text(json.dumps(review,indent=2)+'\n',encoding='utf-8')
prep['initial_inferred_loop_a_cycle']=prep['expected_loop_a_cycle']
prep['expected_loop_a_cycle']=cycle['generation']
prep['cycle_binding_review']='cycle-binding-review.json'
(OUT/'preparation.json').write_text(json.dumps(prep,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':review['status'],'verified_completed_cycle':cycle['generation']}))
