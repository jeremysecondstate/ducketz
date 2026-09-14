from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import databento
from datafetching.cme_runtime import load_repository_environment
from datafetching.symbol_onboarding import _now, _write

load_repository_environment()
directory = Path(__file__).resolve().parent
jobs = []
for symbol in ('CROX', 'PATH', 'TWST', 'IONQ'):
    record = json.loads((directory/symbol/'batch-preparation.json').read_text())
    jobs.extend({'symbol': symbol, **job} for job in record['jobs'])

def inspect(job):
    client = databento.Historical(os.environ['DATABENTO_API_KEY'])
    detail = client.batch.get_job_details(job['job_id'])
    return {**job, **{key: detail.get(key) for key in
        ('state', 'progress', 'record_count', 'billed_size', 'actual_size', 'cost_usd')}}

with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(inspect, jobs))
summary = {'checked_at': _now(), 'states': dict(Counter(j['state'] for j in results)), 'jobs': results}
_write(directory/'batch-status.json', summary)
print(json.dumps({'checked_at':summary['checked_at'], 'states':summary['states'],
    'by_symbol': {symbol: [{'schema':j['schema'],'state':j['state'],'progress':j['progress']}
        for j in results if j['symbol']==symbol] for symbol in ('CROX','PATH','TWST','IONQ')}}))
