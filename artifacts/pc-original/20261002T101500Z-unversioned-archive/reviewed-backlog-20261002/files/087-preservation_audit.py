"""Sanitized local-only baseline and comparison for this native overnight run.

Hashes and schedule metadata only: no account/order identifiers, raw ledger
records, credentials, automation prompts, broker calls or production writes.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True
REPO = Path('<LOCAL_CHECKOUT>')
ROOT = Path('<LOCAL_DATASTORE>')
OUT = Path(__file__).resolve().parent
DB = ROOT/'state/independent-stock-trader/holdings.sqlite3'
STATUS = ROOT/'state/independent-stock-trader/session-status.json'
SOURCE_DIRS = [ROOT/'ml/nightly-gameplan-runs/20260930T061501.586401Z',
               ROOT/'ml/gameplan-trade-plan-runs/20260930T062633.629638Z']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def files(paths):
    return {str(p):{'exists':p.is_file(), 'sha256':sha(p) if p.is_file() else None,
                    'size':p.stat().st_size if p.is_file() else None} for p in sorted(set(paths))}


def git(*args):
    env=dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    return subprocess.run(['git',*args],cwd=REPO,env=env,check=True,capture_output=True,text=True).stdout


def source_files():
    paths=set(git('ls-files','-z').split('\0'))|set(git('ls-files','--others','--exclude-standard','-z').split('\0'))
    roots={'app','ml','datafetching','tests','docs'}
    suffixes={'.py','.md','.json','.toml','.yaml','.yml','.ps1','.cmd','.ini','.cfg','.txt'}
    return files(REPO/rel for rel in paths if rel and Path(rel).suffix.lower() in suffixes
                 and (Path(rel).parts[0] in roots or len(Path(rel).parts)==1))


def git_identity():
    raw=git('rev-parse','--git-path','index').strip()
    index=Path(raw) if Path(raw).is_absolute() else REPO/raw
    return {'head':git('rev-parse','HEAD').strip(),
            'branch':git('rev-parse','--abbrev-ref','HEAD').strip(),
            'index':files([index.resolve()])}


def ledger():
    if Path(str(DB)+'-wal').exists() and not Path(str(DB)+'-shm').exists():
        raise RuntimeError('READ_ONLY_WAL_STATE_UNAVAILABLE')
    before=files([DB,Path(str(DB)+'-wal'),Path(str(DB)+'-shm')])
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=3) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        names=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables={name:[dict(row) for row in conn.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in names}
        result={'physical_files':before,
                'table_counts':{name:len(rows) for name,rows in tables.items()},
                'table_sha256':{name:digest(rows) for name,rows in tables.items()},
                'logical_sha256':digest(tables),
                'reservation_status_counts':dict(Counter(row['status'] for row in tables['reservations'])),
                'pending_reservation_count':sum(row['status'] in {'RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'} for row in tables['reservations']),
                'persistent_block_count':len(tables['blocks'])}
    result['physical_files_unchanged_during_read']=before==files([DB,Path(str(DB)+'-wal'),Path(str(DB)+'-shm')])
    return result


def windows_tasks():
    # No principal, password or full XML content is collected. Action arguments
    # are hashed before persistence in case a task contains private parameters.
    command=r"""$items = @(Get-ScheduledTask | Where-Object { $_.TaskName -match 'Ducketz|Gameplan|Loops|OPRA|Directional' } | Sort-Object TaskPath,TaskName | ForEach-Object {
      [ordered]@{ name=$_.TaskName; state=[string]$_.State; task_path=$_.TaskPath;
        actions=@($_.Actions | Select-Object Execute,Arguments,WorkingDirectory);
        triggers=@($_.Triggers | Select-Object Enabled,StartBoundary,EndBoundary,DaysOfWeek,WeeksInterval,DaysInterval);
        settings=$_.Settings | Select-Object Enabled,StartWhenAvailable,WakeToRun,RestartCount,RestartInterval,MultipleInstances,ExecutionTimeLimit }
    }); ConvertTo-Json -InputObject $items -Depth 8"""
    rows=json.loads(subprocess.run(['powershell.exe','-NoProfile','-Command',command],check=True,text=True,capture_output=True).stdout)
    for row in rows:
        row['action_configuration_sha256']=digest(row['actions'])
        for action in row['actions']:
            text=action.pop('Arguments') or ''
            action['arguments_sha256']=hashlib.sha256(text.encode()).hexdigest()
            action['recognized_flags']=[flag for flag in ['--execute','--wait-for-open','--run-session','--stock-only','--independent-stock-horizons','--archive-history'] if flag in text]
        # Dynamic task state is contextual, not an immutable schedule setting.
        row['observed_task_state']=row.pop('state')
    return rows


def automations():
    home=Path(os.environ.get('CODEX_HOME','<LOCAL_USER>/.codex'))
    result=[]
    for path in sorted((home/'automations').glob('*/automation.toml')):
        data=tomllib.loads(path.read_text(encoding='utf-8-sig'))
        if not any(term in data.get('name','').lower() for term in ['loop','gameplan','stock']):
            continue
        keys=['id','kind','name','status','rrule','model','reasoning_effort','reasoningEffort',
              'notification_policy','notificationPolicy','timezone','execution_environment','executionEnvironment']
        selected={key:data[key] for key in keys if key in data}
        result.append({'path':str(path),'sha256':sha(path),'metadata':selected,
                       'policy_prompt_sha256':hashlib.sha256(str(data.get('prompt','')).encode()).hexdigest()})
    return result


def immutable_sources():
    result={}
    for directory in SOURCE_DIRS:
        manifest,receipt=read(directory/'manifest.json'),read(directory/'receipt.json')
        entries=manifest['output_files']
        paths=[directory/'manifest.json',directory/'receipt.json',*(directory/name for name in entries)]
        recorded=files(paths)
        checks={name:recorded[str(directory/name)]['sha256']==entry['checksum_sha256']
                     and recorded[str(directory/name)]['size']==entry['size'] for name,entry in entries.items()}
        checks['manifest_receipt_binding']=sha(directory/'manifest.json')==receipt.get('manifest_sha256',receipt.get('manifest_checksum_sha256'))
        checks['source_action_date']=receipt['action_date']=='2026-09-30'
        result[str(directory)]={'action_date':receipt['action_date'],'files':recorded,'checks':checks}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare',action='store_true')
    parser.add_argument('--label')
    args=parser.parse_args()
    label=args.label or ('preservation-final' if args.compare else 'preservation-baseline')
    if not label or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in label):
        raise ValueError('Invalid artifact label')
    before_sources=source_files()
    before_git=git_identity()
    control_paths=[ROOT/'controls/stock-trader/operator-intent.txt',ROOT/'controls/gameplan-stock-trader/operator-intent.txt',
                   ROOT/'controls/loop-c/current/halt-control.json',ROOT/'controls/loop-c/current/risk-approval.json',
                   REPO/'.env',REPO/'Start-Gameplan-Trader.cmd',REPO/'docs/datafetch-ml/start_stock_session.ps1',
                   REPO/'datafetching/watchlist.txt']
    controls=files(control_paths)
    ledger_result=ledger()
    claims=files(p for name in ['entry-slots','quote-recovery-slots']
                 for p in (ROOT/'state/independent-stock-trader'/name).rglob('*') if p.is_file())
    session=read(STATUS)
    sources=immutable_sources()
    tasks=windows_tasks()
    schedules=automations()
    after_sources=source_files()
    after_git=git_identity()
    checks={'repository_sources_unchanged_during_capture':before_sources==after_sources,
            'git_head_branch_index_unchanged_during_capture':before_git==after_git,
            'ledger_physical_files_unchanged_during_capture':ledger_result['physical_files_unchanged_during_read'],
            'controls_unchanged_during_capture':controls==files(control_paths),
            'old_immutable_source_bindings_valid':all(all(item['checks'].values()) for item in sources.values())}
    result={'captured_at':datetime.now(timezone.utc).isoformat(),
            'scope':'LOCAL_READ_ONLY_HASHES_AND_SANITIZED_METADATA_NO_BROKER_PROVIDER_LOCK_OR_PRODUCTION_WRITE',
            'native_run':'<LOCAL_DATASTORE>/ml/overnight-runs/20261001T040644.667536Z',
            'controls_environment_launcher_and_watchlist':controls,
            'ledger':ledger_result,'entry_and_recovery_claims':claims,
            'trader_session_status_file':files([STATUS]),
            'trader_status':{key:session.get(key) for key in ['status','action_date','as_of']},
            'last_stock_decision_run':session.get('last_cycle',{}).get('run_directory'),
            'immutable_september30_sources':sources,'windows_launcher_configuration':tasks,
            'automation_schedule_model_policy_metadata':schedules,'git_identity':before_git,
            'repository_source_hashes':before_sources,
            'excluded_from_invariants':['Active overnight supervision claim and locks','Latest pointers',
                'New nightly provider data, model, publication and review outputs','Automation memory files',
                'Windows task dynamic observed state'],
            'repository_source_drift_policy':'Compare separately and attribute from evidence; unrelated concurrent edits are not automatically this task mutation.',
            'checks':checks,'broker_calls':0,'provider_calls':0,'order_actions':0,'production_writes':0}
    if args.compare:
        baseline_path=OUT/'preservation-baseline.json'
        baseline=read(baseline_path)
        keys=['controls_environment_launcher_and_watchlist','ledger','entry_and_recovery_claims',
              'trader_session_status_file','last_stock_decision_run','immutable_september30_sources',
              'automation_schedule_model_policy_metadata','git_identity']
        checks.update({'preserved_'+key:result[key]==baseline[key] for key in keys})
        def stable_tasks(rows):
            return [{key:value for key,value in row.items() if key!='observed_task_state'} for row in rows]
        checks['preserved_windows_launcher_configuration']=stable_tasks(tasks)==stable_tasks(baseline['windows_launcher_configuration'])
        old=baseline['repository_source_hashes']
        result['repository_source_deltas_requiring_attribution']={path:{'baseline':old.get(path),'current':before_sources.get(path)}
            for path in sorted(set(old)|set(before_sources)) if old.get(path)!=before_sources.get(path)}
        result['baseline_sha256']=sha(baseline_path)
    result['status']=('PASS' if args.compare else 'BASELINE_CAPTURED') if all(checks.values()) else 'REVIEW_REQUIRED'
    path=OUT/(label+'.json')
    with path.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,indent=2);stream.write('\n')
    print(json.dumps({'status':result['status'],'checks':checks,'source_file_count':len(before_sources),
                      'immutable_source_file_count':sum(len(item['files']) for item in sources.values()),
                      'automation_count':len(schedules),'windows_task_count':len(tasks),'output':str(path)},indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__=='__main__':
    main()
