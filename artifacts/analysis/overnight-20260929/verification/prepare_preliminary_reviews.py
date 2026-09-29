"""Copy bounded preliminary report reviewers for this native run only."""
from pathlib import Path
import ast
import hashlib
import json

OUT = Path(__file__).resolve().parent
OLD = OUT.parent.parent/'overnight-20260926/verification'
copied = []
for name in ('preliminary_directional_review.py','preliminary_sizing_review.py'):
    source = OLD/name
    value = source.read_text(encoding='utf-8').replace('20260926T040723.834511Z','20260929T040727.779758Z')
    value = value.replace('2026-09-28','2026-09-29').replace('September 28','September 29')
    ast.parse(value,filename=name)
    if (OUT/name).exists():
        raise RuntimeError('Refusing to replace existing helper: '+name)
    (OUT/name).write_text(value,encoding='utf-8')
    copied.append({'name':name,'copied_from':str(source),
        'original_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'adapted_sha256':hashlib.sha256((OUT/name).read_bytes()).hexdigest()})
prepared=json.loads((OUT/'preparation.json').read_text(encoding='utf-8'))
prepared['files'].extend(copied)
(OUT/'preparation.json').write_text(json.dumps(prepared,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'copied':len(copied),'production_mutations':0}))
