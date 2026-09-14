"""Warm verified native batch caches without writing canonical data or job state.

The running onboarding worker remains the sole canonical publisher. Download
into a separate mirror, then atomically create cache entries without replacing
any existing file. Native consumers independently verify these same hashes.
"""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil

import databento
from datafetching import databento_opra_history as h
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_cold_start import _request_kwargs
from datafetching.research_onboarding import load_batch
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_onboarding import load_plan, _write, _now

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--schema')
parser.add_argument('--symbols', nargs='+')
parser.add_argument('--label', default='prefetch')
args = parser.parse_args()
if not args.label.replace('-', '').isalnum():
    raise ValueError('Receipt label must be a simple local name')
load_repository_environment()
folder = Path(__file__).resolve().parent
batch = load_batch(folder/'plan.json')
root = Path(batch['datastore_root']).resolve()
owner = 'c5c26345-be22-4abc-a32a-995470a535ac'
mirror = root/'.research-prefetch'/batch['plan_id']
client = databento.Historical(os.environ['DATABENTO_API_KEY'])
receipt_path = folder/f'{args.label}-receipt.json'
prior = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else None
if prior and prior.get('plan_id') != batch['plan_id']:
    raise ValueError('Existing receipt belongs to another batch')
receipts = prior['jobs'] if prior else []

def check_owner():
    lease = json.loads((root/'ml/overnight-supervision.json').read_text(encoding='utf-8'))
    if lease.get('owner_token') != owner or datetime.fromisoformat(lease['expires_at']) <= datetime.now(timezone.utc):
        raise RuntimeError('The canonical onboarding owner is no longer supervising this prefetch')
    if shutil.disk_usage(root).free < batch['budget']['required_free_bytes']:
        raise RuntimeError('Prefetch capacity reserve is unavailable')

with exclusive_runtime_lock(folder/'prefetch.lock', process_name='Research batch cache prefetch'):
    for symbol in batch['selected_symbols']:
        if args.symbols and symbol not in args.symbols:
            continue
        plan = load_plan(folder/symbol/'plan.json')
        for request in plan['requests']:
            if request['dataset'] != 'OPRA.PILLAR':
                continue
            schema, parents = request['schema'], request['symbol_scope']
            if args.schema and schema != args.schema:
                continue
            for state_path, state in h._batch_job_states(root, schema=schema, symbols=parents):
                if state['status'] == 'COMPLETE':
                    continue
                scope = state['request']
                if str(scope['start']) < request['start'] or str(scope['end']) > request['end']:
                    continue
                check_owner()
                if client.metadata.get_cost(**_request_kwargs(request)) != 0:
                    raise RuntimeError('Prefetch scope no longer quotes zero cost')
                details = client.batch.get_job_details(state['job_id'])
                if details.get('state') != 'done':
                    continue
                files, _ = h._batch_data_file_inventory(
                    client.batch.list_files(state['job_id']), planned_dates=state['planned_dates'],
                    request_start=scope['start'], request_end=scope['end'],
                )
                needed = []
                for info in files:
                    target = h._batch_local_file(root, state=state, schema=schema, symbols=parents, filename=info['filename'])
                    canonical = h.partition_directory(root, schema=schema, day=info['day'], symbols=parents)
                    if canonical.is_dir():
                        continue
                    if target.is_file():
                        h._verify_batch_source_file(target, info)
                    else:
                        needed.append(info)
                if not needed:
                    continue
                print(f'PREFETCH {symbol}/{schema} job={state["job_id"]} files={len(needed)}', flush=True)
                h._download_batch_archive(client, datastore_root=mirror, state=state,
                    schema=schema, symbols=parents, required_files=needed)
                check_owner()
                linked = 0
                for info in needed:
                    source = h._batch_local_file(mirror, state=state, schema=schema, symbols=parents, filename=info['filename'])
                    target = h._batch_local_file(root, state=state, schema=schema, symbols=parents, filename=info['filename'])
                    if not source.resolve().is_relative_to(mirror) or not target.resolve().is_relative_to(h.canonical_root(root)/'.staging'):
                        raise RuntimeError('Prefetch path escaped its native cache')
                    h._verify_batch_source_file(source, info)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source, target)  # Atomic create; never replaces a concurrent download.
                        linked += 1
                    except FileExistsError:
                        pass
                    source.unlink()  # Verified temporary mirror; the native cache/consumer owns its copy.
                receipts.append({'symbol':symbol,'schema':schema,'job_id':state['job_id'],
                    'linked_files':linked,'verified_bytes':sum(int(i['size']) for i in needed),'completed_at':_now()})
                _write(folder/f'{args.label}-progress.json', {'plan_id':batch['plan_id'],'jobs':receipts})
                print(f'CACHED {symbol}/{schema} files={linked}', flush=True)
_write(receipt_path, {'plan_id':batch['plan_id'],'status':'COMPLETE','jobs':receipts,'completed_at':_now()})
