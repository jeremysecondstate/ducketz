"""Verify the native collector against the retained empty-interval evidence."""
import json
import os
from pathlib import Path
from dataclasses import asdict
import databento
from datafetching.cme_runtime import load_repository_environment
from datafetching import databento_opra_history as h

load_repository_environment()
folder = Path(__file__).resolve().parent
evidence = json.loads((folder/'empty-options-day-evidence.json').read_text(encoding='utf-8'))
assert evidence['approved_scope_cost_usd'] == 0
client = databento.Historical(os.environ['DATABENTO_API_KEY'])
reports = []
result = h._execute_stream_plan(
    client, datastore_root=folder/'empty-options-day-verification', entitlement={},
    symbols=('CROX.OPT',), plan=[('cbbo-1s','2025-11-27')],
    reporter=reports.append, fail_fast=True,
)
assert result.skipped_partitions == 1 and result.completed_partitions == 0 and not result.errors
out = {'status':'PASSED','native_result':asdict(result),'reports':reports}
(folder/'empty-options-day-verification.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
print(json.dumps(out))
