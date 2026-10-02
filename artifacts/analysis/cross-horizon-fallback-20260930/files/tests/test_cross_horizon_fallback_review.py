import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from ml.stock_trader.fallback_review import main, review_fallback_day


DAY='2026-10-01'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database(tmp_path):
    path=tmp_path/'holdings.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE fallback_days(action_date TEXT,payload TEXT);
            CREATE TABLE allocations(id TEXT,symbol TEXT,horizon TEXT);
            CREATE TABLE snapshots(id TEXT,observed_at TEXT);
            CREATE TABLE reservations(id TEXT,allocation TEXT,side TEXT,quantity INTEGER,
                price TEXT,filled INTEGER,status TEXT,request TEXT,broker_order TEXT);
            CREATE TABLE fills(id TEXT,reservation TEXT,quantity INTEGER,price TEXT,executed_at TEXT);
        ''')
        baseline={'account_fingerprint':'SECRET_ACCOUNT_FINGERPRINT', 'action_date':DAY,
            'baseline_id':'raw-baseline-id','observed_at':DAY+'T11:00:00+00:00',
            'source_fingerprint':'c'*64,'policy':{'policy_version':'hierarchical-bearish-fallback-v1'},
            'symbols':{'COST':{'daily_cap':150,'initial_owned_longer_horizon_shares':300}},
            'donors':{'raw-donor-id':{'symbol':'COST','horizon':'1w','initial_owned_shares':300,'daily_cap':150}}}
        db.execute('INSERT INTO fallback_days VALUES (?,?)',(DAY,json.dumps(baseline)))
        db.execute('INSERT INTO allocations VALUES (?,?,?)',('raw-donor-id','COST','1w'))
        db.execute('INSERT INTO snapshots VALUES (?,?)',('today',DAY+'T11:00:00+00:00'))
        db.execute('INSERT INTO snapshots VALUES (?,?)',('yesterday','2026-09-30T11:00:00+00:00'))
        def order(identity,kind,origin,snapshot,quantity,filled,status,fill_rows=()):
            request={'kind':kind,'action_date':origin,'snapshot':snapshot,'trigger_forecast':origin+':COST:1h@04:00',
                     'trigger_horizon':'1h','slot_quota':6}
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?)',
                (identity,'raw-donor-id','SELL',quantity,'100',filled,status,json.dumps(request),'SECRET_BROKER_ID'))
            for i,(qty,price,at) in enumerate(fill_rows):
                db.execute('INSERT INTO fills VALUES (?,?,?,?,?)',(identity+str(i),identity,qty,price,at))
        order('partial','fallback-direction-exit',DAY,'today',6,2,'PARTIAL',[(2,'99.75',DAY+'T11:00:20+00:00')])
        order('cancelled','fallback-direction-exit',DAY,'today',6,1,'CANCELLED',[(1,'100.15',DAY+'T12:00:20+00:00')])
        order('unknown','fallback-direction-exit','2026-09-30','yesterday',4,0,'UNKNOWN')
        order('normal','direction-exit',DAY,'today',10,10,'FILLED',[(10,'100.30',DAY+'T13:00:20+00:00')])
        # UTC Oct 1 at06:30 is still Sep30 Pacific and must not enter Oct1 use.
        order('previous','fallback-direction-exit','2026-09-30','yesterday',5,5,'FILLED',
              [(5,'98.50','2026-10-01T06:30:00+00:00')])
        # UTC Oct 2 at01:00 is Oct1 Pacific and must enter today's usage.
        order('late','fallback-direction-exit',DAY,'today',3,3,'FILLED',
              [(3,'101.05','2026-10-02T01:00:00+00:00')])
    return path


def test_reader_is_read_only_sanitized_and_counts_actual_pacific_fills(tmp_path):
    path=database(tmp_path)
    before={p.name:sha(p) for p in tmp_path.iterdir()}
    result=review_fallback_day(path,DAY)
    assert {p.name:sha(p) for p in tmp_path.iterdir()}==before
    assert result['status']=='OBSERVED' and result['read_only']
    encoded=json.dumps(result)
    for secret in ['SECRET_ACCOUNT_FINGERPRINT','SECRET_BROKER_ID','raw-donor-id','raw-baseline-id']:
        assert secret not in encoded
    assert result['sales_counts']=={'fallback_orders_requested':3,'normal_sell_orders_requested':1,
        'fallback_orders_with_fills':3,'normal_sell_orders_with_fills':1,'fallback_shares_filled':6,
        'normal_sell_shares_filled':10,'outstanding_fallback_orders':2,'outstanding_fallback_shares':8}
    assert result['daily_budget_usage']['symbols']['COST']=={'daily_cap':150,'filled_plus_reserved':14,'remaining':136}
    assert result['daily_budget_usage']['donors'][0]['filled_plus_reserved']==14
    donor_hash=hashlib.sha256(b'raw-donor-id').hexdigest()
    assert result['baseline']['donors'][0]['donor_allocation_id_sha256']==donor_hash
    assert all(o['donor_allocation_id_sha256']==donor_hash for o in result['fallback_orders'])
    assert result['daily_budget_usage']['donors'][0]['donor_allocation_id_sha256']==donor_hash
    assert result['read_started_at']<=result['read_completed_at']
    assert sum(o['terminal_unfilled_released_quantity'] for o in result['fallback_orders'])==5
    assert sum(o['unknown_reserved_quantity'] for o in result['fallback_orders'])==4
    prices={f['price'] for o in result['fallback_orders'] for f in o['fills_on_action_date']}
    assert prices=={'99.75','100.15','101.05'}


def test_missing_ledger_or_baseline_never_claims_zero_executions(tmp_path):
    path=tmp_path/'absent.sqlite3'
    absent=review_fallback_day(path,DAY)
    assert absent['status']=='LEDGER_UNAVAILABLE' and absent['sales_counts'] is None and not path.exists()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata (key TEXT,value TEXT)')
    not_enabled=review_fallback_day(path,DAY)
    assert not_enabled['status']=='NOT_ENABLED' and not_enabled['fallback_orders'] is None
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE fallback_days(action_date TEXT,payload TEXT)')
    no_baseline=review_fallback_day(path,DAY)
    assert no_baseline['status']=='NO_BASELINE' and no_baseline['sales_counts'] is None


def test_cli_only_writes_new_reviews_outside_datastore(tmp_path,capsys):
    datastore=tmp_path/'datastore'
    ledger_dir=datastore/'state/independent-stock-trader'
    ledger_dir.mkdir(parents=True)
    ledger=database(ledger_dir)
    before=sha(ledger)
    output=tmp_path/'reviews'
    args=['--datastore-root',str(datastore),'--action-date',DAY,'--output-dir',str(output)]
    assert main(args)==0
    with pytest.raises(SystemExit):
        main(args)
    assert sha(ledger)==before
    report=json.loads((output/'report.json').read_text())
    manifest=json.loads((output/'manifest.json').read_text())
    assert report['status']=='OBSERVED'
    assert manifest['output_files']['report.json']['checksum_sha256']==sha(output/'report.json')
    assert manifest['source_observation']['source_fingerprint']=='c'*64
    assert manifest['source_observation']['read_started_at']==report['read_started_at']
    with pytest.raises(SystemExit):
        main(['--datastore-root',str(datastore),'--action-date',DAY,'--output-dir',str(datastore/'ml/reviews')])
    assert not (datastore/'ml/reviews').exists()
    assert 'SECRET_' not in capsys.readouterr().out


def test_reader_enforces_query_only_on_existing_database(tmp_path,monkeypatch):
    path=database(tmp_path)
    real=sqlite3.connect
    calls=[]
    class CheckedConnection(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            calls.append(sql)
            return super().execute(sql,*args,**kwargs)
    def connect(database_uri,*args,**kwargs):
        assert database_uri.endswith('?mode=ro')
        assert kwargs.get('uri') is True
        return real(database_uri,*args,**kwargs,factory=CheckedConnection)
    monkeypatch.setattr('ml.stock_trader.fallback_review.sqlite3.connect',connect)
    result=review_fallback_day(path,DAY)
    assert result['status']=='OBSERVED'
    assert calls[:2]==['PRAGMA query_only=ON','BEGIN']
    assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','ALTER')) for sql in calls)
