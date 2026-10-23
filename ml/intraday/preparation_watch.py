"""Read-only acquisition-journal consumer for continuous non-trading preparation.

The source journal is append-only and is read in bounded rowid batches. This
module owns only its small indexed checkpoint database. It neither acquires
market data nor constructs a broker, forecast, order, or manual-stop controller.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pandas as pd
from filelock import FileLock

from .data import History, IntervalResponse, equity_sessions, validate_candles
from .native_interval import CONTRACT, _rows as _native_rows, resolve_native_interval
from .prediction import ContractError, STEP, digest, instant

BATCH_ROWS = 4096
MAX_SLOT_ATTEMPTS = 256
SAVED_PAGE_ROWS = 4096


def _candle_payload(row):
    value={}
    for key,item in row.items():
        if item is pd.NA or item is pd.NaT or isinstance(item,float) and pd.isna(item):
            item=None
        elif isinstance(item,(datetime,pd.Timestamp)):
            item=item.isoformat()
        elif callable(getattr(item,'item',None)):
            item=item.item()
        value[key]=item
    return value


def _saved_rows(db, table, symbol):
    """Page the small owned projection by its key; never truncate model history."""
    if table not in {'candles','exclusions'}:
        raise ContractError('Unknown saved preparation projection')
    after=''
    while True:
        rows=db.execute(f'SELECT opened_at,payload,sha256 FROM {table} WHERE symbol=? AND opened_at>? '
                        'ORDER BY opened_at LIMIT ?', (symbol,after,SAVED_PAGE_ROWS)).fetchall()
        for opened,raw,sha in rows:
            yield raw,sha
            after=opened
        if len(rows)<SAVED_PAGE_ROWS:
            break


def _slot_responses(db, slot):
    after=0
    values=[]
    while True:
        rows=db.execute('SELECT rowid,payload FROM attempts WHERE slot=? AND rowid>? ORDER BY rowid LIMIT ?',
                        (slot,after,MAX_SLOT_ATTEMPTS)).fetchall()
        for rowid,raw in rows:
            values.append(IntervalResponse(**json.loads(raw)).validate())
            after=rowid
        if len(rows)<MAX_SLOT_ATTEMPTS:
            return values


class SavedHistorySource:
    """Restartable overlay of the normal feed's exact saved acquisition attempts."""

    def __init__(self, *, datastore, pc, symbols, histories, output_root=None):
        self.datastore, self.pc, self.symbols = Path(datastore), pc, tuple(symbols)
        if pc not in {'Scout','Atlas'} or not self.symbols or len(set(self.symbols))!=len(self.symbols):
            raise ContractError('Saved preparation requires an explicit independent local scope')
        self.histories = dict(histories)
        self._base_actual={}
        for symbol,history in self.histories.items():
            frame=history.frame if hasattr(history,'frame') else history
            self._base_actual[symbol]={instant(row['opened_at']):_candle_payload(row) for row in
                frame.loc[frame.availability_kind.eq('actual')].to_dict('records')}
        self.source = self.datastore/'intraday/acquisition.sqlite3'
        root = Path(output_root) if output_root else self.datastore/'intraday'/pc.lower()/'preparation'
        root.mkdir(parents=True, exist_ok=True)
        self.path = root/'saved-observations.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, slot TEXT, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_slot ON attempts(slot);
                CREATE TABLE IF NOT EXISTS slots(slot TEXT PRIMARY KEY, symbol TEXT, opened_at TEXT, dirty INTEGER);
                CREATE INDEX IF NOT EXISTS slots_dirty_symbol ON slots(symbol,dirty,opened_at);
                CREATE TABLE IF NOT EXISTS candles(symbol TEXT, opened_at TEXT, payload TEXT, sha256 TEXT,
                    PRIMARY KEY(symbol,opened_at));
                CREATE TABLE IF NOT EXISTS receipts(sha256 TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exclusions(symbol TEXT, opened_at TEXT, payload TEXT, sha256 TEXT,
                    PRIMARY KEY(symbol,opened_at));
                CREATE TABLE IF NOT EXISTS quarantine(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')
            identity = {'pc': pc, 'symbols': list(self.symbols), 'source':str(self.source.resolve()),
                        'contract':CONTRACT}
            row = db.execute("SELECT payload FROM state WHERE key='identity'").fetchone()
            if row and json.loads(row[0]) != identity:
                raise ContractError('Saved preparation source identity changed')
            db.execute("INSERT OR IGNORE INTO state VALUES('identity',?)", (json.dumps(identity),))

    def refresh(self, cutoff):
        cutoff = instant(cutoff)
        with closing(sqlite3.connect(self.path)) as db, db:
            saved = db.execute("SELECT payload FROM state WHERE key='cursor'").fetchone()
            cursor = json.loads(saved[0]) if saved else {'rowid':0}
            saved_revision=db.execute("SELECT payload FROM state WHERE key='projection_revision'").fetchone()
            revision=int(saved_revision[0]) if saved_revision else 0
            rows, backlog = [], False
            if self.source.exists():
                with closing(sqlite3.connect(self.source.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                    source.execute('PRAGMA query_only=ON')
                    if cursor['rowid']:
                        prior = source.execute('SELECT id,payload FROM attempts WHERE rowid=?',
                                               (cursor['rowid'],)).fetchone()
                        if prior is None or digest(list(prior)) != cursor['sha256']:
                            raise ContractError('Saved acquisition journal was reset or revised')
                    rows = source.execute('SELECT rowid,id,slot,payload FROM attempts WHERE rowid>? '
                                          'ORDER BY rowid LIMIT ?', (cursor['rowid'],BATCH_ROWS+1)).fetchall()
                    backlog = len(rows)>BATCH_ROWS
                    rows = rows[:BATCH_ROWS]
            elif cursor['rowid']:
                raise ContractError('Previously observed acquisition journal is missing')
            quarantined=[]
            for rowid, identity, slot, raw in rows:
                cursor = {'rowid':rowid,'sha256':digest([identity,raw])}
                value=None
                try:
                    value=json.loads(raw)
                    if not isinstance(value,dict):
                        raise ContractError('Saved acquisition payload is not an object')
                    if (value.get('symbol') not in self.symbols or value.get('source_dataset')!='EQUS.MINI'
                            or value.get('source_id')!='databento:EQUS.MINI:ohlcv-1m'):
                        continue
                    response = IntervalResponse(**value).validate()
                    if response.attempt_id != identity:
                        raise ContractError('Saved acquisition attempt identity changed')
                    from .data import AcquisitionJournal
                    expected = AcquisitionJournal.slot(response.symbol, response.source_id,
                                                       response.interval_open, response.interval_close)
                    if slot != expected:
                        raise ContractError('Saved acquisition slot differs from its response')
                    previous = db.execute('SELECT payload FROM attempts WHERE id=?',(identity,)).fetchone()
                    if previous and previous[0] != raw:
                        raise ContractError('Saved acquisition attempt was revised')
                    db.execute('INSERT OR IGNORE INTO attempts VALUES(?,?,?)',(identity,slot,raw))
                    db.execute('INSERT INTO slots VALUES(?,?,?,1) ON CONFLICT(slot) DO UPDATE SET dirty=1',
                               (slot,response.symbol,instant(response.interval_open).isoformat()))
                except (ValueError,TypeError,KeyError,ContractError) as exc:
                    item={'id':identity,'source_rowid':rowid,'source_payload_sha256':digest(raw),
                          'symbol':value.get('symbol') if isinstance(value,dict) else None,
                          'reason':str(exc),'status':'quarantined_invalid_attempt'}
                    quarantined.append(item)
                    db.execute('INSERT OR IGNORE INTO quarantine VALUES(?,?)',(identity,json.dumps(item)))
            db.execute("INSERT INTO state VALUES('cursor',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                       (json.dumps(cursor),))
            # One failed symbol must not monopolize the oldest dirty-slot page.
            # Every symbol receives a bounded share, rotating if the configured
            # batch is smaller than the scope. Extra work stays explicitly local.
            prior=db.execute("SELECT payload FROM state WHERE key='projection_symbol_cursor'").fetchone()
            first=int(prior[0]) if prior else 0
            order=self.symbols[first:]+self.symbols[:first]
            quota,extra=divmod(BATCH_ROWS,len(order))
            pending=[]
            symbol_backlog=[]
            for index,symbol in enumerate(order):
                limit=quota+(index<extra)
                selected=db.execute('SELECT slot,symbol,opened_at FROM slots WHERE symbol=? AND dirty=1 '
                                    'ORDER BY opened_at LIMIT ?', (symbol,limit+1)).fetchall()
                if len(selected)>limit:
                    symbol_backlog.append(symbol)
                pending.extend(selected[:limit])
            pending.sort(key=lambda row:row[2])
            db.execute("INSERT INTO state VALUES('projection_symbol_cursor',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                       (str((first+1)%len(self.symbols)),))
            outcomes=[]
            symbol_errors={}
            sessions = equity_sessions(pending[0][2],pending[-1][2]) if pending else ()
            for slot,symbol,opened in pending:
                if symbol in symbol_errors:
                    continue
                slot_revision=revision
                outcome_start=len(outcomes)
                db.execute('SAVEPOINT slot_projection')
                try:
                    attempts=_slot_responses(db,slot)
                    eligible=[a for a in attempts if instant(a.request_ended_at)<=cutoff]
                    eligible.sort(key=lambda a:(instant(a.request_started_at),instant(a.request_ended_at),a.attempt_id))
                    session=next((s for s in sessions if s.contains(opened)),None)
                    if eligible and session is not None:
                        try:
                            result=resolve_native_interval(eligible[-2:],session=session)
                        except ContractError as exc:
                            result={'status':'unusable','reason':str(exc),'candle':None}
                        candle=result.get('candle')
                        if candle is not None:
                            # Locate the first adequate receipt for this exact native
                            # snapshot since its last observed revision. A/B/A must
                            # not inherit A's superseded original receipt clock.
                            final_observations=json.loads(candle['evidence_json'])['observations']
                            revision_start=0
                            revision_boundary=None
                            for index,attempt in enumerate(eligible[:-1]):
                                if attempt.status!='success' or not attempt.covered():
                                    continue
                                try:
                                    observed=_native_rows(attempt.rows,opened=pd.Timestamp(attempt.interval_open),
                                                          closed=pd.Timestamp(attempt.interval_close))
                                except ContractError:
                                    observed=None
                                if observed!=final_observations:
                                    revision_start=index+1
                                    revision_boundary={'kind':'observed_native_revision_v1',
                                        'attempt_id':attempt.attempt_id,'available_at':attempt.request_ended_at}
                            for index in range(revision_start,len(eligible)-1):
                                attempt=eligible[index]
                                if not attempt.rows or attempt.status!='success':
                                    continue
                                try:
                                    earlier=resolve_native_interval(eligible[max(0,index-1):index+1],session=session).get('candle')
                                except ContractError:
                                    continue
                                if (earlier is not None and
                                        json.loads(earlier['evidence_json'])['observations']==final_observations):
                                    candle=earlier
                                    break
                        outcomes.append({'symbol':symbol,'opened_at':opened,'status':result['status'],
                                         'reason':result.get('reason')})
                        if candle is not None:
                            previous=db.execute('SELECT payload,sha256 FROM candles WHERE symbol=? AND opened_at=?',
                                                (symbol,opened)).fetchone()
                            if previous and digest(json.loads(previous[0]))!=previous[1]:
                                raise ContractError('Saved preparation candle checksum mismatch')
                            prior=json.loads(previous[0]) if previous else self._base_actual.get(symbol,{}).get(instant(opened))
                            if (prior is not None and instant(prior['available_at'])<=instant(candle['available_at'])
                                    and (revision_boundary is None or
                                         instant(prior['available_at'])>instant(revision_boundary['available_at']))
                                    and json.loads(prior['evidence_json'])['observations']==
                                        json.loads(candle['evidence_json'])['observations']):
                                candle=prior
                            if revision_boundary is not None:
                                evidence=json.loads(candle['evidence_json'])
                                evidence['coverage']['revision_boundary']=revision_boundary
                                candle['evidence_json']=json.dumps(evidence,sort_keys=True,separators=(',',':'))
                            if previous is None or digest(json.loads(previous[0]))!=digest(candle):
                                revision+=1
                            db.execute('INSERT INTO candles VALUES(?,?,?,?) ON CONFLICT(symbol,opened_at) '
                                       'DO UPDATE SET payload=excluded.payload,sha256=excluded.sha256',
                                       (symbol,opened,json.dumps(candle),digest(candle)))
                            revision+=db.execute('DELETE FROM exclusions WHERE symbol=? AND opened_at=?',(symbol,opened)).rowcount
                        elif eligible[-1].status=='success' and eligible[-1].covered():
                            # A failed request is not a revision of known prices.
                            # A newly observed contradictory/empty/unusable native
                            # snapshot is withheld until the exact resolver confirms
                            # a usable replacement. Every attempt remains immutable.
                            exclusion={'symbol':symbol,'opened_at':opened,'status':result['status'],
                                       'reason':result.get('reason'),'available_at':eligible[-1].request_ended_at}
                            previous=db.execute('SELECT sha256 FROM exclusions WHERE symbol=? AND opened_at=?',
                                                (symbol,opened)).fetchone()
                            if previous is None or previous[0]!=digest(exclusion):
                                revision+=1
                            db.execute('INSERT INTO exclusions VALUES(?,?,?,?) ON CONFLICT(symbol,opened_at) '
                                       'DO UPDATE SET payload=excluded.payload,sha256=excluded.sha256',
                                       (symbol,opened,json.dumps(exclusion),digest(exclusion)))
                    if len(eligible)==len(attempts):
                        db.execute('UPDATE slots SET dirty=0 WHERE slot=?',(slot,))
                except (ContractError, ValueError, TypeError, KeyError) as exc:
                    db.execute('ROLLBACK TO slot_projection')
                    revision=slot_revision
                    del outcomes[outcome_start:]
                    outcomes.append({'symbol':symbol,'opened_at':opened,'status':'unavailable',
                                     'reason':str(exc),'persisted':False})
                    symbol_errors[symbol]={'stage':'saved_projection',
                        'error_type':type(exc).__name__,'reason':str(exc)}
                finally:
                    db.execute('RELEASE slot_projection')
            db.execute("INSERT INTO state VALUES('projection_revision',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                       (str(revision),))
            receipt={'cursor':cursor,'projection_revision':revision,'backlog':backlog,
                     'source_rows_read':len(rows),'outcomes':outcomes,'quarantined':quarantined,
                     'symbol_backlog':symbol_backlog}
            histories={}
            for symbol in self.symbols:
                if symbol in symbol_errors:
                    continue
                if symbol in symbol_backlog:
                    symbol_errors[symbol]={'stage':'saved_projection',
                        'reason':'Bounded saved projection has pending slots for this symbol','retryable':True}
                    continue
                base=self.histories.get(symbol)
                if base is None:
                    continue
                try:
                    frame=base.frame if hasattr(base,'frame') else base
                    manifest=dict(getattr(base,'manifest',frame.attrs.get('intraday_history',{})))
                    current={instant(row['opened_at']):row for row in frame.to_dict('records')}
                    used=0
                    for raw,sha in _saved_rows(db,'candles',symbol):
                        candle=json.loads(raw)
                        if digest(candle)!=sha:
                            raise ContractError('Saved preparation candle checksum mismatch')
                        if instant(candle['available_at'])>cutoff:
                            continue
                        opened=instant(candle['opened_at'])
                        previous=current.get(opened)
                        if previous is not None and previous.get('availability_kind')=='actual':
                            same=(json.loads(previous['evidence_json'])['observations']==
                                  json.loads(candle['evidence_json'])['observations'])
                            earlier=instant(previous['available_at'])<=instant(candle['available_at'])
                            boundary=json.loads(candle['evidence_json'])['coverage'].get('revision_boundary')
                            current_revision=(boundary is None or
                                              instant(previous['available_at'])>instant(boundary['available_at']))
                            # A sibling may supply its pinned base only after the
                            # shared journal checkpoint was built. Reconcile here
                            # even when that slot is no longer dirty.
                            if (same and earlier and current_revision) or (not same and not earlier):
                                if same and boundary is not None:
                                    evidence=json.loads(previous['evidence_json'])
                                    evidence['coverage']['revision_boundary']=boundary
                                    current[opened]={**previous,'evidence_json':json.dumps(
                                        evidence,sort_keys=True,separators=(',',':'))}
                                continue
                        current[opened]=candle
                        used+=1
                    excluded=0
                    for raw,sha in _saved_rows(db,'exclusions',symbol):
                        exclusion=json.loads(raw)
                        if digest(exclusion)!=sha:
                            raise ContractError('Saved source exclusion checksum mismatch')
                        if instant(exclusion['available_at'])<=cutoff:
                            current.pop(instant(exclusion['opened_at']),None)
                            excluded+=1
                    merged=validate_candles(pd.DataFrame(list(current.values())))
                    manifest['saved_preparation_overlay']={'source':'normal_acquisition_journal',
                        'cursor':cursor,'actual_candles':used,'exclusions':excluded,
                        'backlog':backlog}
                    merged.attrs['intraday_history']=manifest
                    histories[symbol]=History(merged,manifest)
                except (ContractError, ValueError, TypeError, KeyError) as exc:
                    # A corrupt symbol projection is unavailable, never silently
                    # replaced by its base. Preserve its evidence and continue
                    # healthy symbols; a global journal error still fails closed.
                    symbol_errors[symbol]={'stage':'saved_projection',
                        'error_type':type(exc).__name__,'reason':str(exc)}
            receipt['symbol_errors']=symbol_errors
            receipt_sha=digest(receipt)
            db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?)',(receipt_sha,json.dumps(receipt)))
            for history in histories.values():
                history.manifest['saved_preparation_overlay']['receipt_sha256']=receipt_sha
                history.frame.attrs['intraday_history']=history.manifest
            return histories, {**receipt,'sha256':receipt_sha}


def current_reference(now, *, offset_seconds=0):
    """Latest completed eligible quarter, including delayed session-end data.

    This reference is preparation-only. A current or future executable target
    is deliberately not required, so final labels can mature after the close.
    """
    now=instant(now)
    reference=(pd.Timestamp(now)-pd.Timedelta(seconds=offset_seconds)).floor('15min').to_pydatetime()-STEP
    sessions=equity_sessions(pd.Timestamp(now)-pd.Timedelta(days=7),now)
    candidates=[]
    for session in sessions:
        candidate=min(reference,instant(session.closed_at)-STEP)
        if session.contains(candidate):
            candidates.append(candidate)
    return max(candidates) if candidates else None


def context_receipt_identity(datastore, pc, symbols, runtime, cutoff):
    """Bounded point seeks notice context arrivals without reading raw records."""
    from .market_collection_index import selected_receipt_entries
    contract=getattr(runtime.get('dataset_builder'),'market_context_contract',None)
    if contract is None:
        return None
    root=Path(datastore)/'intraday'/pc.lower()/'market-data'
    since=pd.Timestamp(cutoff)-pd.Timedelta(seconds=max(contract['max_receipt_age_seconds'].values()))
    entries={}
    for symbol in symbols:
        values=selected_receipt_entries(root,symbol=symbol,cutoff=cutoff,since=since,
            decision_cutoffs=[cutoff],source_ids=contract['source_ids'])
        entries[symbol]=None if values is None else [(r['snapshot_id'],r['sha256']) for r in values]
    return digest(entries)


def latest_saved_references(histories, *, expected_reference_at, cutoff):
    """Choose genuine completed saved observations for preparation only.

    Existing explicit historical availability assumptions remain identified by
    their original candle contract. No source-empty or synthetic row is filled.
    """
    expected,available=instant(expected_reference_at),instant(cutoff)
    selected={}
    for symbol,history in histories.items():
        frame=history.frame if hasattr(history,'frame') else history
        eligible=frame.loc[(pd.to_datetime(frame.opened_at,utc=True)<=expected)
            & (pd.to_datetime(frame.opened_at,utc=True)+STEP<=available)
            & (pd.to_datetime(frame.available_at,utc=True)<=available)
            & frame.availability_kind.isin(('actual','actual_historical_recovery','assumed_historical_close'))
            & frame.synthetic.eq(False) & frame.native_interval_contract.eq(CONTRACT)]
        if len(eligible):
            selected[symbol]=instant(eligible.opened_at.max())
    return selected


def overlay_saved_native_history(history, *, root, symbol, pc):
    """Use the same actual-clock projection at normal opted-in runtime startup."""
    selection=Path(root)/'intraday'/pc.lower()/'minute-recovery/installed.json'
    raw=selection.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=history.manifest['recovery']['manifest_sha256']:
        raise ContractError('Recovery selection changed before actual journal projection')
    installed=json.loads(raw)
    if installed.get('pc')!=pc or symbol not in installed.get('symbols',[]):
        raise ContractError('Actual journal projection differs from installed local recovery scope')
    source=SavedHistorySource(datastore=root,pc=pc,symbols=installed['symbols'],histories={symbol:history},
        output_root=Path(root)/'intraday'/pc.lower()/'native-actual')
    cutoff=datetime.now(timezone.utc)
    for _ in range(32):
        histories,receipt=source.refresh(cutoff)
        if not receipt['backlog'] and symbol not in receipt.get('symbol_backlog',[]):
            if symbol in receipt.get('symbol_errors',{}):
                raise ContractError(receipt['symbol_errors'][symbol]['reason'])
            if selection.read_bytes()!=raw:
                raise ContractError('Recovery selection changed during actual journal projection')
            return histories[symbol]
    raise ContractError('Actual journal projection backlog remains; saved bounded progress can resume')


def run_preparation(*, datastore, pc, output_root=None, **kwargs):
    """Hold one owner-local lock for the complete saved-data watcher lifetime."""
    root=Path(output_root) if output_root else Path(datastore)/'intraday'/pc.lower()/'preparation'
    root.mkdir(parents=True,exist_ok=True)
    with FileLock(str(root/'watch.lock'),timeout=0):
        return _run_preparation(datastore=datastore,pc=pc,output_root=root,**kwargs)


def _verify_watch_completion(saved, signature, fit):
    from .preparation import _verified, _verify_artifacts
    if saved['signature']!=signature:
        raise ContractError('Saved preparation watch signature changed')
    cycle=_verified(Path(saved['cycle_path']))
    if fit:
        for item in cycle['results'].values():
            evidence=item.get('fit',{})
            if evidence.get('status') in {'fitted','reused_verified_training_artifacts'}:
                _verify_artifacts(_verified(Path(evidence['evidence_path'])))
    return {**cycle,'evidence_path':saved['cycle_path']}


def _run_preparation(*, datastore, pc, symbols, histories, config, model_runtime, fit=False,
                    stopped=lambda:False, clock=lambda:datetime.now(timezone.utc), pause=time.sleep,
                    poll_seconds=15, output_root=None, history_errors=None, emit=None, saved_source=None,
                    on_verified_reuse=None):
    """Watch saved inputs until stopped; persist progress and retry on new inputs.

    No service is installed or started by importing this entry point. The caller
    owns its stop event, clock and pause. Polling never fetches provider data.
    An optional reuse callback reports verified durable state after restart
    without representing it as a new cycle, fit, market observation or forecast.
    """
    from .preparation import prepare_cycle, _digest, _verified, _write_once, _sources
    if not 1<=poll_seconds<=60:
        raise ContractError('Preparation polling must be between one and sixty seconds')
    if set(getattr(model_runtime.get('dataset_builder'),'native_interval_symbols',()))!=set(symbols):
        raise ContractError('Saved minute watcher requires the frozen covered-native scope for every selected symbol')
    root=Path(output_root) if output_root else Path(datastore)/'intraday'/pc.lower()/'preparation'
    source=saved_source or SavedHistorySource(datastore=datastore,pc=pc,symbols=symbols,
                                             histories=histories,output_root=root)
    implementation=_sources()
    recipe_path=Path(datastore)/'intraday'/pc.lower()/'manual-model-recipe.json'
    recipe_bytes=recipe_path.read_bytes()
    base_hashes={s:hashlib.sha256((h.frame if hasattr(h,'frame') else h).to_json(
        orient='split',date_format='iso').encode()).hexdigest() for s,h in histories.items()}
    count=0
    while not stopped():
        if _sources()!=implementation or recipe_path.read_bytes()!=recipe_bytes:
            raise ContractError('Preparation source or local recipe changed; restart with reviewed inputs')
        now=instant(clock())
        reference=current_reference(now,offset_seconds=config.publication_offset_seconds)
        if reference is not None:
            errors=dict(history_errors or {})
            try:
                current,receipt=source.refresh(now)
                errors.update(receipt.get('symbol_errors',{}))
                context_identity=context_receipt_identity(datastore,pc,symbols,model_runtime,now)
            except Exception as exc:
                current,receipt={}, {'error_type':type(exc).__name__,'reason':str(exc)}
                context_identity=None
                errors.update({s:{**receipt,'stage':'saved_acquisition'} for s in symbols})
            if not receipt.get('backlog'):
                actual_references=latest_saved_references(current,expected_reference_at=reference,cutoff=now)
                for symbol in symbols:
                    if symbol not in actual_references and symbol not in errors:
                        errors[symbol]={'stage':'saved_reference','reason':'No eligible completed saved native reference',
                                        'expected_reference_at':reference.isoformat()}
                signature=_digest({'reference_at':reference.isoformat(),'receipt':receipt.get('cursor',receipt),
                    'reference_by_symbol':{s:v.isoformat() for s,v in actual_references.items()},
                    'projection_revision':receipt.get('projection_revision'),
                    'recipe_sha256':hashlib.sha256(recipe_bytes).hexdigest(),'sources':implementation,
                    'base_history_sha256':base_hashes,'context_receipts':context_identity,'errors':errors,'fit':fit})
                marker=root/'watch-completed'/(signature+'.json')
                if marker.exists():
                    saved=_verified(marker)
                    reused=_verify_watch_completion(saved,signature,fit)
                    if on_verified_reuse:
                        on_verified_reuse(reused)
                else:
                    prior_attempts=[]
                    for number in range(1,4):
                        attempt_path=root/'watch-attempts'/signature/(str(number)+'.json')
                        if attempt_path.exists():
                            prior_attempts.append(_verified(attempt_path))
                    if prior_attempts and (not prior_attempts[-1]['retryable'] or len(prior_attempts)>=3):
                        # Recover a crash after the immutable attempt write but
                        # before its completion marker, without another fit.
                        reused=_verify_watch_completion(prior_attempts[-1],signature,fit)
                        _write_once(marker,prior_attempts[-1])
                        if on_verified_reuse:
                            on_verified_reuse(reused)
                        if not stopped():
                            pause(poll_seconds)
                        continue
                    if prior_attempts and now<instant(prior_attempts[-1]['retry_at']):
                        if not stopped():
                            pause(poll_seconds)
                        continue
                    cycle=prepare_cycle(datastore=datastore,pc=pc,symbols=symbols,histories=current,
                        reference_at=reference,cutoff=now,config=config,model_runtime=model_runtime,
                        fit=fit,output_root=root,history_errors=errors,reference_by_symbol=actual_references)
                    record={'signature':signature,'cycle_path':cycle['evidence_path']}
                    record['sha256']=_digest(record)
                    # Deterministic support failures wait for changed inputs.
                    # Temporary failed execution/artifact writes get at most
                    # three durable attempts, at least one minute apart.
                    record['statuses']={s:item['status'] for s,item in cycle['results'].items()}
                    retryable=any(item.get('retryable') or item.get('fit',{}).get('retryable')
                                  for item in cycle['results'].values())
                    number=len(prior_attempts)+1
                    record.update(attempt=number,retryable=bool(retryable),
                                  retry_at=(now+pd.Timedelta(seconds=60)).isoformat())
                    record['sha256']=_digest({k:v for k,v in record.items() if k!='sha256'})
                    _write_once(root/'watch-attempts'/signature/(str(number)+'.json'),record)
                    if not retryable or number>=3:
                        _write_once(marker,record)
                    count+=1
                    if emit:
                        emit(cycle)
        if not stopped():
            pause(poll_seconds)
    return {'status':'stopped','trading':False,'cycles_prepared':count}
