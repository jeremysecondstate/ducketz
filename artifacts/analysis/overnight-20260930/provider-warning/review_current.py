"""Bounded offline provider-warning snapshots; no provider calls or production writes."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import sys

sys.dont_write_bytecode = True
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT/'ml/overnight-runs/20260930T040723.733025Z'
START = pd.Timestamp('2026-09-30T04:07:24.527431Z')
SOURCE_CLOSE = pd.Timestamp('2026-09-30T00:00:00Z')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compact_capture(scope, schema):
    period = scope.lower()+'_'+schema
    path = ROOT/'pools/cme'/scope/period/'databento/normalized'/(scope+'_'+period+'.parquet')
    if path.stat().st_size > 32*1024*1024:
        raise ValueError('COMPACT_CAPTURE_SIZE_BOUND_EXCEEDED')
    data = path.read_bytes()
    source = io.BytesIO(data)
    metadata = pq.read_metadata(source)
    if metadata.num_rows > 300000:
        raise ValueError('COMPACT_CAPTURE_ROW_BOUND_EXCEEDED')
    names = set(pq.read_schema(source).names)
    requested = ['symbol','fetched_at','timestamp','provider_dataset','provider_schema','provider_stype_in',
                 'initial_range_start','initial_range_end','effective_range_start','effective_range_end',
                 'request_limit_saturated','latest_window_shrink_count','cme_schema_status']
    columns = [key for key in requested if key in names]
    frame = pq.read_table(source, columns=columns).to_pandas()
    fetched = pd.to_datetime(frame['fetched_at'], utc=True)
    current = frame.loc[fetched.ge(START)].copy()
    groups = []
    for symbol, rows in current.groupby('symbol'):
        market = pd.to_datetime(rows['timestamp'], utc=True)
        groups.append({'symbol':symbol,'rows':len(rows),'first_market':str(market.min()),
                       'last_market':str(market.max()),'rows_after_stock_source_close':int(market.ge(SOURCE_CLOSE).sum()),
                       'rows_before_stock_source_close':int(market.lt(SOURCE_CLOSE).sum())})
    detail = [key for key in columns if key not in {'symbol','timestamp','fetched_at'}]
    return {'path':str(path),'sha256':digest(data),'file_rows':len(frame),'current_native_rows':len(current),
            'latest_fetched_at':str(fetched.max()),'symbols':groups,
            'current_request_metadata':current[detail].drop_duplicates().to_dict('records'),
            'snapshot_status':'CURRENT_NATIVE_CAPTURE' if len(current) else 'PENDING_CURRENT_NATIVE_CAPTURE'}


def main():
    log_path = RUN/'loop_a_close_fetch.log'
    data = log_path.read_bytes()
    lines = data.decode('utf-8',errors='replace').splitlines()
    warnings = [{'line':n,'text':line} for n,line in enumerate(lines,1)
                if re.search(r'warning|advisory|traceback|error|failed|degraded',line,re.I)]
    reduced = [row for row in warnings if 'reduced quality' in row['text']]
    request_lines = [{'line':n,'text':line} for n,line in enumerate(lines,1)
                     if 'provider.request' in line and 'CME ' in line]
    named_days = sorted(set(re.findall(r'(\d{4}-\d{2}-\d{2}) \(([^)]+)\)', '\n'.join(row['text'] for row in reduced))))
    report = json.loads((RUN/'stage-report.json').read_text(encoding='utf-8-sig'))
    captures = [compact_capture(scope,schema) for scope in ['CME_CONTEXT','CME_CONTRACTS']
                for schema in ['ohlcv-1m','bbo-1m','mbp-10']]
    observed = datetime.now(timezone.utc)
    result = {
        'reviewed_at':observed.isoformat(),'scope':'LOCAL_READ_ONLY_CURRENT_PROVIDER_WARNING_TRIAGE',
        'run':str(RUN),'native_status':report.get('status'),'native_stage':report.get('current_stage'),
        'source_session':'2026-09-29','stock_source_close':SOURCE_CLOSE.isoformat(),
        'log_snapshot':{'path':str(log_path),'bytes':len(data),'sha256':digest(data)},
        'warnings':warnings,'cme_request_log':request_lines,'provider_degraded_dates':named_days,
        'current_cme_captures':captures,
        'classification':'CURRENT_UTC_DAY_PROVIDER_DEGRADED_CAUSE_UNRESOLVED' if reduced else 'NO_PROVIDER_DEGRADED_WARNING',
        'findings':[
            'The reduced-quality warning names September30, the UTC day still in progress at capture; the completed September29 stock action session ends at September30T00:00Z.',
            'Native CME requests deliberately include available current-day context after that stock-source boundary. Native code caps request ends to the lesser of local now and advertised schema availability.',
            'A current-day label and successful request do not prove degraded quality is caused only by partial-day publication. No saved dataset-condition reason snapshot was found; actual vendor quality remains explicitly degraded.',
            'Current per-symbol records and native successful request lines establish observed delivery only, not complete minute coverage or model eligibility. Saturated MBP limits cannot establish full-range completeness.',
            'No provider request, retry, configuration edit or gate change was made. Native completed-artifact audits remain responsible for final coverage and input eligibility.'
        ],
        'new_failure_candidates':[row for row in warnings if any(token in row['text'].lower() for token in ['traceback','status=failed','status=error','runtimeerror:','exception:'])],
        'provider_calls':0,'production_writes':0,'orders_placed':0,
    }
    payload = json.dumps(result,indent=2,default=str)+'\n'
    name = observed.strftime('warning-review-%Y%m%dT%H%M%SZ.json')
    with (OUT/name).open('x',encoding='utf-8') as handle:
        handle.write(payload)
    (OUT/'latest.json').write_text(payload,encoding='utf-8')
    print(json.dumps({'review':str(OUT/name),'warning_lines':len(warnings),'degraded_dates':named_days,
                      'current_capture_rows':{Path(item['path']).stem:item['current_native_rows'] for item in captures},
                      'failure_candidates':len(result['new_failure_candidates'])}))


if __name__=='__main__':
    main()
