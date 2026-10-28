"""Complete, independently renewable iterations for Atlas's data-only service.

The host owns the data lifetime and lock. This adapter owns no trading session,
account setup, manual trader controls, broker or order interface. A checkpoint
is reached only after this worker's admitted operation and resources finish.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time


def lane_symbols(symbols, workers, index):
    """Keep the original explicit allocation, without an executor backlog."""
    if type(workers) is not int or workers < 1 or type(index) is not int:
        raise ValueError("Invalid frozen data worker allocation")
    count = min(workers, len(symbols))
    if not 0 <= index < count:
        raise ValueError("Unknown frozen data preparation lane")
    return tuple(symbols[index::count])


def next_symbol(symbols, last_symbol, ready):
    symbols = tuple(symbols)
    start = symbols.index(last_symbol) + 1 if last_symbol in symbols else 0
    return next((symbol for symbol in symbols[start:] + symbols[:start]
                 if symbol not in ready), None)


def market_state(state, reference):
    """Restore the quarter worker's own queue independently of provider retries."""
    known = set(state.get("known_references", ()))
    pending = deque(state.get("pending_references", ()))
    if reference not in known:
        known.add(reference)
        pending.append(reference)
    return known, pending, bool(state.get("collect_turn", True))


def _collection_schema(store):
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS system_1a_collection_days("
                   "identity TEXT PRIMARY KEY,close_at TEXT NOT NULL,configuration TEXT NOT NULL,symbols TEXT NOT NULL,"
                   "status TEXT NOT NULL,last_attempt_at TEXT NOT NULL)")


def _collection_day(actor, close):
    from .prediction import PACIFIC, instant
    return "system-1a:" + actor.lower() + ":" + instant(close).astimezone(PACIFIC).date().isoformat() + ":collection"


def _admit_collection_day(store, session, close, now):
    from .prediction import encoded, instant
    day = _collection_day(session["actor"], close)
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO system_1a_collection_days VALUES(?,?,?,?,?,?)",
                   (day, instant(close).isoformat(), encoded(session["config"]),
                    encoded(session["symbols"]), "pending", instant(now).isoformat()))
        row = db.execute("SELECT close_at,configuration,symbols FROM system_1a_collection_days WHERE identity=?",
                         (day,)).fetchone()
    if row[0] != instant(close).isoformat():
        raise ValueError("The data window identity changed its original close")
    # A previous admitted process may have frozen this day's collection under
    # earlier local settings. Preserve and consume that original row; a new
    # worker must neither relabel it nor make a settings comparison a new gate.
    return day


def _pending_collection(store):
    with store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM system_1a_collection_days WHERE status='pending'").fetchone()[0]


def _history(root, store, session, config):
    """Same lazy retained-history rules as the existing independent service."""
    from .data import AcquisitionPending, load_local_history
    from .coverage import RECOVERY_POPULATION_CONTRACT
    from .prediction import instant

    class Histories(dict):
        def __init__(self):
            super().__init__()
            self.missing = set()
            self.loading = threading.RLock()

        def _load(self, symbol):
            with self.loading:
                if super().__contains__(symbol) or symbol in self.missing or symbol not in session["symbols"]:
                    return
                try:
                    self[symbol] = load_local_history(root, symbol, source_dataset=config.source_dataset,
                        **({"population_contract": config.population_contract} if config.population_contract else {}),
                        **({"recovery_cutoff": instant(session["started_at"])}
                           if config.population_contract == RECOVERY_POPULATION_CONTRACT else {}))
                except Exception as exc:
                    code = getattr(exc, "sqlite_errorcode", None)
                    if isinstance(exc, sqlite3.OperationalError) and type(code) is int and code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise AcquisitionPending("local_history_storage_contention", retry_after_seconds=1) from exc
                    self.missing.add(symbol)
                    store.event(session["session_id"], "history_unavailable", {"error_type": type(exc).__name__}, symbol=symbol)

        def __getitem__(self, symbol):
            self._load(symbol)
            return super().__getitem__(symbol)

        def __contains__(self, symbol):
            self._load(symbol)
            return super().__contains__(symbol)

        def get(self, symbol, default=None):
            self._load(symbol)
            return super().get(symbol, default)

        def values(self):
            for symbol in session["symbols"]:
                self._load(symbol)
            return super().values()

        def __iter__(self):
            for symbol in session["symbols"]:
                self._load(symbol)
            return super().__iter__()

    return Histories()


def _initialize(renewal, clock):
    from .runtime import ResearchConfig, local_symbols, with_local_data_policy
    from .resource_policy import manual_symbol_workers
    from .candle_collection import market_close_at
    from .prediction import instant
    from .split_service import data_window_close
    from .store import ShadowStore
    envelope = renewal.envelope
    root = Path(envelope["datastore"])
    actor, symbols = local_symbols(envelope["profile"], envelope["watchlist"], root)
    if actor != "Atlas":
        raise ValueError("This independent data service belongs to Atlas")
    options = dict(envelope["options"])
    options["symbol_workers"] = manual_symbol_workers(root, actor=actor, explicit=options.get("symbol_workers"))
    config = with_local_data_policy(ResearchConfig(**options), root).validate()
    if config.source_dataset != "EQUS.MINI":
        raise ValueError("Atlas data source binding changed")
    started = instant(clock())
    close = market_close_at(started)
    wake, weekdays = envelope.get("daily_data_wake"), tuple(envelope.get("data_weekdays", ()))
    data_window_close(started, close, daily_data_wake=wake, data_weekdays=weekdays)
    session = dict(session_id=envelope["session"]["session_id"], actor=actor,
                   symbols=list(symbols), config=json.loads(json.dumps(asdict(config))),
                   started_at=started.isoformat(), close_at=close.isoformat(),
                   daily_data_wake=wake, data_weekdays=list(weekdays), trading_authority=False)
    store = ShadowStore(root / "intraday/system-1a/collection.sqlite3")
    _collection_schema(store)
    _admit_collection_day(store, session, close, started)
    count = min(config.symbol_workers, len(symbols))
    return {"session": session, "roles": [*("prep-" + str(index) for index in range(count)),
                                            "candles", *(["market_inputs"] if config.market_inputs_enabled else [])]}


def _context(renewal, clock):
    from .prediction import instant
    from .runtime import ResearchConfig, local_symbols
    from .split_service import data_window_close
    from .store import ShadowStore
    session = renewal.session
    root = Path(renewal.envelope["datastore"])
    actor, symbols = local_symbols(renewal.envelope["profile"], renewal.envelope["watchlist"], root)
    if actor != "Atlas" or actor != session["actor"] or list(symbols) != session["symbols"]:
        raise ValueError("Frozen data-service membership changed")
    fields = dict(session["config"])
    fields["models"] = tuple(fields["models"])
    config = ResearchConfig(**fields).validate()
    if config.source_dataset != "EQUS.MINI":
        raise ValueError("Frozen Atlas data source changed")
    close = data_window_close(clock(), renewal.state.get("data_close_at", session["close_at"]),
        daily_data_wake=session.get("daily_data_wake"), data_weekdays=tuple(session.get("data_weekdays", ())))
    base = root / "intraday/system-1a"
    store = ShadowStore(base / "collection.sqlite3")
    _collection_schema(store)
    _admit_collection_day(store, session, close, clock())
    return root, base, session, config, store, instant(close)


def _checkpoint(renewal, state, close, clock, seconds):
    from .prediction import instant
    renewal.checkpoint({**state, "data_close_at": close.isoformat()},
                       (instant(clock()) + timedelta(seconds=seconds)).isoformat())
    raise RuntimeError("A data checkpoint must end the worker")


def _prepare(renewal, *, root, base, session, config, store, close, clock, pause):
    from .prediction import PACIFIC, STEP, digest, instant
    from .runtime import ShadowRunner, LiveHistories
    from .split_service import DataService
    from .split_handoff import ReadySnapshots
    from .store import ShadowStore
    from .live_session import eligible_reference, latest_completed_reference
    from .data import AcquisitionJournal
    from app.services.databento_market_data import DatabentoMarketDataProvider
    index = int(renewal.request["role"].split("-", 1)[1])
    assigned = lane_symbols(session["symbols"], config.symbol_workers, index)
    reference = latest_completed_reference(clock(), publication_offset_seconds=config.publication_offset_seconds)
    if not eligible_reference(reference):
        _checkpoint(renewal, renewal.state, close, clock, 3)
    handoff = ReadySnapshots(base / "ready", actor=session["actor"], symbols=session["symbols"], clock=clock)
    config_hash = digest(asdict(config))
    experiment = "system-1a:" + session["actor"].lower() + ":" + reference.astimezone(PACIFIC).date().isoformat()
    cycle_id = digest(dict(pc=session["actor"], reference_at=reference.isoformat(),
                          configuration_hash=config_hash, mode="live", experiment=experiment))
    ready = {row["symbol"] for row in handoff.ready(clock(), configuration_hash=config_hash) if row["cycle_id"] == cycle_id}
    symbol = next_symbol(assigned, renewal.state.get("last_symbol"), ready)
    if symbol is None:
        _checkpoint(renewal, renewal.state, close, clock, 3)
    common = ShadowRunner(store=ShadowStore(base / "preparation.sqlite3"), pc=session["actor"],
        symbols=session["symbols"], config=config, clock=clock, datastore=root, stopped=renewal.stopped,
        selected_config_path=root / "intraday/model-supervision/active.json")
    # Only the short policy-envelope creation is serialized. Provider work and
    # the complete symbol pass remain independent after this guard is released.
    from datafetching.runtime_lock import interprocess_file_lock
    with interprocess_file_lock(base / "locks" / ("policy-envelope-" + cycle_id + ".lock")):
        with common.store.connect() as db:
            old = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if old is None:
            common.prepare_cycle({}, reference_at=reference, mode="live", experiment=experiment, retry_symbols=[],
                                 acquisition={"kind": "independent_system_1a_exact_interval",
                                              "original_target_close": (reference + 3 * STEP).isoformat()})
            with common.store.connect() as db:
                old = db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()
    runner = ShadowRunner(store=ShadowStore(base / ("preparation-" + symbol + ".sqlite3")),
        pc=session["actor"], symbols=session["symbols"], config=config, clock=clock, datastore=root,
        stopped=renewal.stopped, selected_config_path=root / "intraday/model-supervision/active.json")
    runner.store.begin_cycle(cycle_id, json.loads(old[0]))
    frozen = common.store.inputs(cycle_id, symbol)
    if frozen is not None and runner.store.freeze_inputs(cycle_id, symbol, frozen[1])[0] != frozen[0]:
        raise ValueError("The preparation lane already owns different original inputs")
    provider = DatabentoMarketDataProvider()
    if provider.dataset != config.source_dataset:
        raise ValueError("Data provider differs from frozen Atlas source")
    history_cutoffs = dict(renewal.state.get("history_cutoffs", {}))
    retained = history_cutoffs.get(symbol)
    if not isinstance(retained, dict) or retained.get("reference_at") != reference.isoformat():
        retained = {"reference_at": reference.isoformat(), "cutoff": instant(clock()).isoformat()}
        history_cutoffs[symbol] = retained
    histories = _history(root, store, {**session, "started_at": retained["cutoff"]}, config)
    journal = AcquisitionJournal(base / "acquisition.sqlite3")
    incoming = lambda selected: LiveHistories(histories=histories, provider=provider, store=store,
        journal=journal, reference_at=selected, clock=clock, sleep=pause, bounded=True,
        stopped=renewal.stopped, reuse_saved=True, population_contract=config.population_contract,
        restore_eagerly=False, datastore=root)
    service = DataService(runner=runner, handoff=handoff, incoming=incoming,
                          session_id=session["session_id"], clock=clock, assigned_symbols=(symbol,))
    result = service.pass_once(reference_at=reference)
    _checkpoint(renewal, {"last_symbol": symbol, "last_pass": result,
                          "history_cutoffs": history_cutoffs}, close, clock, 3)


def _candles(renewal, *, root, base, session, config, store, close, clock, pause):
    from .prediction import instant
    from .runtime import ResearchConfig
    from .candle_collection import CandleCollector
    from .data import AcquisitionJournal
    from app.services.databento_market_data import DatabentoMarketDataProvider
    now = instant(clock())
    day = _collection_day(session["actor"], close)
    turn = int(renewal.state.get("turn", 0))
    with store.connect() as db:
        current = db.execute("SELECT * FROM system_1a_collection_days WHERE identity=?", (day,)).fetchone()
        old = db.execute("SELECT * FROM system_1a_collection_days WHERE identity!=? AND status='pending' "
                         "ORDER BY last_attempt_at,close_at LIMIT 1", (day,)).fetchone()
        selected = old if old is not None and (current[4] == "complete" or turn % 3 == 2) else current
        db.execute("UPDATE system_1a_collection_days SET last_attempt_at=? WHERE identity=?", (now.isoformat(), selected[0]))
    fields = json.loads(selected[2]); fields["models"] = tuple(fields["models"])
    selected_config = ResearchConfig(**fields).validate()
    provider = DatabentoMarketDataProvider()
    if selected_config.source_dataset != provider.dataset:
        raise ValueError("Persisted collection source differs from its original provider")
    history_window = renewal.state.get("history_window")
    if not isinstance(history_window, dict) or history_window.get("close_at") != close.isoformat():
        history_window = {"close_at": close.isoformat(), "cutoff":
            session["started_at"] if close == instant(session["close_at"]) else now.isoformat()}
    selected_session = ({**session, "symbols": json.loads(selected[3]),
                         "started_at": history_window["cutoff"]} if selected[0] == day else
                        {"session_id": selected[0], "symbols": json.loads(selected[3]), "started_at": selected[1]})
    histories = _history(root, store, selected_session, selected_config)
    collector = CandleCollector(store, histories=histories, provider=provider,
        journal=AcquisitionJournal(base / "acquisition.sqlite3"), symbols=tuple(json.loads(selected[3])),
        session_id=selected[0], started_at=instant(selected[1]), close_at=instant(selected[1]),
        clock=clock, publication_offset_seconds=selected_config.publication_offset_seconds,
        stopped=renewal.stopped, population_contract=selected_config.population_contract,
        pause=pause, datastore=root, continuous=True, max_steps=1)
    result = collector.pass_once()
    with store.connect() as db:
        pending = db.execute("SELECT COUNT(*) FROM intraday_candle_collection WHERE session_id=? AND status='pending'", (selected[0],)).fetchone()[0]
        if instant(clock()) >= instant(selected[1]) and not pending:
            db.execute("UPDATE system_1a_collection_days SET status='complete' WHERE identity=?", (selected[0],))
    if instant(clock()) >= close and not _pending_collection(store):
        return {"status": "COMPLETED", "final_collection_complete": True}
    _checkpoint(renewal, {"turn": turn + 1, "last_pass": result,
                          "history_window": history_window}, close, clock, 1)


def _market(renewal, *, root, base, session, config, store, close, clock, pause):
    from .prediction import instant
    from .live_session import latest_completed_reference
    from .market_inputs import MarketInputCollector
    from .market_input_worker import _failure_details
    generation_reference = latest_completed_reference(clock(), publication_offset_seconds=config.publication_offset_seconds).isoformat()
    known, pending, collect_turn = market_state(renewal.state, generation_reference)
    collector = MarketInputCollector(root, session["symbols"], clock=clock)
    continuation = lambda: {"known_references": sorted(known), "pending_references": list(pending), "collect_turn": collect_turn}
    def event(kind, value):
        try:
            store.event(session["session_id"], kind, value)
        except Exception:
            pass  # Diagnostic storage cannot revoke a completed acquisition.
    try:
        try:
            event("system_1a_market_inputs_started", collector.start())
        except Exception as exc:
            event("system_1a_market_inputs_error", {"error_type": type(exc).__name__, "core_preparation_continues": True})
        while not renewal.stopped() and instant(clock()) < close:
            reference = latest_completed_reference(clock(), publication_offset_seconds=config.publication_offset_seconds).isoformat()
            if reference != generation_reference:
                break
            operation, stage = "collect", "collector_call"
            try:
                if pending and collect_turn:
                    collect_turn = False
                    reference = pending.popleft()
                    collected = collector.collect(instant(reference))
                    stage = "event_write"
                    event("market_inputs_collected", collected)
                else:
                    collect_turn = True
                    operation = "recover_pending"
                    recovered = collector.recover_pending()
                    if recovered:
                        stage = "event_write"
                        event("market_inputs_retry_completed", recovered)
            except Exception as exc:
                if operation == "collect" and stage == "collector_call":
                    pending.append(reference)
                secrets = (getattr(getattr(collector, "fmp", None), "api_key", None),)
                event("market_inputs_error", {**_failure_details(exc, clock(), secrets=secrets),
                    "operation": operation, "stage": stage, "reference_at": reference,
                    "collection_pending": reference in pending, "pending_collection_count": len(pending),
                    "core_forecasts_continue": True})
            pause(1)
    finally:
        collector.close()
    if renewal.stopped():
        return {"status": "STOPPED"}
    if instant(clock()) >= close and not _pending_collection(store):
        return {"status": "COMPLETED"}
    _checkpoint(renewal, continuation(), close, clock, 0)


def run(renewal, *, clock=None, sleep=time.sleep):
    """Run one original complete data-only iteration, then end this process."""
    from .store import utcnow
    from .prediction import instant
    clock = clock or utcnow
    if renewal.stopped():
        return {"status": "STOPPED"}
    role = renewal.request["role"]
    if role == "initialize":
        return _initialize(renewal, clock)
    if role not in {"candles", "market_inputs"} and not re.fullmatch(r"prep-[0-9]+", role):
        raise ValueError("Unknown data-only renewal role")
    renewal.wait_due(clock=clock, sleep=sleep)
    if renewal.stopped():
        return {"status": "STOPPED"}
    repository = Path(renewal.envelope["repository"])
    os.environ["DUCKETS_PRODUCTION_WATCHLIST"] = renewal.envelope["watchlist"]
    from dotenv import load_dotenv
    load_dotenv(repository / ".env", override=False)
    from app.services.schwab_token_store import configure_local_repository
    configure_local_repository(repository)
    root, base, session, config, store, close = _context(renewal, clock)
    if role != "candles" and instant(clock()) >= close:
        if not _pending_collection(store):
            return {"status": "COMPLETED"}
        _checkpoint(renewal, renewal.state, close, clock, 3)
    def pause(seconds):
        end = time.monotonic() + max(0, float(seconds))
        while time.monotonic() < end:
            if renewal.stopped():
                raise InterruptedError("Data-only lifetime ended; durable work retained")
            sleep(min(.1, max(0, end - time.monotonic())))
    arguments = dict(root=root, base=base, session=session, config=config, store=store, close=close, clock=clock, pause=pause)
    return (_candles(renewal, **arguments) if role == "candles" else
            _market(renewal, **arguments) if role == "market_inputs" else _prepare(renewal, **arguments))
