"""Bounded independent final planning/actuals verification; no broker calls."""
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
from collections import Counter
import hashlib
import json
import math
import statistics
import subprocess
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,'C:/dev/ducketz')
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'preflight'))
import pandas as pd
from pandas.testing import assert_frame_equal
from verify_native_reconciliation import tables
from ml.stock_trader.contracts import canonical_sha256

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
PRE=OUT.parent/'preflight'
RUN=ROOT/'ml/overnight-runs/20260925T040822.112032Z'
PLAN=ROOT/'ml/gameplan-trade-plan-runs/20260925T061845.719846Z'
ACTUALS=ROOT/'ml/gameplan-actuals-review-runs/20260925T062206.766961Z'
GAME=ROOT/'ml/nightly-gameplan-runs/20260925T060654.631426Z'


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):
    if path.stat().st_size>16*1024*1024:raise RuntimeError('BOUNDED_OUTPUT_HASH_EXCEEDED')
    return hashlib.sha256(path.read_bytes()).hexdigest()
def dec(value):return Decimal(str(value))
def close(a,b,tolerance='0.011'):return abs(dec(a)-dec(b))<=dec(tolerance)


def main():
    issues=[];checks=0
    def check(condition,message):
        nonlocal checks
        checks+=1
        if not condition:issues.append(message)
    def same_frame(left,right,key,label):
        try:assert_frame_equal(left.sort_values(key).reset_index(drop=True),right.sort_values(key).reset_index(drop=True),check_dtype=False)
        except AssertionError:check(False,label)
        else:check(True,label)
    report,receipt=read(RUN/'stage-report.json'),read(RUN/'receipt.json')
    check(report['status']==receipt['status']=='COMPLETE' and len(report['stages'])==8,'Native eight-stage completion')
    check(report['orders_placed']==0 and report['broker_orders_enabled'] is False,'Native zero-order authority')
    check(all(stage['status']=='COMPLETE' and stage['exit_code']==0 for stage in report['stages']),'Every stage success')
    bindings={}
    for folder in [PLAN,ACTUALS]:
        rec,man=read(folder/'receipt.json'),read(folder/'manifest.json')
        check(rec['status']=='COMPLETE' and rec['orders_placed']==0 and rec['broker_orders_enabled'] is False,'Tail completion/zero orders '+folder.name)
        check(sha(folder/'manifest.json')==rec['manifest_sha256'],'Tail receipt manifest '+folder.name)
        for name,binding in man['output_files'].items():
            path=folder/name
            check(path.stat().st_size==binding['size'] and sha(path)==binding['checksum_sha256'],'Bounded output binding '+name)
        bindings[str(folder)]={'manifest_sha256':sha(folder/'manifest.json'),'receipt_sha256':sha(folder/'receipt.json'),'outputs_checked':len(man['output_files'])}
    pr=read(PLAN/'report.json');snap=read(PLAN/'account-snapshot.json');ledger=read(PLAN/'direction-ledger.json')
    path=read(PLAN/'planning-price-path.json');reference=read(PLAN/'planning-reference-completion.json')
    forecasts=pd.read_parquet(GAME/'forecasts.parquet');aug=pd.read_parquet(PLAN/'trade-plan.parquet')
    symbols=read(OUT.parent/'startup.json')['configured_symbols']
    check(len(aug)==264 and aug['forecast_id'].nunique()==264 and aug.groupby('symbol').size().to_dict()=={s:24 for s in symbols},'264 augmented rows exact universe')
    same_frame(forecasts,aug[forecasts.columns],'id','Frozen successor forecasts unchanged')
    check(pr['source_gameplan_run']==GAME.relative_to(ROOT).as_posix() and pr['source_receipt_sha256']==sha(GAME/'receipt.json'),'Pinned source identity')
    check(pr['direction_policy_version']=='stock-direction-50-v2' and pr['direction_up_threshold']==pr['direction_down_threshold']==0.5,'Saved 50 percent direction policy')
    check(ledger['holding_policy']=='accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1','Saved signal-driven holding policy')
    check(path['contract_version']=='conditional-hourly-planning-price-path-v3' and reference['max_gap_minutes']==240,'Saved sparse reference contract')
    check(reference['native_prices_modified'] is False and reference['model_training_prices_modified'] is False and reference['regular_session_prices_filled'] is False,'Synthetic scope declarations')
    check(pr['direction_projection_status']==ledger['status']=='COMPLETE','Complete projection')
    check(snap['orders_placed']==0 and snap['orders_enabled'] is False and snap['broker_data_http_methods']==['GET'],'Read-only cash snapshot')
    check(snap['working_order_count']==0 and snap['reserved_cash']==0 and not any(snap['pending_buy_shares'].values()) and not any(snap['pending_sell_shares'].values()),'No current pending reservations')
    check(snap['ownership']['safe_for_planning'] is True,'Snapshot safe ownership')
    check(pd.Timestamp(pr['observed_at'])<pd.Timestamp(snap['observed_at'])<pd.Timestamp(pr['completed_at']),'Fresh snapshot taken during planning')
    check(snap['available_cash']==snap['balances']['cash_balance']==ledger['summary']['starting_cash']==47346.43,'Literal available cash retained')
    check(ledger['no_fill_baseline']['cash']==snap['available_cash'] and ledger['no_fill_baseline']['held_shares']==snap['held_shares'],'No-fill baseline conserved')
    check(set(path['points'])=={f'{s}|2026-09-25|{hour:02}:00' for s in symbols for hour in range(4,18)},'154 exact hourly price points')
    for key,point in path['points'].items():
        check(point['status']=='AVAILABLE' and point['sample_count']==len(point['samples']) and point['sample_count']>=2,'Price point availability '+key)
        median=statistics.median(sample['ratio'] for sample in point['samples'])
        center=point['reference_price']*median
        check(close(median,point['ratio_median'],'0.000000000001'),'Recorded median '+key)
        check(close(center,point['planned_price_mid']) and close(math.floor(center*.998*100)/100,point['planned_price_low']) and close(math.ceil(center*1.002*100)/100,point['planned_price_high']),'Conditional price arithmetic '+key)
        ref=reference['references'][point['symbol']+'|2026-09-25']
        check(point['reference_observed_at']==ref['observed_at'] and point['reference_price']==ref['price'] and point['reference_is_synthetic']==ref['is_synthetic'],'Original anchor timestamps '+key)
        check(all(sample['session']<'2026-09-25' for sample in point['samples']),'Causal historical price samples '+key)
    synthetic=pd.read_parquet(PLAN/'synthetic-reference-bars.parquet')
    anchors=[]
    for ref in reference['references'].values():
        rows=synthetic.loc[synthetic.symbol.eq(ref['symbol'])]
        if not ref['is_synthetic']:
            check(rows.empty,'No synthetic bars for observed anchor '+ref['symbol']);continue
        anchors.append({key:ref[key] for key in ['symbol','price','observed_at','effective_at','gap_minutes','fill_count','reason']})
        check(len(rows)==ref['fill_count']==ref['gap_minutes']<=240,'Synthetic count/bound '+ref['symbol'])
        check((pd.Timestamp(ref['effective_at'])-pd.Timestamp(ref['observed_at'])).total_seconds()/60==ref['gap_minutes'],'Original synthetic gap '+ref['symbol'])
        check(pd.Timestamp(ref['observed_at'])>=pd.Timestamp(ref['regular_session_close_at']),'No regular-session fill '+ref['symbol'])
        check(all(rows[column].eq(ref['price']).all() for column in ['open','high','low','close']) and rows.volume.eq(0).all() and rows.is_synthetic.eq(True).all(),'Constant disclosed synthetic OHLCV '+ref['symbol'])
        check(rows.original_observed_at.eq(ref['observed_at']).all() and rows.reason.eq('ASSUMED_NO_TRADES').all(),'Synthetic observation identity '+ref['symbol'])
        observed=pd.Timestamp(ref['observed_at']);effective=pd.Timestamp(ref['effective_at'])
        check(list(pd.to_datetime(rows.timestamp,utc=True).sort_values())==list(pd.date_range(observed,effective-pd.Timedelta(minutes=1),freq='min')),'Synthetic minute coverage '+ref['symbol'])
        coverage=ref['source_coverage']
        check(coverage['native_partition_verified'] and pd.Timestamp(coverage['start'])<=observed and pd.Timestamp(coverage['end'])>=effective and pd.Timestamp(coverage['published_at'])<=pd.Timestamp(path['observed_at']),'Synthetic acquisition coverage '+ref['symbol'])
    check(len(synthetic)==sum(item['fill_count'] for item in anchors)==70,'Total disclosed synthetic rows')
    cash={name:dec(snap['available_cash']) for name in ['low','base','high']};held={s:dec(value) for s,value in snap['held_shares'].items()}
    by_forecast={};event_index=0
    for hour in ledger['hourly']:
        hour_events=[event for event in ledger['events'] if event['timestamp']==hour['timestamp']]
        check(hour['event_sequences']==[event['sequence'] for event in hour_events],'Hourly event membership')
        seen_buy=False
        for event in hour_events:
            event_index+=1
            check(event['sequence']==event_index,'Contiguous event sequence')
            buy=event['action']=='BUY';quantity=dec(event['quantity']);symbol=event['symbol']
            check(event['reason'] in ['BULLISH_BUY','BEARISH_SELL'],'No scheduled expiry sale')
            check(not (seen_buy and not buy),'Bearish sales precede buys')
            seen_buy=seen_buy or buy
            check(held[symbol]==dec(event['shares_before']),'Before-event shares')
            for name in ['low','base','high']:
                check(cash[name]==dec(event['cash_before_'+name]),'Before-event shared cash')
                price_name=({'low':'high','base':'base','high':'low'} if buy else {'low':'low','base':'base','high':'high'})[name]
                delta=quantity*dec(event['price_'+price_name])*(-1 if buy else 1)
                check(close(delta,event['cash_change_'+name]),'Event fill-price arithmetic')
                cash[name]+=dec(event['cash_change_'+name])
                check(close(cash[name],event['cash_'+name]),'Event cash conservation')
            held[symbol]+=quantity*(1 if buy else -1)
            check(held[symbol]>=0 and held[symbol]==dec(event['shares_after']),'Event shares conserved/no oversell')
            check(cash['low']<=cash['base']<=cash['high'] and cash['low']>=dec(ledger['summary']['cash_buffer'])-dec('.011'),'Cash ranges and initial buffer')
            by_forecast[event['forecast_id']]=event
        check(all(close(cash[name],hour['cash_'+name]) for name in cash),'Entire-hour cash conservation')
        check(held=={s:dec(v) for s,v in hour['held_shares'].items()},'Entire-hour share conservation')
        local=pd.Timestamp(hour['timestamp']).tz_convert('America/Los_Angeles').strftime('%H:%M')
        for row in aug.loc[aug.action_anchor_local.eq(local)].to_dict('records'):
            check(all(close(row['projected_cash_after_'+name],hour['cash_'+name]) for name in cash),'Same-clock row post-hour cash')
            check(close(row['projected_shares_after'],hour['held_shares'][row['symbol']]),'Same-clock row shares')
    check(held=={s:dec(v) for s,v in ledger['ending_positions'].items()},'Ending holdings conservation')
    check(all(close(cash[name],ledger['summary']['ending_cash_'+name]) for name in cash),'Ending cash conservation')
    check(len(ledger['events'])==70 and ledger['summary']['buy_events']==47 and ledger['summary']['sell_events']==23,'Direction event count')
    for row in aug.to_dict('records'):
        if not row['execution_eligible']:
            check(pd.isna(row['direction_based_trade_quantity']),'Non-entry quantity omitted');continue
        event=by_forecast.get(row['forecast_id'])
        expected=event['quantity']*(1 if event['action']=='BUY' else -1) if event else 0
        check(row['direction_based_trade_quantity']==expected,'Forecast signed quantity matches event')
    ar=read(ACTUALS/'report.json');actual=pd.read_parquet(ACTUALS/'forecast-results.parquet');prices=pd.read_parquet(ACTUALS/'price-results.parquet')
    prior_plan=Path(ar['source_trade_plan_path']);prior_game=Path(ar['source_gameplan_path'])
    same_frame(pd.read_parquet(prior_plan/'trade-plan.parquet'),actual[pd.read_parquet(prior_plan/'trade-plan.parquet').columns],'id','Prior frozen planning estimates preserved')
    check(pd.Timestamp(read(prior_plan/'receipt.json')['completed_at'])<pd.Timestamp('2026-09-24T11:00:00Z'),'Prior estimate saved before opening')
    check(ar['action_date']=='2026-09-24' and ar['successor_action_date']=='2026-09-25' and ar['successor_gameplan_run']==GAME.relative_to(ROOT).as_posix(),'Actuals prior/successor dates')
    prior_points=read(prior_plan/'planning-price-path.json')['points']
    for row in prices.to_dict('records'):
        point=prior_points[f"{row['symbol']}|2026-09-24|{row['clock_local']}"]
        check(all(row['planned_price_'+name]==point['planned_price_'+name] for name in ['low','mid','high']),'Frozen same-clock price estimate')
        if row['comparison_status']=='COMPARED':
            check(row['actual_gap_seconds']<=300 and row['actual_status']=='OBSERVED','Actual price five-minute boundary')
            delta=row['actual_price']-row['planned_price_mid']
            check(close(delta,row['price_error']) and close(delta/row['planned_price_mid'],row['price_error_fraction'],'0.000000001'),'Actual price difference arithmetic')
            check(row['in_planned_range']==(row['planned_price_low']<=row['actual_price']<=row['planned_price_high']),'Actual price in-range result')
    for row in actual.to_dict('records'):
        if row['actuals_status']=='EVALUATED':
            check(row['actual_start_gap_seconds']<=300 and row['actual_end_gap_seconds']<=300,'Forecast actual boundary tolerance')
            move=row['actual_end_price']/row['actual_start_price']-1
            check(close(move,row['actual_return'],'0.000000001'),'Forecast raw price return')
            correct=move>0 if row['direction']=='BULLISH' else move<0 if row['direction']=='BEARISH' else None
            check(correct is None or row['direction_correct']==correct,'Raw directional outcome')
        else:check(pd.isna(row['direction_correct']),'Pending/missing excluded from direction accuracy')
    dated=ROOT/'ml/gameplan-actuals-review-by-date/2026-09-24/Gameplan-results.md'
    check(sha(dated)==sha(ACTUALS/'Gameplan-results.md'),'Dated actuals review content link')
    text=(PLAN/'Gameplan.md').read_text()
    check(str(dated).replace('\\','/') in text,'Successor readable prior-results link')
    check('Projected Trade Quantity | Direction Based Trade Qty' in text,'Adjacent quantity columns')
    for pointer,folder in [('gameplan-trade-plan-latest',PLAN),('gameplan-actuals-review-latest',ACTUALS),('nightly-gameplan-latest',GAME)]:
        check(folder.relative_to(ROOT).as_posix() in json.dumps(read(ROOT/'ml'/pointer/'run.json')),'Current pointer '+pointer)
    pin=read(PRE/'reconciliation-plan.json');recon=read(PRE/'native-reconciliation.json')
    check(all(sha(Path(name))==digest for name,digest in pin['guarded_artifact_sha256'].items()),'Controls/session/claims unchanged')
    state,_=tables(ROOT/'state/independent-stock-trader/holdings.sqlite3')
    check(canonical_sha256(state)==recon['after_logical_sha256'],'Ownership ledger unchanged since verified reconciliation')
    check(snap['held_shares']==recon['portfolio']['held_shares'],'Fresh planning holdings agree with reconciled broker baseline')
    command="Get-ScheduledTask -TaskName 'Ducketz Independent Stock Session' | ForEach-Object { [pscustomobject]@{name=$_.TaskName;state=[string]$_.State;enabled=$_.Settings.Enabled;actions=@($_.Actions | Select-Object Execute,Arguments,WorkingDirectory);triggers=@($_.Triggers | Select-Object Enabled,StartBoundary,DaysOfWeek,WeeksInterval)} } | ConvertTo-Json -Depth 8"
    current_schedule=json.loads(subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,text=True,check=True).stdout)
    check(current_schedule==read(OUT.parent/'windows-schedules.json'),'Saved Windows schedule unchanged')
    result={'verified_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if not issues else 'ISSUES_FOUND','checks':checks,'issues':issues,
        'scope':'Bounded final planning/actuals output checks and guarded controls/ownership/Windows schedule. No heavy source archive hashes, broker/provider calls or production writes. App automation schedule comparison remains with root because this subtask has no pre-run app automation snapshot.',
        'bindings':bindings,'source_gameplan':str(GAME),'trade_plan':str(PLAN),'actuals':str(ACTUALS),
        'policies':{'direction':pr['direction_policy_version'],'holding':ledger['holding_policy'],'price_path':path['contract_version'],'reference_completion':reference['contract_version'],'maximum_reference_gap_minutes':reference['max_gap_minutes']},
        'forecast_rows':len(aug),'price_points':len(path['points']),'synthetic_rows':len(synthetic),'synthetic_anchors':anchors,
        'cash_snapshot_at':snap['observed_at'],'starting_cash':snap['available_cash'],'cash_summary':ledger['summary'],
        'actual_forecast_counts':actual['actuals_status'].value_counts().to_dict(),'actual_price_counts':prices['comparison_status'].value_counts().to_dict(),
        'actual_direction_counts':actual['direction_result'].value_counts().to_dict(),
        'missing_forecast_symbols':actual.loc[actual.actuals_status.eq('MATURE_AWAITING_DATA'),'symbol'].value_counts().to_dict(),
        'missing_price_symbols':prices.loc[prices.comparison_status.eq('MATURE_AWAITING_DATA'),'symbol'].value_counts().to_dict(),
        'zero_overnight_orders':True,'windows_schedule':current_schedule}
    (OUT/'independent-output-review.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:result[key] for key in ['status','checks','issues','forecast_rows','price_points','synthetic_anchors','cash_summary','actual_forecast_counts','actual_price_counts','actual_direction_counts']},indent=2))


if __name__=='__main__':main()
