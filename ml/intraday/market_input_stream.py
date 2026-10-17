"""Owned Databento live context transport with durable local receipt clocks.

One client per entitled dataset. Raw frames are length-prefixed DBN records:
8-byte big-endian local receipt nanoseconds, 4-byte record length, record bytes.
Segments are opened exclusively, closed at snapshot/stop, and SHA-256 bound in
the snapshot. They are private evidence, not a replacement for canonical bars.
"""
from __future__ import annotations

from collections import defaultdict, deque, OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import uuid
from typing import Any, Callable, Sequence

from .market_inputs import SOURCE_FIELDS, _clean, _iso, _number, _ratio, _utc, _native_observation

UTC = timezone.utc
MAX_RECORDS = 200000
SCHEMAS = {
    "EQUS.MINI": ("definition", "mbp-1", "ohlcv-1m"),
    "GLBX.MDP3": ("definition", "mbp-1", "mbp-10", "mbo", "ohlcv-1m", "statistics"),
    "OPRA.PILLAR": ("definition", "cbbo-1s", "trades", "ohlcv-1m", "statistics"),
}
# MBP-1 includes trade events; do not separately subscribe to duplicate trades.
RTYPES = {0:"trades",1:"mbp-1",10:"mbp-10",19:"definition",22:"mapping",33:"ohlcv-1m",24:"statistics",160:"mbo",192:"cbbo-1s"}


def _get(record: Any, name: str, default=None):
    return record.get(name, default) if isinstance(record, dict) else getattr(record,name,default)


def _price(value):
    number = _number(value)
    # DBN integer fixed-point prices; UNDEF_PRICE is int64 max.
    if number is None or abs(number) >= 9e18:
        return None
    return number / 1_000_000_000


def _event(record):
    value = _number(_get(record,"ts_event"))
    if value is None or value <= 0 or value >= 1.8e19:
        return None
    try:
        return datetime.fromtimestamp(value / 1e9,UTC)
    except (ValueError,OverflowError,OSError):
        return None


def _kind(record):
    value = _get(record,"rtype")
    try:
        return RTYPES.get(int(value),"other")
    except (ValueError,TypeError):
        return str(value)


def _root(symbol: str) -> str:
    value = str(symbol).strip().upper()
    # OCC option raw symbol is a padded root + YYMMDD + C/P + strike.
    match = re.match(r"^([A-Z0-9.]+)\s*\d{6}[CP]\d{8}$",value)
    return match.group(1) if match else value.removesuffix(".OPT")


class DatabentoMarketStreams:
    def __init__(self, root: str | Path, symbols: Sequence[str], futures: Sequence[str], *,
                 clock: Callable[[],datetime] | None = None, client_factory=None,
                 maximum_records: int = MAX_RECORDS, future_stype: str | None = None,
                 datasets: Sequence[str] | None = None):
        self.root, self.symbols, self.futures = Path(root), tuple(symbols), tuple(futures)
        self.clock = clock or (lambda:datetime.now(UTC))
        self.factory = client_factory
        self.datasets = tuple(datasets) if datasets is not None else tuple(SCHEMAS)
        if any(dataset not in SCHEMAS for dataset in self.datasets):
            raise ValueError("Live dataset outside configured Standard suite")
        self.future_stype = future_stype or os.getenv("DATABENTO_CME_CONTEXT_STYPE_IN", "").strip() or "continuous"
        if self.future_stype not in {"continuous","raw_symbol","parent"}:
            raise ValueError("Unsupported configured CME symbol type")
        self.maximum_records = int(maximum_records)
        if self.maximum_records < 1:
            raise ValueError("Positive stream record bound required")
        self._lock = threading.RLock()
        self._clients, self._files, self._status = {}, {}, {}
        self._disabled_schemas = {dataset:set() for dataset in SCHEMAS}
        self._observed_schemas = {dataset:set() for dataset in SCHEMAS}
        self._recovering, self._recovery_pending, self._terminal = set(), set(), set()
        self._diagnostics = {dataset:deque(maxlen=32) for dataset in SCHEMAS}
        self._mapping, self._definitions = {}, {}
        self._records = {dataset:deque() for dataset in SCHEMAS}
        # Constant-per-minute sufficient statistics avoid retaining every quote
        # or order update from dense OPRA/CME feeds. Raw frames remain complete.
        self._books = {dataset:{} for dataset in SCHEMAS}
        self._flows = {dataset:{} for dataset in SCHEMAS}
        self._statistics = {dataset:{} for dataset in SCHEMAS}
        self._instrument_last = {dataset:{} for dataset in SCHEMAS}
        self._seen = {dataset:OrderedDict() for dataset in SCHEMAS}
        self._dropped = defaultdict(int)
        self._overflow_at = {}
        self._segments = {dataset:deque() for dataset in SCHEMAS}
        self._raw_counts = defaultdict(int)
        self._started_at = self.clock()
        self._start_requested = False
        self._closed = False

    def start(self) -> dict:
        if self._closed:
            raise RuntimeError("Closed live input owner")
        self._start_requested = True
        key = os.getenv("DATABENTO_API_KEY", "").strip()
        if self.factory is None and not key:
            self._status = {dataset:"missing_credentials" for dataset in self.datasets}
            return dict(self._status)
        if self.factory is None:
            import databento as db
            self.factory = db.Live
        for dataset in self.datasets:
            if dataset in self._clients or dataset in self._terminal:
                continue
            self._open_dataset(dataset,key)
        return dict(self._status)

    def _open_dataset(self,dataset,key):
        client = None
        try:
            with self._lock:
                if self._closed or dataset in self._terminal:
                    return
                schemas=tuple(s for s in SCHEMAS[dataset] if s not in self._disabled_schemas[dataset])
                if not schemas:
                    self._status[dataset]="all_schemas_rejected"
                    self._terminal.add(dataset)
                    return
                # The SDK otherwise reconnects permanent authorization errors
                # indefinitely. Own recovery removes denied schemas first.
                client = self.factory(key=key,ts_out=True,reconnect_policy="none",slow_reader_behavior="warn")
                client.add_callback(lambda record,d=dataset:self.ingest(d,record),lambda exc,d=dataset:self._error(d,"callback_error"))
                client.add_reconnect_callback(lambda *args,d=dataset:self._error(d,"reconnected"),lambda exc,d=dataset:self._error(d,"reconnect_error"))
                symbols = self.futures if dataset == "GLBX.MDP3" else tuple(s+".OPT" for s in self.symbols) if dataset == "OPRA.PILLAR" else self.symbols
                stype = self.future_stype if dataset == "GLBX.MDP3" else "parent" if dataset == "OPRA.PILLAR" else "raw_symbol"
                if not symbols:
                    self._status[dataset] = "no_configured_symbols"
                    return
                for schema in schemas:
                    args = {"dataset":dataset,"schema":schema,"symbols":symbols,"stype_in":stype}
                    # Definition replay is bounded to the current service day;
                    # fresh market events begin now (no reconstructed receipts).
                    if schema == "definition":
                        args["start"] = 0
                    if schema == "mbo":
                        args["snapshot"] = True
                    client.subscribe(**args)
                self._clients[dataset] = client
                self._status[dataset] = "starting"
                client.start()
        except Exception as exc:
            with self._lock:
                self._status[dataset] = "start_unavailable"
                self._diagnostic(dataset,"start_error",str(exc))
                self._clients.pop(dataset,None)
            if client is not None:
                try:
                    client.terminate()
                except Exception:
                    pass

    def _provider_error(self,dataset,record):
        message=str(_get(record,"err",""))
        self._status[dataset]="provider_error_record"
        self._diagnostic(dataset,_get(record,"code","unknown"),message)
        match=re.search(r"not authorized for ([a-z0-9-]+) schema",message,re.I)
        schema=match.group(1).lower() if match else None
        if schema in SCHEMAS[dataset]:
            if schema in self._disabled_schemas[dataset]:
                return
            self._disabled_schemas[dataset].add(schema)
            self._recovery_pending.add(dataset)
            if dataset not in self._recovering:
                self._recovering.add(dataset)
                threading.Thread(target=self._recover_dataset,args=(dataset,),daemon=True,name="market-input-schema-recovery").start()
        elif re.search(r"not authorized|auth.*fail|api.?key.*deactivat|connection limit",message,re.I):
            self._terminal.add(dataset)
            self._recovery_pending.add(dataset)
            if dataset not in self._recovering:
                self._recovering.add(dataset)
                threading.Thread(target=self._recover_dataset,args=(dataset,),daemon=True,name="market-input-rejection-close").start()

    def _recover_dataset(self,dataset):
        # Each rejection permanently removes one schema from this owner. The
        # loop therefore has at most len(SCHEMAS[dataset]) recovery attempts.
        while True:
            with self._lock:
                if self._closed or dataset not in self._recovery_pending:
                    self._recovering.discard(dataset)
                    return
                self._recovery_pending.discard(dataset)
                client=self._clients.pop(dataset,None)
            if client is not None:
                try:
                    # Consume the SDK's rejected-session future before cleanup;
                    # immediate terminate loses that future and logs its error.
                    client.block_for_close(timeout=2)
                except Exception:
                    try:client.terminate()
                    except Exception:pass
            self._open_dataset(dataset,os.getenv("DATABENTO_API_KEY", "").strip())

    def _error(self,dataset,status):
        with self._lock:
            self._status[dataset] = status

    def _diagnostic(self,dataset,code,message):
        message=str(message)
        for key in ("DATABENTO_API_KEY","FMP_API_KEY"):
            secret=os.getenv(key,"")
            if secret:message=message.replace(secret,"[REDACTED]")
        message=re.sub(r"https?://\S+","[URL]",message)
        message=re.sub(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)\s*[:=]\s*[^\s,;]+",r"\1=[REDACTED]",message)
        message=re.sub(r"(?i)bearer\s+\S+","Bearer [REDACTED]",message)
        value={"dataset":dataset,"received_at":_iso(self.clock()),"code":str(code)[:80],"message":_clean(message[:1024])}
        self._diagnostics[dataset].append(value)
        try:
            raw=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
            folder=self.root/"stream-errors"/_utc(self.clock()).strftime("%Y/%m/%d")
            folder.mkdir(parents=True,exist_ok=True)
            path=folder/(hashlib.sha256(raw).hexdigest()+".json")
            with path.open("xb") as output:output.write(raw)
        except FileExistsError:
            pass
        except OSError:
            self._status[dataset]="diagnostic_persistence_error"

    def _raw(self,dataset,record,received):
        if dataset not in self._files:
            folder = self.root / "streams" / dataset / received.strftime("%Y/%m/%d")
            folder.mkdir(parents=True,exist_ok=True)
            path = folder / (uuid.uuid4().hex+".frames")
            self._files[dataset] = {"path":path,"handle":path.open("xb",buffering=1024*1024),"sha":hashlib.sha256(),"first":received,"last":received,"records":0,"bytes":0}
        stream = self._files[dataset]
        raw = bytes(record)
        frame = struct.pack(">QI",int(received.timestamp()*1e9),len(raw))+raw
        stream["handle"].write(frame)
        stream["sha"].update(frame)
        stream["last"], stream["records"], stream["bytes"] = received,stream["records"]+1,stream["bytes"]+len(frame)
        self._raw_counts[dataset] += 1

    def ingest(self,dataset,record):
        received = _utc(self.clock())
        with self._lock:
            if self._closed:
                return
            kind = _kind(record)
            if int(_get(record,"rtype",-1)) == 21:
                self._provider_error(dataset,record)
                return
            if kind == "other":
                return
            try:
                self._raw(dataset,record,received)
            except Exception:
                self._status[dataset] = "persistence_error"
                return
            if kind != "mapping":
                self._observed_schemas[dataset].add(kind)
            instrument = str(_get(record,"instrument_id", ""))
            if kind == "mapping":
                incoming = str(_get(record,"stype_in_symbol", "")).strip("\x00")
                outgoing = str(_get(record,"stype_out_symbol", "")).strip("\x00")
                self._mapping[dataset,instrument] = incoming if dataset != "OPRA.PILLAR" else outgoing
                return
            if kind == "definition":
                symbol = str(_get(record,"raw_symbol", "")).strip("\x00")
                self._definitions[dataset,instrument] = {"raw_symbol":symbol,"received_at":_iso(received),"expiration":str(_get(record,"expiration","")),"strike_price":_price(_get(record,"strike_price")),"instrument_class":str(_get(record,"instrument_class",""))}
                self._definitions[dataset,instrument]["raw_segment_path"] = str(self._files[dataset]["path"])
                if dataset != "GLBX.MDP3":
                    self._mapping[dataset,instrument] = symbol
                return
            if kind not in {"trades","mbp-1","mbp-10","mbo","ohlcv-1m","cbbo-1s","statistics"}:
                return
            if not _native_observation(record):
                # Preserve the original raw frame, but reject explicit synthetic
                # or source-empty markers before dropping fields into features.
                return
            flags = int(_get(record,"flags",0) or 0)
            if flags & 4 or (kind=="mbo" and flags & 32):
                # An unrecoverable book gap is not valid numeric context. MBO
                # snapshot adds are existing orders, not new order-flow events.
                return
            event = _event(record)
            maximum_age = timedelta(days=3) if kind == "statistics" else timedelta(minutes=30)
            if event is None or event > received or event < received-maximum_age:
                return
            symbol = self._mapping.get((dataset,instrument),"")
            row = {"received_at":received,"event_at":event,"kind":kind,"instrument":instrument,"symbol":symbol,
                   "action":str(_get(record,"action","")),"side":str(_get(record,"side","")),
                   "price":_price(_get(record,"price")),"size":_number(_get(record,"size")),"sequence":_get(record,"sequence"),
                   "stat_type":_get(record,"stat_type"),"quantity":_number(_get(record,"quantity")),"levels":[]}
            row["raw_segment_path"] = str(self._files[dataset]["path"])
            for level in list(_get(record,"levels",[]) or [])[:10]:
                row["levels"].append({"bid":_price(_get(level,"bid_px")),"ask":_price(_get(level,"ask_px")),"bid_size":_number(_get(level,"bid_sz")),"ask_size":_number(_get(level,"ask_sz"))})
            self._accumulate(dataset,row)
            overflow = self._overflow_at.get(dataset)
            self._status[dataset] = "receiving" if overflow is None or received-overflow > timedelta(minutes=15) else "derived_window_overflow"

    def _group(self,dataset,symbol):
        if dataset == "GLBX.MDP3":
            match = re.match(r"^(ES|NQ|RTY|CL|GC|ZN)(?:[.A-Z0-9].*)?$",symbol.upper())
            return "db_cme_"+match.group(1).lower() if match else ""
        return _root(symbol)

    def _accumulate(self,dataset,row):
        group = self._group(dataset,row["symbol"])
        if not group:
            return
        identity=(row["kind"],row["instrument"],row["event_at"],row["sequence"],row["action"],row["price"],row["size"])
        if identity in self._seen[dataset]:
            return
        self._seen[dataset][identity] = True
        if len(self._seen[dataset])>self.maximum_records:
            self._seen[dataset].popitem(last=False)
        if row["levels"]:
            previous=self._books[dataset].get(row["instrument"])
            if previous is None or row["kind"]=="mbp-10" or previous["kind"]!="mbp-10" or row["received_at"]-previous["received_at"]>timedelta(seconds=30):
                self._books[dataset][row["instrument"]] = row
        self._instrument_last[dataset][row["instrument"]]=(row["received_at"],group)
        if row["kind"]=="statistics":
            self._statistics[dataset][row["instrument"],str(row["stat_type"])] = row
        minute = row["received_at"].replace(second=0,microsecond=0)
        key=(minute,group)
        flow = self._flows[dataset].setdefault(key,{"received_at":row["received_at"],"first_received_at":row["received_at"],"group":group,"rows":0,"trade_count":0,"volume":0.,"notional":0.,"signed":0.,"puts":0.,"calls":0.,"adds":0,"cancels":0,"bars":0})
        flow["received_at"]=row["received_at"];flow["rows"]+=1
        trade=row["kind"]=="trades" or (row["kind"]=="mbp-1" and row["action"]=="T")
        if trade and row["price"] is not None and row["price"]>0 and row["size"] is not None and row["size"]>=0:
            flow["trade_count"]+=1;flow["volume"]+=row["size"];flow["notional"]+=row["price"]*row["size"]
            flow["signed"]+=row["size"]*(1 if row["side"]=="B" else -1 if row["side"]=="A" else 0)
            if re.search(r"\d{6}P\d{8}$",row["symbol"]):flow["puts"]+=row["size"]
            if re.search(r"\d{6}C\d{8}$",row["symbol"]):flow["calls"]+=row["size"]
        flow["adds"]+=row["kind"]=="mbo" and row["action"]=="A"
        flow["cancels"]+=row["kind"]=="mbo" and row["action"]=="C"
        flow["bars"]+=row["kind"]=="ohlcv-1m"
        # Bounds count live instruments/minute buckets, not event throughput.
        while len(self._books[dataset])+len(self._statistics[dataset])+len(self._flows[dataset])+len(self._instrument_last[dataset])>self.maximum_records:
            self._dropped[dataset]+=1;self._overflow_at[dataset]=row["received_at"]
            if self._books[dataset]:self._books[dataset].pop(next(iter(self._books[dataset])))
            elif self._statistics[dataset]:self._statistics[dataset].pop(next(iter(self._statistics[dataset])))
            elif self._instrument_last[dataset]:self._instrument_last[dataset].pop(next(iter(self._instrument_last[dataset])))
            else:self._flows[dataset].pop(next(iter(self._flows[dataset])))

    def _window(self,dataset,received):
        boundary=(received-timedelta(minutes=15)).replace(second=0,microsecond=0)
        for key in [k for k in self._flows[dataset] if k[0]<boundary]:
            del self._flows[dataset][key]
        for instrument in [k for k,v in self._books[dataset].items() if v["received_at"]<boundary]:
            del self._books[dataset][instrument]
        for instrument in [k for k,v in self._instrument_last[dataset].items() if v[0]<boundary]:
            del self._instrument_last[dataset][instrument]
        for key in [k for k,v in self._statistics[dataset].items() if v["received_at"]<received-timedelta(days=3)]:
            del self._statistics[dataset][key]
        groups=defaultdict(lambda:{"rows":[],"flows":[],"contracts":set()})
        for instrument,(_,group) in self._instrument_last[dataset].items():
            groups[group]["contracts"].add(instrument)
        for row in [*self._books[dataset].values(),*self._statistics[dataset].values()]:
            if row["received_at"]<=received:
                groups[self._group(dataset,row["symbol"])]["rows"].append(row)
        for flow in self._flows[dataset].values():
            if flow["received_at"]<=received:
                groups[flow["group"]]["flows"].append(flow)
        return groups

    def _seal(self,dataset):
        stream = self._files.pop(dataset,None)
        if stream is None:
            return None
        try:
            stream["handle"].flush()
            os.fsync(stream["handle"].fileno())
        finally:
            stream["handle"].close()
        manifest = {"path":str(stream["path"]),"sha256":stream["sha"].hexdigest(),"bytes":stream["bytes"],"records":stream["records"],"first_received_at":_iso(stream["first"]),"last_received_at":_iso(stream["last"]),"encoding":"receipt-ns-u64be,length-u32be,DBN-record"}
        with stream["path"].with_suffix(".json").open("x",encoding="utf-8") as output:
            json.dump(manifest,output,sort_keys=True)
        self._segments[dataset].append(manifest)
        return manifest

    def snapshot(self) -> list[dict]:
        received = _utc(self.clock())
        result = []
        with self._lock:
            for dataset in self.datasets:
                client=self._clients.get(dataset)
                connected=client is not None and (not hasattr(client,"is_connected") or client.is_connected())
                if self._start_requested and not self._closed and dataset not in self._terminal and dataset not in self._recovering and not connected:
                    self._status[dataset]="disconnected"
                    self._recovery_pending.add(dataset)
                    self._recovering.add(dataset)
                    threading.Thread(target=self._recover_dataset,args=(dataset,),daemon=True,name="market-input-connection-recovery").start()
                segment = self._seal(dataset)
                while self._segments[dataset] and _utc(self._segments[dataset][0]["last_received_at"]) < received-timedelta(minutes=30):
                    self._segments[dataset].popleft()
                status = self._status.get(dataset,"not_started")
                groups = self._window(dataset,received)
                expected = list(dict.fromkeys([*(self._group(dataset,x) for x in self.futures),*groups])) if dataset == "GLBX.MDP3" else self.symbols
                for group in expected:
                    source = group if dataset == "GLBX.MDP3" else "db_options" if dataset == "OPRA.PILLAR" else "db_equity"
                    if source not in SOURCE_FIELDS:
                        continue
                    values = groups.get(group,{"rows":[],"flows":[],"contracts":set()})
                    rows, flows = values["rows"], values["flows"]
                    features = _summarize(rows,dataset)
                    count = sum(f["trade_count"] for f in flows)
                    volume = sum(f["volume"] for f in flows)
                    if count:
                        features.update(trade_count=count,trade_volume=volume,vwap=_ratio(sum(f["notional"] for f in flows),volume),trade_imbalance=_ratio(sum(f["signed"] for f in flows),volume))
                        if dataset=="OPRA.PILLAR":features["put_call_volume"]=_ratio(sum(f["puts"] for f in flows),sum(f["calls"] for f in flows))
                    adds,cancels=sum(f["adds"] for f in flows),sum(f["cancels"] for f in flows)
                    if adds:features["cancel_add_ratio"]=cancels/adds
                    bars=sum(f["bars"] for f in flows)
                    if bars:features["observed_bars"]=bars
                    contracts=values["contracts"]
                    if contracts:features["contracts"]=len(contracts)
                    ok = bool(rows or flows) and status == "receiving"
                    instrument_ids = {r["instrument"] for r in rows} | contracts
                    definitions = {i:self._definitions.get((dataset,i)) for i in instrument_ids}
                    segments = {x["path"]:x for x in self._segments[dataset]}
                    extra_paths = {r["raw_segment_path"] for r in rows} | {v["raw_segment_path"] for v in definitions.values() if v}
                    for raw_path in extra_paths:
                        manifest_path=Path(raw_path).with_suffix(".json")
                        if raw_path not in segments and manifest_path.exists():
                            segments[raw_path]=json.loads(manifest_path.read_text())
                    result.append({"symbol":None if dataset == "GLBX.MDP3" else group,"source":source,"started_at":_iso(self._started_at),"received_at":_iso(received),"status":"ok" if ok else "unavailable","features":features,
                        "request":{"dataset":dataset,"schemas":list(SCHEMAS[dataset]),"explicitly_rejected_schemas":sorted(self._disabled_schemas[dataset]),"remaining_requested_schemas":[s for s in SCHEMAS[dataset] if s not in self._disabled_schemas[dataset]],"observed_schemas":sorted(self._observed_schemas[dataset]),"acceptance":"Only received records establish observed schema access; remaining requested schemas are unverified","window_minutes":15,"window_start":_iso((received-timedelta(minutes=15)).replace(second=0,microsecond=0))},
                        "payload":{"stream_status":status,"diagnostics":list(self._diagnostics[dataset]),"raw_segments":list(segments.values()),"definitions":definitions,"window_records":sum(f["rows"] for f in flows),"derived_state_entries":len(self._books[dataset])+len(self._flows[dataset])+len(self._statistics[dataset])+len(self._instrument_last[dataset]),"derived_dropped_records":self._dropped[dataset],"cumulative_raw_records":self._raw_counts[dataset],"coverage":"minute sufficient statistics plus latest book per configured instrument; window starts at the minute floor; no market-wide completeness claim"}})
        return result

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for client in list(self._clients.values()):
            try:
                client.stop()
                client.block_for_close(timeout=2)
            except Exception:
                try:
                    client.terminate()
                except Exception:
                    pass
        with self._lock:
            for dataset in list(self._files):
                try:
                    self._seal(dataset)
                except Exception:
                    self._status[dataset]="seal_error_on_close"


def _summarize(rows,dataset):
    result = {}
    seen, trades, books, stats = set(), [], [], []
    adds = cancels = bars = 0
    for row in rows:
        identity = (row["instrument"],row["event_at"],row["sequence"],row["action"],row["price"],row["size"],row["kind"])
        if identity in seen:
            continue
        seen.add(identity)
        if row["kind"] == "trades" or (row["kind"] == "mbp-1" and row["action"] == "T"):
            if row["price"] is not None and row["price"] > 0 and row["size"] is not None and row["size"] >= 0:
                trades.append(row)
        if row["levels"]:
            books.append(row)
        if row["kind"] == "statistics":
            stats.append(row)
        adds += row["kind"] == "mbo" and row["action"] == "A"
        cancels += row["kind"] == "mbo" and row["action"] == "C"
        bars += row["kind"] == "ohlcv-1m"
    if trades:
        volume = sum(r["size"] for r in trades)
        result.update(trade_volume=volume,trade_count=len(trades),vwap=_ratio(sum(r["price"]*r["size"] for r in trades),volume),trade_imbalance=_ratio(sum(r["size"]*(1 if r["side"]=="B" else -1 if r["side"]=="A" else 0) for r in trades),volume))
        if dataset == "OPRA.PILLAR":
            puts = sum(r["size"] for r in trades if re.search(r"\d{6}P\d{8}$",r["symbol"]))
            calls = sum(r["size"] for r in trades if re.search(r"\d{6}C\d{8}$",r["symbol"]))
            result["put_call_volume"] = _ratio(puts,calls)
    if books:
        # Latest state per instrument rather than activity-weighted quote skew.
        latest = {r["instrument"]:r for r in books}
        spreads, imbalances, depth = [], [], []
        for row in latest.values():
            level = row["levels"][0]
            bid, ask, bs, az = (level[k] for k in ("bid","ask","bid_size","ask_size"))
            if bid and ask and ask >= bid:
                spreads.append((ask-bid)/((ask+bid)/2))
            if bid and ask and ask >= bid and bs is not None and az is not None and 0 <= bs < 4294967295 and 0 <= az < 4294967295 and bs+az > 0:
                imbalances.append((bs-az)/(bs+az))
            sizes = [(r["bid_size"],r["ask_size"]) for r in row["levels"] if row["kind"]=="mbp-10" and r["bid"] and r["ask"] and r["ask"]>=r["bid"] and r["bid_size"] is not None and r["ask_size"] is not None and 0<=r["bid_size"]<4294967295 and 0<=r["ask_size"]<4294967295]
            if sizes and sum(a+b for a,b in sizes)>0:
                depth.append(sum(a-b for a,b in sizes)/sum(a+b for a,b in sizes))
        result.update(spread_relative=sum(spreads)/len(spreads) if spreads else None,quote_imbalance=sum(imbalances)/len(imbalances) if imbalances else None,depth_imbalance=sum(depth)/len(depth) if depth else None)
    if rows:
        result["contracts"] = len({r["instrument"] for r in rows})
    if bars:
        result["observed_bars"] = bars
    if adds:
        result["cancel_add_ratio"] = cancels/adds
    # DBN StatType: settlement=3, open interest=9 (raw types also retained).
    oi = [r["quantity"] for r in stats if r["stat_type"] == 9 and r["quantity"] is not None]
    settlement = [r["price"] for r in stats if r["stat_type"] == 3 and r["price"] is not None]
    if oi:
        result["open_interest"] = sum(oi) if dataset=="OPRA.PILLAR" else oi[-1]
    if settlement:
        result["settlement"] = settlement[-1]
    return result
