"""Provider-free model work for an already manually admitted Atlas session."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import time

from .prediction import ContractError, STEP, digest, instant
from .runtime import ShadowRunner
from .store import ShadowStore
from .split_handoff import ReadySnapshots

ARCHITECTURE = "independent-1a1b-v1"


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
    configuration = asdict(config)
    records = handoff.ready(now=now, configuration_hash=digest(configuration),
        scientific_configuration_hash=digest({key: value for key, value in configuration.items() if key != "symbol_workers"}))
    from .live_session import latest_completed_reference
    latest = latest_completed_reference(now, publication_offset_seconds=config.publication_offset_seconds)
    records = [record for record in records if instant(record["reference_at"]) == latest]
    symbols = list(session["symbols"])
    assigned_symbols = tuple(assigned_symbols or symbols)
    if not set(assigned_symbols) <= set(symbols):
        raise ContractError("Model lane changed the original local symbol ownership")
    records = [record for record in records if record["symbol"] in assigned_symbols]
    previous = state.get("last_symbol")
    start = symbols.index(previous) + 1 if previous in symbols else 0
    rank = {symbol: index for index, symbol in enumerate(symbols[start:] + symbols[:start])}
    records = sorted(records, key=lambda record: rank[record["symbol"]])
    selected, prepared = [], {}
    for record in records:
        if max_symbols is not None and len(selected) >= max_symbols:
            break
        identity = digest(record)
        old = attempted.get(record["symbol"], {})
        if old.get("record_id") == identity and (old.get("complete")
                or old.get("retry_at") and instant(old["retry_at"]) > now):
            continue
        try:
            cycle, snapshot = handoff.load(record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            store.event(session["session_id"], "system_1b_ready_rejected", {
                "symbol": record["symbol"], "record_sha256": record.get("record_sha256"),
                "error_type": type(exc).__name__, "other_symbols_continue": True})
            attempted[record["symbol"]] = {"record_id": identity, "complete": True,
                                          "disposition": "invalid_immutable_handoff"}
            continue
        selected.append(record)
        prepared[record["symbol"]] = (digest(snapshot), snapshot)
    if not selected:
        return {"status": "WAITING_FOR_READY", "state": {**state, "attempted": attempted}}
    began = time.perf_counter()
    reference = latest
    # Preserve the fixed target deadline across the durable handoff and queue.
    if instant(clock()) >= reference + 3 * STEP:
        return {"status": "EXPIRED", "state": state}
    if stopped():
        return {"status": "STOPPED", "state": state}
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
    science_store = science_store or ShadowStore(store.path.parent / "system-1b/models.sqlite3")
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
    completed = [record["symbol"] for record in selected if record["symbol"] in published_success]
    state = {"last_symbol": selected[-1]["symbol"],
             "attempted": {key: value for key, value in attempted.items() if key in symbols}}
    store.event(session["session_id"], "system_1b_model_pass", {
        "symbols": [record["symbol"] for record in selected], "completed": completed,
        "publication_failed_symbols": sorted(publication_failed),
        "model_pass_seconds": time.perf_counter() - began,
        "queue_wait_seconds_by_symbol": {record["symbol"]: max(0., (now - instant(record["ready_at"])).total_seconds())
                                         for record in selected if "ready_at" in record},
        "target_close": (reference + 3 * STEP).isoformat(),
        "deadline_missed": instant(clock()) >= reference + 3 * STEP})
    return {"status": "PUBLISHED" if completed else "MODEL_UNAVAILABLE", "state": state,
            "symbols": completed, "cycle_id": result.get("cycle_id")}


def renewal_model_pass(*, root, store, queue, renewal, config, clock, lane_index=0, lane_count=1):
    session = renewal.session
    handoff = ReadySnapshots(Path(root) / "intraday/system-1a/ready",
        actor=session["actor"], symbols=tuple(session["symbols"]))
    result = model_pass(store=store, queue=queue, session=session, config=config,
        handoff=handoff, state=renewal.state, clock=clock, stopped=renewal.stopped,
        assigned_symbols=tuple(session["symbols"])[lane_index::lane_count],
        science_store=ShadowStore(Path(root) / ("intraday/system-1b/models-" + str(lane_index) + ".sqlite3")),
        max_symbols=1)
    if result["status"] in {"STOPPED", "COMPLETED"}:
        return {"status": result["status"], "role": renewal.request.get("role", "forecast")}
    renewal.checkpoint(result["state"], (instant(clock()) + timedelta(seconds=1)).isoformat())
    raise RuntimeError("Renewal checkpoint must end the model worker")
