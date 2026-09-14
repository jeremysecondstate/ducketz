"""Preserve the already-verified baseline before finalizing the candidate."""
import json
from pathlib import Path
from datafetching.research_publication import POINTERS
from datafetching.symbol_onboarding import _checksum, _write
from ml.artifacts import file_checksum, verify_manifest

folder=Path(__file__).resolve().parent
root=Path('C:/DATASTORE')
restored=json.loads((folder/'production-restoration.json').read_text(encoding='utf-8'))
baseline={name:json.loads((root/name).read_text(encoding='utf-8')) for name in POINTERS[:2]}
for name in POINTERS[:2]:
    assert file_checksum(root/name)==restored['pointer_sha256'][str(Path(name))]
run=root/restored['trade_plan_run']
verify_manifest(run)
receipt=json.loads((run/'receipt.json').read_text(encoding='utf-8'))
baseline[POINTERS[2]]={'schema_version':receipt['schema_version'], 'current':{
    'action_date':receipt['action_date'], 'receipt_sha256':file_checksum(run/'receipt.json'),
    'run_path':receipt['run_path'], 'source_receipt_sha256':receipt['source_receipt_sha256']}}
assert baseline[POINTERS[2]]['current']['source_receipt_sha256']==baseline[POINTERS[0]]['current']['receipt_checksum_sha256']
_write(folder/'production-baseline.json', {'plan_id':restored['plan_id'], 'pointers':baseline, 'sha256':_checksum(baseline)})
print('Verified prior seven-symbol pointers saved; no current pointer changed.')
