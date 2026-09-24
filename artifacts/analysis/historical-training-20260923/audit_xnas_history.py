"""Read-only XNAS OHLCV inventory and native-second endpoint feasibility.

Only this audit directory receives output. No provider, broker, model fitting,
archive mutation, cache write, activation, or new symbol selection occurs.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gc
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path('C:/DATASTORE/market-data/databento/us-equities/XNAS.ITCH')
OUT = Path(__file__).resolve().parent
SCHEMAS = ('ohlcv-1d', 'ohlcv-1h', 'ohlcv-1m', 'ohlcv-1s')
WATCHLIST = Path('C:/dev/ducketz/datafetching/watchlist.txt')

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()

def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def utc(value):
    return pd.Timestamp(value).tz_localize('UTC') if pd.Timestamp(value).tzinfo is None else pd.Timestamp(value).tz_convert('UTC')

def parquet_statistics(path, timestamp_column):
    file = pq.ParquetFile(path)
    fields = [timestamp_column, 'symbol', 'open', 'high', 'low', 'close', 'volume']
    output = {'rows':file.metadata.num_rows, 'row_groups':file.metadata.num_row_groups,
              'columns':file.schema_arrow.names, 'statistics':{}}
    for field in fields:
        if field not in file.schema_arrow.names:
            raise ValueError('Missing normalized field ' + field)
        index = file.schema_arrow.names.index(field)
        stats = [file.metadata.row_group(i).column(index).statistics for i in range(file.metadata.num_row_groups)]
        complete = all(s is not None and s.has_min_max for s in stats)
        output['statistics'][field] = {'complete_row_group_statistics':complete,
            'minimum':min(s.min for s in stats) if complete and stats else None,
            'maximum':max(s.max for s in stats) if complete and stats else None,
            'null_count':sum(s.null_count for s in stats) if all(s is not None and s.null_count is not None for s in stats) else None}
    return output

def second_minutes(path):
    """Aggregate observed seconds for comparison only; retain real endpoint clocks."""
    columns = ['ts_event','open','high','low','close','volume']
    pieces, tail = [], pd.DataFrame()
    last_ts = None
    for batch in pq.ParquetFile(path).iter_batches(batch_size=131072, columns=columns):
        frame = batch.to_pandas()
        if 'ts_event' not in frame and frame.index.name == 'ts_event':
            frame = frame.reset_index()
        frame['ts_event'] = pd.to_datetime(frame.ts_event, utc=True)
        if not frame.ts_event.is_monotonic_increasing or (last_ts is not None and frame.ts_event.iloc[0] <= last_ts):
            raise ValueError('Second observations are nonmonotonic/overlapping')
        last_ts = frame.ts_event.iloc[-1]
        frame = pd.concat([tail,frame], ignore_index=True)
        frame['invalid_observation'] = frame[['open','high','low','close']].isna().any(axis=1) | frame[['open','high','low','close']].le(0).any(axis=1)
        frame['minute'] = frame.ts_event.dt.floor('min')
        final_minute = frame.minute.iloc[-1]
        tail = frame.loc[frame.minute.eq(final_minute)].drop(columns='minute')
        complete = frame.loc[frame.minute.ne(final_minute)]
        if len(complete):
            pieces.append(complete.groupby('minute',sort=True).agg(
                open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),
                volume=('volume','sum'),observed_seconds=('ts_event','size'),invalid_seconds=('invalid_observation','sum'),
                first_observed_second=('ts_event','first'),last_observed_second=('ts_event','last')))
    if len(tail):
        tail['minute'] = tail.ts_event.dt.floor('min')
        pieces.append(tail.groupby('minute',sort=True).agg(
            open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),
            volume=('volume','sum'),observed_seconds=('ts_event','size'),invalid_seconds=('invalid_observation','sum'),
            first_observed_second=('ts_event','first'),last_observed_second=('ts_event','last')))
    result = pd.concat(pieces).reset_index() if pieces else pd.DataFrame()
    if result.minute.duplicated().any():
        raise ValueError('Second aggregation produced duplicate minute identity')
    result['actual_open_observed_at'] = result.first_observed_second
    result['actual_close_observed_at'] = result.last_observed_second + pd.Timedelta(seconds=1)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-hash-budget-bytes',type=int,default=1024**3)
    parser.add_argument('--raw-file-limit-bytes',type=int,default=128*1024**2)
    parser.add_argument('--skip-second-comparison',action='store_true')
    args = parser.parse_args()
    watchlist = [s.strip().upper() for s in WATCHLIST.read_text().splitlines() if s.strip() and not s.strip().startswith('#')]
    plans, preliminary = [], defaultdict(lambda:{'partitions':0,'raw_bytes':0,'normalized_bytes':0,'rows':0,'symbols':set()})
    # Inventory metadata and payload sizes before reading any raw payload bytes.
    for schema in SCHEMAS:
        for path in sorted((ROOT/schema).rglob('manifest.json')):
            if '.staging' in path.parts:
                continue
            manifest = load(path)
            request = manifest['request']
            plan = {'manifest_path':path,'manifest':manifest,'schema':schema,'symbol':request['symbol_scope'][0]}
            plans.append(plan)
            s = preliminary[schema]
            s['partitions'] += 1
            s['raw_bytes'] += manifest['raw']['size_bytes']
            s['normalized_bytes'] += manifest['normalized']['size_bytes']
            s['rows'] += manifest['normalized']['row_count']
            s['symbols'].add(plan['symbol'])
    for s in preliminary.values():
        s['symbols'] = sorted(s['symbols'])
    preflight = {'observed_at':datetime.now(timezone.utc).isoformat(), 'schemas':dict(preliminary),
        'raw_bytes_total':sum(s['raw_bytes'] for s in preliminary.values()),
        'normalized_bytes_total':sum(s['normalized_bytes'] for s in preliminary.values()),
        'largest_raw_file_bytes':max(p['manifest']['raw']['size_bytes'] for p in plans),
        'raw_hash_budget_bytes':args.raw_hash_budget_bytes,'raw_file_limit_bytes':args.raw_file_limit_bytes,
        'provider_cost_usd':0,'provider_requests':0,'note':'Local filesystem I/O estimate only; no provider acquisition or cost request.'}
    (OUT/'audit-io-preflight.json').write_text(json.dumps(preflight,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'phase':'IO_PREFLIGHT',**{k:preflight[k] for k in ('raw_bytes_total','normalized_bytes_total','largest_raw_file_bytes')}}),flush=True)
    import databento as db
    findings, partitions, raw_used = [], [], 0
    for plan in plans:
        path, m, schema, symbol = plan['manifest_path'],plan['manifest'],plan['schema'],plan['symbol']
        directory, request = path.parent,m['request']
        entry = {'manifest_path':str(path),'schema':schema,'symbol':symbol,'request':request,
                 'production_symbol':symbol in watchlist,'issues':[],'checks':{}}
        def test(name,value):
            entry['checks'][name] = bool(value)
            if not value:
                entry['issues'].append(name)
        try:
            receipt_path = directory/'receipt.json'
            rc = load(receipt_path)
            test('manifest_schema',m['schema_version']=='databento-cold-start-partition-v1')
            test('receipt_schema',rc['schema_version']=='databento-cold-start-receipt-v1')
            test('manifest_binding',sha(path)==rc['manifest_checksum_sha256'])
            test('request_identity',request['dataset']=='XNAS.ITCH' and request['schema']==schema and request['stype_in']=='raw_symbol' and request['symbol_scope']==[symbol] and request['request_id']==rc['request_id'])
            test('root_identity',directory.is_relative_to(ROOT/schema/symbol) and '.staging' not in directory.parts)
            test('request_range',utc(request['start'])<utc(request['end']))
            test('publication_binding',m['published_at']==rc['published_at'])
            payloads = {}
            for kind in ('normalized','raw'):
                file = (directory/m[kind]['path']).resolve()
                test(kind+'_path',file.is_relative_to(directory.resolve()))
                test(kind+'_size',file.is_file() and file.stat().st_size==m[kind]['size_bytes'])
                test(kind+'_receipt_hash_binding',m[kind]['checksum_sha256']==rc[kind+'_checksum_sha256'])
                payloads[kind]=file
                entry[kind+'_path']=str(file)
                entry[kind+'_bytes']=m[kind]['size_bytes']
            test('normalized_payload_hash',sha(payloads['normalized'])==m['normalized']['checksum_sha256'])
            stats = parquet_statistics(payloads['normalized'],m['normalized']['timestamp_column'])
            entry['parquet_metadata']=stats
            test('normalized_row_count',stats['rows']==m['normalized']['row_count'])
            time_stats=stats['statistics'][m['normalized']['timestamp_column']]
            symbol_stats=stats['statistics']['symbol']
            test('timestamp_statistics_complete',time_stats['complete_row_group_statistics'] and time_stats['null_count']==0)
            test('symbol_statistics_complete',symbol_stats['complete_row_group_statistics'] and symbol_stats['null_count']==0)
            test('all_rows_exact_symbol',symbol_stats['minimum']==symbol_stats['maximum']==symbol)
            if stats['rows']:
                test('normalized_manifest_time_range',utc(time_stats['minimum'])==utc(m['normalized']['earliest_timestamp']) and utc(time_stats['maximum'])==utc(m['normalized']['latest_timestamp']))
                test('normalized_times_inside_request',utc(time_stats['minimum'])>=utc(request['start']) and utc(time_stats['maximum'])<utc(request['end']))
            prices_valid=all(stats['statistics'][k]['complete_row_group_statistics'] and stats['statistics'][k]['null_count']==0 and stats['statistics'][k]['minimum']>0 for k in ('open','high','low','close'))
            entry['quality_checks']={'all_observed_ohlc_finite_positive':prices_valid}
            if not prices_valid:
                quality_frame=pd.read_parquet(payloads['normalized'],columns=[m['normalized']['timestamp_column'],'open','high','low','close','volume']).reset_index()
                bad=quality_frame[['open','high','low','close']].isna().any(axis=1)|quality_frame[['open','high','low','close']].le(0).any(axis=1)
                entry['quality_exclusions']={'invalid_ohlc_rows':int(bad.sum()),
                    'sample':json.loads(quality_frame.loc[bad].head(10).to_json(orient='records',date_format='iso')),
                    'policy':'Preserve native evidence; undefined/nonpositive observations are not valid target prices or features.'}
            size=m['raw']['size_bytes']
            if size<=args.raw_file_limit_bytes and raw_used+size<=args.raw_hash_budget_bytes:
                test('raw_payload_hash',sha(payloads['raw'])==m['raw']['checksum_sha256'])
                raw_used += size
                entry['raw_hash_status']='VERIFIED' if entry['checks']['raw_payload_hash'] else 'FAILED'
            else:
                entry['raw_hash_status']='DEFERRED_IO_BUDGET'
            store=db.DBNStore.from_file(payloads['raw'])
            try:
                meta=store.metadata
                entry['native_metadata']={'dataset':str(meta.dataset),'schema':str(meta.schema),'symbols':list(meta.symbols),
                    'start_ns':meta.start,'end_ns':meta.end,'partial':list(meta.partial),'not_found':list(meta.not_found)}
                test('native_request_metadata',str(meta.dataset)=='XNAS.ITCH' and str(meta.schema)==schema and list(meta.symbols)==[symbol] and meta.start==utc(request['start']).value and meta.end==utc(request['end']).value)
                test('native_not_found_empty',not meta.not_found)
                # Partial native symbology may reflect listing/ticker periods;
                # retain it explicitly, never infer observation availability.
                if meta.partial:
                    entry['coverage_note']='NATIVE_PARTIAL_SYMBOLOGY: requested range is not evidence of full observed symbol history'
            finally:
                store.reader.close()
            entry['raw_record_decode_status']='NOT_PERFORMED_FULL_RECORD_NORMALIZATION_REPLAY'
            entry['provider_warnings']=m.get('provider_warnings',[])
        except Exception as exc:
            entry['issues'].append(type(exc).__name__+': '+str(exc))
        entry['status']='VERIFIED_METADATA_AND_PAYLOAD_BINDINGS' if not entry['issues'] else 'ISSUES_FOUND'
        if entry['issues']:
            findings.append({'manifest_path':str(path),'issues':entry['issues']})
        partitions.append(entry)
    print(json.dumps({'phase':'PARTITIONS_VERIFIED','partitions':len(partitions),'issues':len(findings),'raw_bytes_hashed':raw_used}),flush=True)
    comparisons=[]
    if not args.skip_second_comparison:
        for symbol in sorted({p['symbol'] for p in partitions if p['schema']=='ohlcv-1s'}):
            seconds=[p for p in partitions if p['symbol']==symbol and p['schema']=='ohlcv-1s']
            minutes=[p for p in partitions if p['symbol']==symbol and p['schema']=='ohlcv-1m']
            if any(p['issues'] for p in seconds+minutes):
                comparisons.append({'symbol':symbol,'status':'BLOCKED_BY_PARTITION_VERIFICATION'})
                continue
            try:
                aggregates=pd.concat([second_minutes(Path(p['normalized_path'])) for p in seconds],ignore_index=True).sort_values('minute')
                if aggregates.minute.duplicated().any():
                    raise ValueError('Overlapping seconds require an explicit duplicate authority review')
                second_rows_total=int(aggregates.observed_seconds.sum())
                invalid_second_rows=int(aggregates.invalid_seconds.sum())
                excluded_minute_count=int(aggregates.invalid_seconds.gt(0).sum())
                aggregates=aggregates.loc[aggregates.invalid_seconds.eq(0)].copy()
                fields=['ts_event','open','high','low','close','volume']
                frames=[pd.read_parquet(p['normalized_path'],columns=fields).reset_index() for p in minutes]
                native=pd.concat(frames,ignore_index=True)[fields]
                native=native.drop_duplicates(fields)
                conflicts=native.ts_event.duplicated(keep=False)
                if conflicts.any():
                    raise ValueError('Native minute partitions contain conflicting observations')
                joined=aggregates.merge(native,left_on='minute',right_on='ts_event',how='left',suffixes=('_seconds','_minute'),indicator=True)
                common=joined.loc[joined._merge.eq('both')]
                matches={name:int(common[name+'_seconds'].eq(common[name+'_minute']).sum()) for name in ('open','high','low','close','volume')}
                exact=common[[k+'_seconds' for k in ('open','high','low','close','volume')]].to_numpy()==common[[k+'_minute' for k in ('open','high','low','close','volume')]].to_numpy()
                added=joined.loc[joined._merge.eq('left_only')]
                record={'symbol':symbol,'status':'COMPARISON_COMPLETE','observed_second_rows':int(aggregates.observed_seconds.sum()),
                    'native_second_rows_total':second_rows_total,'invalid_second_rows':invalid_second_rows,
                    'minute_buckets_excluded_for_any_invalid_second':excluded_minute_count,
                    'observed_minute_buckets':len(aggregates),'existing_native_minute_overlap':len(common),
                    'minute_buckets_absent_from_native_1m':len(added),'exact_ohlcv_match_in_overlap':int(exact.all(axis=1).sum()),
                    'field_matches_in_overlap':matches,'first_observed_second':str(aggregates.first_observed_second.min()),
                    'overlap_disagreement_sample':json.loads(common.loc[~exact.all(axis=1),['minute','actual_open_observed_at','actual_close_observed_at','open_seconds','open_minute','high_seconds','high_minute','low_seconds','low_minute','close_seconds','close_minute','volume_seconds','volume_minute']].head(12).to_json(orient='records',date_format='iso')),
                    'last_observed_second':str(aggregates.last_observed_second.max()),
                    'first_second_at_minute_boundary':int(aggregates.first_observed_second.eq(aggregates.minute).sum()),
                    'last_second_completes_at_minute_boundary':int((aggregates.last_observed_second+pd.Timedelta(seconds=1)).eq(aggregates.minute+pd.Timedelta(minutes=1)).sum()),
                    'minute_buckets_with_all_60_observed_seconds':int(aggregates.observed_seconds.eq(60).sum()),
                    'additional_minute_sample':json.loads(added[['minute','actual_open_observed_at','actual_close_observed_at','open_seconds','close_seconds','volume_seconds']].head(10).to_json(orient='records',date_format='iso')),
                    'interpretation':'Comparison only: observed seconds retain actual open clock and one-second completion clock. Bucket labels do not authorize fabricated minute boundary timestamps, omitted trade assumptions, replacement of native minute prices, or new fitted-label qualification.'}
                comparisons.append(record)
                print(json.dumps({'phase':'SECOND_COMPARISON','symbol':symbol,'buckets':len(aggregates),'additional':len(added),'exact_overlap':record['exact_ohlcv_match_in_overlap']}),flush=True)
                del aggregates,frames,native,joined,common,added
                gc.collect()
            except Exception as exc:
                comparisons.append({'symbol':symbol,'status':'COMPARISON_FAILED','error':type(exc).__name__+': '+str(exc)})
                findings.append({'second_comparison_symbol':symbol,'error':str(exc)})
    result={'audited_at':datetime.now(timezone.utc).isoformat(),'status':'ISSUES_FOUND' if findings else 'VERIFIED_INVENTORY_WITH_EXPLICIT_SCOPE_LIMITS',
        'root':str(ROOT),'current_watchlist':watchlist,'discovered_symbols':sorted({p['symbol'] for p in partitions}),
        'symbols_outside_watchlist':sorted({p['symbol'] for p in partitions}-set(watchlist)),
        'schema_inventory':dict(preliminary),'io_preflight':preflight,'partition_count':len(partitions),
        'normalized_hashes_verified':sum(p['checks'].get('normalized_payload_hash',False) for p in partitions),
        'raw_hashes_verified':sum(p.get('raw_hash_status')=='VERIFIED' for p in partitions),
        'raw_hashes_deferred':sum(p.get('raw_hash_status')=='DEFERRED_IO_BUDGET' for p in partitions),
        'raw_bytes_hashed':raw_used,'raw_full_decode_replays':0,'issues':findings,'partitions':partitions,
        'quality_exclusions':[{'symbol':p['symbol'],'schema':p['schema'],'manifest_path':p['manifest_path'],**p['quality_exclusions']} for p in partitions if 'quality_exclusions' in p],
        'second_endpoint_comparisons':comparisons,
        'limits':['Requested ranges are acquisition coverage, not proof of observed bars at every clock.',
            'Daily/hourly rows support separate causal historical features; they cannot reconstruct missing minute/second endpoints.',
            'All native time offsets, observed prices and volume are preserved. No synthetic data or price replacement produced.',
            'Existing immutable publications keep their v1 source identity; this inventory is not activation or fitting qualification.',
            'Raw SHA256 and native header metadata were checked; complete native record-to-Parquet replay was not performed.'],
        'scope':'Four explicit XNAS.ITCH OHLCV schema directories, every manifest-backed symbol/window. Audit evidence writes only; no production writes, provider/broker requests, model fits or symbol activation.'}
    (OUT/'xnas-history-inventory.json').write_text(json.dumps(result,indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8')
    md=['# All-symbol XNAS.ITCH historical inventory','',f"Audited {result['audited_at']}. **{result['status']}**.",'',
        f"Verified **{result['normalized_hashes_verified']}/{len(partitions)} normalized hashes** and **{result['raw_hashes_verified']}/{len(partitions)} raw hashes**, with **{result['raw_hashes_deferred']} raw files deferred**. Manifest/receipt bindings, native request headers, Parquet row counts and observed ranges were checked. Full native record normalization replay was not performed.",'',
        '| Schema | Symbols | Windows | Rows including overlapping windows | Raw bytes | Normalized bytes |','|---|---:|---:|---:|---:|---:|']
    for schema,s in preliminary.items():
        md.append(f"| {schema} | {len(s['symbols'])} | {s['partitions']} | {s['rows']:,} | {s['raw_bytes']:,} | {s['normalized_bytes']:,} |")
    md+=['','Only the existing eleven production symbols were found. No symbols were selected or activated. Requested history ranges and duplicate-window row totals must not be mistaken for distinct training examples.','','## Second-bar endpoint evidence','','| Symbol | First observed second | Last observed second | Observed minute buckets | Absent in native minute archive | Exact OHLCV matches / overlap |','|---|---|---|---:|---:|---:|']
    for c in comparisons:
        if c['status']=='COMPARISON_COMPLETE':
            md.append(f"| {c['symbol']} | {c['first_observed_second']} | {c['last_observed_second']} | {c['observed_minute_buckets']:,} | {c['minute_buckets_absent_from_native_1m']:,} | {c['exact_ohlcv_match_in_overlap']:,} / {c['existing_native_minute_overlap']:,} |")
        else:
            md.append(f"| {c['symbol']} | {c['status']} | | | | |")
    md+=['','Minute buckets above are audit comparisons, not new price artifacts. The first observed second supplies its genuine open time; the last observed second supplies a close available one second later. Never promote these times to a minute boundary or assume absent seconds had no trades. Native minute observations remain their own source evidence.','','The seven original symbols have only August 5–14 second bars; CROX/IONQ/PATH/TWST have longer second history ending September 11. Therefore saved second history cannot fill September 22 actuals. It can support prospective historical feature/label admission where exact source, timestamp, target and chronological checks succeed.','','Daily and hourly archives can contribute causal features at their actual availability times. They cannot supply missing intraday target endpoints. No training, model assessment, performance claim, production mutation or provider request was made.','',f"Issues: **{len(findings)}**. Full per-window checks and raw verification scope: [JSON](C:/dev/ducketz/artifacts/analysis/historical-training-20260923/xnas-history-inventory.json).",'']
    md+=['## Native quality exclusions','']
    for q in result['quality_exclusions']:
        md.append(f"- {q['symbol']} {q['schema']}: {q['invalid_ohlc_rows']} undefined/nonpositive OHLC observations; exact timestamps retained in JSON. These rows must be excluded from eligible inputs, without changing source evidence.")
    md+=['','The seconds comparison conservatively excludes any minute bucket containing an invalid second. Overlap discrepancies are reported, not repaired or used to overwrite native minute data.','']
    (OUT/'xnas-history-inventory.md').write_text('\n'.join(md),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('audited_at','status','partition_count','normalized_hashes_verified','raw_hashes_verified','raw_hashes_deferred','raw_bytes_hashed','issues')},indent=2),flush=True)
    return 1 if findings else 0

if __name__=='__main__':
    raise SystemExit(main())
