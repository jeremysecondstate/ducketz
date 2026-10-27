"""Renew optional market-data acquisition at its full quarter boundary.

The one-second retry subpasses keep subscribed streams continuously recording
through a quarter. They are not new scientific or execution iterations. At the
next reference the entire collector closes before a successor admits new work.
"""
from collections import deque
import time

from .prediction import instant


def renewal_market_input_pass(*, root, symbols, store, renewal, clock, config, sleep=time.sleep):
    from .live_session import latest_completed_reference
    from .market_inputs import MarketInputCollector
    from .market_input_worker import _failure_details

    session, state = renewal.session, renewal.state
    known = set(state.get("known_references", ()))
    pending = deque(state.get("pending_references", ()))
    collect_turn = bool(state.get("collect_turn", True))
    collector = MarketInputCollector(root, symbols, clock=clock)
    try:
        try:
            started = collector.start()
            store.event(session["session_id"], "market_input_streams_started", {"status_by_dataset": started})
        except Exception as exc:
            store.event(session["session_id"], "market_inputs_start_error",
                        {"error_type": type(exc).__name__, "core_forecasts_continue": True})
        renewal.wait_due(clock=clock, sleep=sleep)
        generation_reference = latest_completed_reference(clock(),
            publication_offset_seconds=config.publication_offset_seconds).isoformat()
        while not renewal.stopped() and instant(clock()) < instant(session["close_at"]):
            reference = latest_completed_reference(clock(),
                publication_offset_seconds=config.publication_offset_seconds).isoformat()
            if reference != generation_reference:
                # Existing collector cursors and provider retry descriptors are
                # already durable. Retain the loop's separate fairness queue.
                continuation = {"known_references": sorted(known), "pending_references": list(pending),
                                "collect_turn": collect_turn}
                collector.close()
                renewal.checkpoint(continuation, instant(clock()).isoformat())
                raise RuntimeError("Renewal checkpoint must end the worker")
            if reference not in known:
                known.add(reference)
                pending.append(reference)
            operation, stage = "collect", "collector_call"
            try:
                if pending and collect_turn:
                    collect_turn = False
                    reference = pending.popleft()
                    collected = collector.collect(instant(reference))
                    stage = "event_write"
                    store.event(session["session_id"], "market_inputs_collected", collected)
                else:
                    collect_turn = True
                    operation = "recover_pending"
                    recovered = collector.recover_pending()
                    if recovered:
                        stage = "event_write"
                        store.event(session["session_id"], "market_inputs_retry_completed", recovered)
            except Exception as exc:
                if operation == "collect" and stage == "collector_call":
                    pending.append(reference)
                secrets = (getattr(getattr(collector, "fmp", None), "api_key", None),)
                store.event(session["session_id"], "market_inputs_error", {
                    **_failure_details(exc, clock(), secrets=secrets), "operation": operation,
                    "stage": stage, "reference_at": reference, "collection_pending": reference in pending,
                    "pending_collection_count": len(pending), "core_forecasts_continue": True})
            # Stop checks remain bounded even when a source has nothing due.
            if not renewal.stopped():
                sleep(1)
    finally:
        collector.close()
    return {"status": "STOPPED" if renewal.stopped() else "COMPLETED", "role": "market_inputs"}
