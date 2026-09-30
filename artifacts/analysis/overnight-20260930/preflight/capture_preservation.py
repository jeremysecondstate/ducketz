"""Capture local preservation baseline without broker calls or production writes."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import argparse
import hashlib
import json
import sqlite3
import subprocess

OUT = Path(__file__).resolve().parent
ROOT = Path('C:/DATASTORE')
REPO = Path('C:/dev/ducketz')

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def table_state():
    db=ROOT/'state/independent-stock-trader/holdings.sqlite3'
    conn=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=3)
    conn.row_factory=sqlite3.Row
    result={}
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            rows=[dict(r) for r in conn.execute('SELECT * FROM "'+name+'" ORDER BY rowid')]
            result[name]={'rows':len(rows),'sha256':digest(rows)}
            if name in ['allocations','fills','reservations','inventory_assignments','blocks','metadata']:
                result[name]['records']=rows
            if name=='snapshots':
                result[name]['latest']=max(rows,key=lambda r:r['observed_at']) if rows else None
    finally:
        conn.close()
    return result

def tasks():
    command=r"""$items = @(Get-ScheduledTask | Where-Object { $_.TaskName -match 'Ducketz|Gameplan|Loops|OPRA|Directional' } | ForEach-Object { [ordered]@{name=$_.TaskName;state=[string]$_.State;task_path=$_.TaskPath;actions=@($_.Actions|Select-Object Execute,Arguments,WorkingDirectory);triggers=@($_.Triggers|Select-Object Enabled,StartBoundary,DaysOfWeek,WeeksInterval);settings=$_.Settings|Select-Object Enabled,StartWhenAvailable,WakeToRun,RestartCount,RestartInterval,MultipleInstances} }); ConvertTo-Json -InputObject $items -Depth 8"""
    return json.loads(subprocess.run(['powershell.exe','-NoProfile','-Command',command],check=True,capture_output=True,text=True).stdout)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label',default='initial-baseline')
    parser.add_argument('--compare')
    args=parser.parse_args()
    if any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in args.label):
        raise ValueError('Invalid local evidence label')
    protected=read(OUT/'controls-and-schedules.json')
    local=read(OUT/'local-preflight.json')
    ledger=table_state()
    controls={path:{'exists':Path(path).is_file(),'sha256':sha(path) if Path(path).is_file() else None}
              for path in protected['controls_and_launcher_hashes']}
    automations={item['path']:sha(item['path']) for item in protected['automations']}
    claims={str(p):sha(p) for name in ['entry-slots','quote-recovery-slots']
            for p in sorted((ROOT/'state/independent-stock-trader'/name).rglob('*')) if p.is_file()}
    status_path=ROOT/'state/independent-stock-trader/session-status.json'
    status=read(status_path)
    source_files={path:sha(path) for path in local['source_sha256'] if Path(path).is_file()}
    git=subprocess.run(['git','status','--short','--untracked-files=all'],cwd=REPO,text=True,capture_output=True,check=True).stdout
    concurrent={}
    for line in git.splitlines():
        rel=line[3:]
        if rel.startswith('artifacts/analysis/overnight-20260930/'):
            continue
        p=REPO/rel
        concurrent[rel]={'git_status':line[:2],'sha256':sha(p) if p.is_file() else None}
    result={'captured_at':datetime.now(timezone.utc).isoformat(),
       'scope':'READ_ONLY_NO_BROKER_PROVIDER_LOCK_OR_PRODUCTION_WRITE',
       'ledger_tables':ledger,'ledger_table_counts':{k:v['rows'] for k,v in ledger.items()},
       'ledger_table_sha256':{k:v['sha256'] for k,v in ledger.items()},
       'reservation_counts':dict(Counter(r['status'] for r in ledger['reservations']['records'])),
       'control_hashes':controls,'automation_hashes':automations,'claim_hashes':claims,
       'windows_task_configuration':tasks(),'watchlist_sha256':sha(REPO/'datafetching/watchlist.txt'),
       'terminal_session_sha256':sha(status_path),'terminal_session_status':status['status'],
       'source_hashes':source_files,'concurrent_changes':concurrent,
       'last_stock_decision_run':status['last_cycle']['run_directory'],
       'broker_calls':0,'provider_calls':0,'order_actions':0,'production_writes':0}
    if args.compare:
        baseline=read(OUT/(args.compare+'.json'))
        keys=['control_hashes','automation_hashes','claim_hashes','windows_task_configuration',
              'watchlist_sha256','terminal_session_sha256','source_hashes','last_stock_decision_run',
              'ledger_table_counts','ledger_table_sha256']
        result['checks']={key:result[key]==baseline[key] for key in keys}
        result['concurrent_change_deltas']={p:{'baseline':baseline['concurrent_changes'].get(p),'current':v}
            for p,v in concurrent.items() if baseline['concurrent_changes'].get(p)!=v}
        result['status']='PASS' if all(result['checks'].values()) else 'REVIEW_REQUIRED'
    else:
        result['status']='BASELINE_CAPTURED'
    output=OUT/(args.label+'.json')
    output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'ledger_counts':result['ledger_table_counts'],
          'reservation_counts':result['reservation_counts'],'output':str(output),
          'checks':result.get('checks')}))

if __name__=='__main__':
    main()
