"""Market inputs and durable retries owned by an already-started manual trader."""
from collections import deque
from pathlib import Path
import traceback

from .provider_recovery import safe_failure, sanitize_cause


def _failure_details(exc, now, *, secrets=()):
    """Bounded exception evidence without provider URLs, keys or frame locals."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        safe = safe_failure(current, now, secrets=secrets)
        detail = {"error_type": type(current).__name__, "message": safe["cause"],
                  "frames": [{"file": Path(frame.filename).name,
                              "function": frame.name, "line": frame.lineno}
                             for frame in traceback.extract_tb(current.__traceback__, limit=-8)]}
        code, name = getattr(current, "sqlite_errorcode", None), getattr(current, "sqlite_errorname", None)
        if type(code) is int:
            detail["sqlite_errorcode"] = code
        if isinstance(name, str):
            detail["sqlite_errorname"] = sanitize_cause(name, secrets)
        chain.append(detail)
        current = (current.__cause__ if current.__cause__ is not None else
                   None if current.__suppress_context__ else current.__context__)
    return {"error_type": type(exc).__name__, "message": chain[0]["message"],
            "exception_chain": chain, "exception_chain_truncated": current is not None}


def run_market_input_worker(collector, *, clock, stopped, pause, reference_for, event):
    """No scheduler/start authority; retry work never blocks the model worker."""
    if collector is None:
        return
    known_references, pending_references = set(), deque()
    collect_turn = True
    while not stopped():
        reference = reference_for(clock())
        if reference not in known_references:
            known_references.add(reference)
            pending_references.append(reference)
        operation, stage = "collect", "collector_call"
        try:
            if pending_references and collect_turn:
                collect_turn = False
                reference = pending_references.popleft()
                collected = collector.collect(reference)
                # Success belongs to collection, even if writing its event fails.
                stage = "event_write"
                event("market_inputs_collected", collected)
            else:
                # Durable provider retries progress even while collection fails.
                # One bounded operation per turn keeps manual stop checks active.
                collect_turn = True
                operation = "recover_pending"
                recovered = collector.recover_pending()
                if recovered:
                    stage = "event_write"
                    event("market_inputs_retry_completed", recovered)
        except Exception as exc:
            if operation == "collect" and stage == "collector_call":
                # Retain every failed reference, but let other pending work run.
                pending_references.append(reference)
            secrets = (getattr(getattr(collector, "fmp", None), "api_key", None),)
            event("market_inputs_error", {**_failure_details(exc, clock(), secrets=secrets),
                "operation": operation, "stage": stage, "reference_at": reference.isoformat(),
                "collection_pending": reference in pending_references,
                "pending_collection_count": len(pending_references), "core_forecasts_continue": True})
        pause(1)
