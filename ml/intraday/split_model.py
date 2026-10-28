"""Provider-free model work for an already manually admitted Atlas session."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import sqlite3
import time

from .prediction import ContractError, STEP, digest, instant
from .runtime import ShadowRunner
from .store import ShadowStore
from .split_handoff import ReadySnapshots

ARCHITECTURE = "independent-1a1b-v1"
IDLE_ADMISSION_SECONDS = 15.


def _storage_contention(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    if type(code) is int and code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return {"error_type": type(exc).__name__, "sqlite_errorcode": code,
                "sqlite_errorname": getattr(exc, "sqlite_errorname", None)}
    return None


def _record_scope(store, session, reference, assigned, outcomes, state):
    """Retain every assigned disposition, without inventing a forecast.

    Symbol lanes together cover the frozen session universe. These append-only
    records are model evidence, not readiness or execution gates. Unchanged
    polling state does not create an acknowledgment/diagnostic write loop.
    """
    evidence = {"reference_at": reference.isoformat(),
        "target_close": (reference + 3 * STEP).isoformat(),
        "scope_symbols": list(session["symbols"]), "assigned_symbols": list(assigned),
        "symbols": outcomes}
    signature = digest(evidence)
    if state.get("scope_evidence_sha256") != signature:
        store.event(session["session_id"], "system_1b_scope_accounting", evidence)
    return {**state, "scope_evidence_sha256": signature}


def model_pass(*, store, queue, session, config, handoff, state=None,
               clock, stopped=lambda: False, runner_factory=ShadowRunner, science_store=None,
               assigned_symbols=None, max_symbols=None):
    """Fit a bounded ready batch; immutable inputs never activate trading.

    The model process has no acquisition adapter or broker. Its only output is
    the existing forecast publication callback. Execution/recovery has its own
    process and manual authority. Each completed symbol publishes immediately;
    Atlas's existing explicit symbol worker count supplies model concurrency.
    """
    state = dict(state or {})
    if max_symbols is not None and (type(max_symbols) is not int or max_symbols < 1):
        raise ContractError("A positive per-lane symbol admission bound is required")
    attempted = dict(state.get("attempted", {}))
    now = instant(clock())
    if stopped() or now >= instant(session["close_at"]):
        return {"status": "STOPPED" if stopped() else "COMPLETED", "state": state}
    manual_stopped = stopped
    stopped = lambda: manual_stopped() or instant(clock()) >= instant(session["close_at"])
    symbols = list(session["symbols"])
    assigned_symbols = tuple(symbols if assigned_symbols is None else assigned_symbols)
    if (not assigned_symbols or len(set(assigned_symbols)) != len(assigned_symbols)
            or not set(assigned_symbols) <= set(symbols)):
        raise ContractError("Model lane changed the original local symbol ownership")
    science_store = science_store or ShadowStore(store.path.parent / "system-1b/models.sqlite3")
    admission_began, admission_cpu_began = time.perf_counter(), time.process_time()
    configuration = asdict(config)
    ready_error = None
    try:
        records = handoff.ready(now=now, configuration_hash=digest(configuration),
            scientific_configuration_hash=digest({key: value for key, value in configuration.items() if key != "symbol_workers"}))
    except sqlite3.OperationalError as exc:
        ready_error = _storage_contention(exc)
        if ready_error is None:
            raise
        records = []
    ready_index_seconds = time.perf_counter() - admission_began
    from .live_session import latest_completed_reference
    latest = latest_completed_reference(now, publication_offset_seconds=config.publication_offset_seconds)
    records = [record for record in records if instant(record["reference_at"]) == latest]
    records = [record for record in records if record["symbol"] in assigned_symbols]
    outcomes = {symbol: {"disposition": "deferred",
                        "reason": "ready_index_storage_contention" if ready_error else "no_verified_current_ready_snapshot",
                        **(ready_error or {})}
                for symbol in assigned_symbols}
    for rejection in (() if ready_error else handoff.rejected):
        if rejection["symbol"] in outcomes:
            outcomes[rejection["symbol"]] = {"disposition": "deferred", "reason": "invalid_ready_record",
                                             "error_type": rejection["error_type"]}
    for record in records:
        symbol = record["symbol"]
        prior = attempted.get(symbol, {})
        if prior.get("record_id") == digest(record) and prior.get("complete"):
            outcomes[symbol] = {"disposition": prior["disposition"], "record_sha256": record["record_sha256"]}
        else:
            outcomes[symbol] = {"disposition": "deferred", "reason": "bounded_fair_queue",
                                "record_sha256": record["record_sha256"]}
    previous = state.get("last_symbol")
    start = symbols.index(previous) + 1 if previous in symbols else 0
    rank = {symbol: index for index, symbol in enumerate(symbols[start:] + symbols[:start])}
    records = sorted(records, key=lambda record: rank[record["symbol"]])
    selected, prepared, snapshot_validation_seconds = [], {}, {}
    for record in records:
        if max_symbols is not None and len(selected) >= max_symbols:
            break
        identity = digest(record)
        old = attempted.get(record["symbol"], {})
        if old.get("record_id") == identity and (old.get("complete")
                or old.get("retry_at") and instant(old["retry_at"]) > now):
            if not old.get("complete"):
                outcomes[record["symbol"]] = {"disposition": "deferred", "reason": "publication_retry_pending",
                    "retry_at": old["retry_at"], "record_sha256": record["record_sha256"]}
            continue
        loading_began = time.perf_counter()
        try:
            cycle, snapshot = handoff.load(record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            store.event(session["session_id"], "system_1b_ready_rejected", {
                "symbol": record["symbol"], "record_sha256": record.get("record_sha256"),
                "error_type": type(exc).__name__, "other_symbols_continue": True})
            attempted[record["symbol"]] = {"record_id": identity, "complete": True,
                                          "disposition": "invalid_immutable_handoff"}
            outcomes[record["symbol"]] = {"disposition": "invalid_immutable_handoff",
                "record_sha256": record["record_sha256"], "error_type": type(exc).__name__}
            continue
        finally:
            snapshot_validation_seconds[record["symbol"]] = time.perf_counter() - loading_began
        selected.append(record)
        # load verified the original bytes against this hash. ShadowRunner still
        # checks the canonical snapshot before freezing it; do not serialize the
        # complete matrix an extra time in this admission wrapper.
        prepared[record["symbol"]] = (record["snapshot_hash"], snapshot)
        outcomes[record["symbol"]] = {"disposition": "admitted", "record_sha256": record["record_sha256"]}
    state = _record_scope(science_store, session, latest, assigned_symbols, outcomes,
                          {**state, "attempted": attempted})
    if not selected:
        return {"status": "WAITING_FOR_READY", "state": state}
    began, model_cpu_began = time.perf_counter(), time.process_time()
    admission_seconds, admission_cpu_seconds = began - admission_began, model_cpu_began - admission_cpu_began
    model_started_at = instant(clock())
    reference = latest
    # Preserve the fixed target deadline across the durable handoff and queue.
    if instant(clock()) >= reference + 3 * STEP:
        for record in selected:
            outcomes[record["symbol"]] = {"disposition": "deferred", "reason": "original_target_expired",
                                          "record_sha256": record["record_sha256"]}
        state = _record_scope(science_store, session, reference, assigned_symbols, outcomes, state)
        return {"status": "EXPIRED", "state": state}
    if stopped():
        for record in selected:
            outcomes[record["symbol"]] = {"disposition": "deferred", "reason": "original_session_ended",
                                          "record_sha256": record["record_sha256"]}
        state = _record_scope(science_store, session, reference, assigned_symbols, outcomes, state)
        return {"status": "STOPPED" if manual_stopped() else "COMPLETED", "state": state}
    def prepared_source(_consumer_cycle, requested):
        if requested not in prepared:
            raise ContractError("Prepared symbol differs from ready work")
        return prepared[requested]

    published_success = set()
    publication_failed = set()
    def published(forecast):
        if stopped():
            return
        # Only small metadata and compact forecast bytes cross into execution's
        # DB. Full frozen matrices and fitted diagnostics stay in a separate DB.
        with science_store.connect() as db:
            import json
            source = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?",
                                          (forecast["cycle_id"],)).fetchone()[0])
        retained = {key: source[key] for key in ("pc", "symbols", "reference_at", "mode", "experiment",
            "configuration_hash", "started_at", "training_cutoff", "candle_close", "scheduled_acquisition")}
        retained["architecture"] = ARCHITECTURE
        def publish_exact():
            store.begin_cycle(forecast["cycle_id"], retained)
            mirrored = store.save_forecast(forecast)
            if mirrored != forecast:
                raise ContractError("Published execution forecast differs from its scientific original")
            queue.enqueue(mirrored)
        from .live_session import _retry_forecast_storage
        try:
            _retry_forecast_storage(publish_exact, store=store, session_id=session["session_id"],
                operation="compact_forecast_publication", stopped=stopped, pause=time.sleep)
        except Exception:
            publication_failed.add(forecast["symbol"])
            raise
        published_success.add(forecast["symbol"])

    from .split_execution import prepare_forecast
    runner = runner_factory(store=science_store, pc=session["actor"], symbols=tuple(symbols),
        config=config, clock=clock, stopped=stopped, prepared_source=prepared_source,
        forecast_preparer=lambda payload, snapshot, fitted: prepare_forecast(payload, snapshot, fitted,
            evidence_root=store.path.parent / "system-1b/evidence"))
    # The consumer's manual-session cycle identity stays separate from the data
    # producer identity, which is retained as immutable acquisition provenance.
    result = runner.run_cycle({}, reference_at=reference, mode="live",
        experiment=session["session_id"], cutoff=min(record["training_cutoff"] for record in selected),
        retry_symbols=[record["symbol"] for record in selected], on_published=published,
        acquisition={"kind": "verified_system_1a_ready_snapshot", "data_handoffs": selected,
                     "original_target_close": (reference + 3 * STEP).isoformat()},
        allow_execution=True)
    for record in selected:
        symbol = record["symbol"]
        publication_pending = symbol not in published_success and symbol in result.get("forecasts", {})
        attempted[symbol] = {"record_id": digest(record), "complete": not publication_pending,
            "disposition": "published" if symbol in published_success else "publication_pending" if publication_pending else "scientific_error_recorded",
            **({"retry_at": (instant(clock()) + timedelta(seconds=5)).isoformat()} if publication_pending else {})}
        outcomes[symbol] = {"disposition": attempted[symbol]["disposition"],
                            "record_sha256": record["record_sha256"]}
    completed = [record["symbol"] for record in selected if record["symbol"] in published_success]
    state = {**state, "last_symbol": selected[-1]["symbol"],
             "attempted": {key: value for key, value in attempted.items() if key in symbols}}
    state = _record_scope(science_store, session, reference, assigned_symbols, outcomes, state)
    store.event(session["session_id"], "system_1b_model_pass", {
        "symbols": [record["symbol"] for record in selected], "completed": completed,
        "publication_failed_symbols": sorted(publication_failed),
        "model_pass_seconds": time.perf_counter() - began,
        "model_pass_cpu_seconds": time.process_time() - model_cpu_began,
        "ready_index_seconds": ready_index_seconds,
        "admission_seconds": admission_seconds, "admission_cpu_seconds": admission_cpu_seconds,
        "snapshot_validation_seconds_by_symbol": snapshot_validation_seconds,
        "queue_wait_seconds_by_symbol": {record["symbol"]: max(0., (now - instant(record["ready_at"])).total_seconds())
                                         for record in selected if "ready_at" in record},
        "model_started_queue_wait_seconds_by_symbol": {
            record["symbol"]: max(0., (model_started_at - instant(record["ready_at"])).total_seconds())
            for record in selected if "ready_at" in record},
        "target_close": (reference + 3 * STEP).isoformat(),
        "deadline_missed": instant(clock()) >= reference + 3 * STEP})
    return {"status": "PUBLISHED" if completed else "MODEL_UNAVAILABLE", "state": state,
            "symbols": completed, "cycle_id": result.get("cycle_id")}


def renewal_model_pass(*, root, store, queue, renewal, config, clock, lane_index=0, lane_count=1,
                       pause=time.sleep, monotonic=time.monotonic):
    session = renewal.session
    if (type(lane_index) is not int or type(lane_count) is not int
            or not 0 <= lane_index < lane_count <= len(session["symbols"])):
        raise ContractError("Model lane differs from the frozen symbol partition")
    assigned = tuple(session["symbols"])[lane_index::lane_count]
    handoff, science_store = None, None
    state, idle_boundary = renewal.state, monotonic() + IDLE_ADMISSION_SECONDS
    while True:
        if renewal.stopped() or instant(clock()) >= instant(session["close_at"]):
            return {"status": "STOPPED" if renewal.stopped() else "COMPLETED",
                    "role": renewal.request.get("role", "forecast")}
        try:
            if handoff is None:
                handoff = ReadySnapshots(Path(root) / "intraday/system-1a/ready",
                    actor=session["actor"], symbols=tuple(session["symbols"]))
            if science_store is None:
                science_store = ShadowStore(Path(root) / ("intraday/system-1b/models-" + str(lane_index) + ".sqlite3"))
        except sqlite3.OperationalError as exc:
            contention = _storage_contention(exc)
            if contention is None:
                raise
            result = {"status": "WAITING_FOR_READY", "state": {**state,
                "admission_storage_deferral": {"reason": "model_admission_store_setup_contention",
                    "assigned_symbols": list(assigned), "original_close_at": session["close_at"], **contention}}}
        else:
            result = model_pass(store=store, queue=queue, session=session, config=config,
                handoff=handoff, state=state, clock=clock, stopped=renewal.stopped,
                assigned_symbols=assigned, science_store=science_store, max_symbols=1)
        if result["status"] in {"STOPPED", "COMPLETED"}:
            return {"status": result["status"], "role": renewal.request.get("role", "forecast")}
        state = result["state"]
        if result["status"] != "WAITING_FOR_READY" or monotonic() >= idle_boundary:
            break
        # No scientific iteration was admitted. Poll the small index in this
        # interpreter instead of verifying/importing a fresh process every
        # second. Keep the original ready cadence and responsive manual stop;
        # a bounded idle pass still yields for controller/generation adoption.
        poll_at = min(idle_boundary, monotonic() + 1.)
        while monotonic() < poll_at:
            if renewal.stopped() or instant(clock()) >= instant(session["close_at"]):
                return {"status": "STOPPED" if renewal.stopped() else "COMPLETED",
                        "role": renewal.request.get("role", "forecast")}
            pause(min(.1, max(0., poll_at - monotonic())))
    renewal.checkpoint(state, (instant(clock()) + timedelta(seconds=1)).isoformat())
    raise RuntimeError("Renewal checkpoint must end the model worker")
