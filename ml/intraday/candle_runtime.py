"""One complete candle-collection pass of an existing manual session."""
from datetime import timedelta

from .candle_collection import CandleCollector, _retry_collection_storage
from .prediction import instant


def renewal_candle_pass(*, store, histories, provider, journal, renewal, config, clock, pause):
    session = renewal.session
    stopped = renewal.stopped
    if stopped():
        return {"status": "STOPPED", "role": "candles"}
    collector = _retry_collection_storage(lambda: CandleCollector(store, histories=histories,
        provider=provider, journal=journal, symbols=session["symbols"], session_id=session["session_id"],
        started_at=instant(session["started_at"]), close_at=instant(session["close_at"]),
        publication_offset_seconds=config.publication_offset_seconds, clock=clock, stopped=stopped,
        population_contract=config.population_contract, pause=pause), store=store,
        session_id=session["session_id"], operation="initialize", stopped=stopped, pause=pause)
    result = collector._storage("pass_once", collector.pass_once)
    if stopped():
        return {"status": "STOPPED", "role": "candles"}
    if result["final_complete"]:
        # This lane terminates after closeout. Planned renewals never reach this
        # branch, and unknown failures are never restarted by the controller.
        from .live_session import score_saved_session_outcomes
        def complete():
            scored = score_saved_session_outcomes(store,
                session_id=session["session_id"], actor=session["actor"], symbols=session["symbols"],
                as_of=clock(), stopped=stopped)
            if not stopped():
                store.event(session["session_id"], "final_saved_outcomes_scored", scored)
        collector._storage("on_completed", complete)
        if stopped():
            return {"status": "STOPPED", "role": "candles"}
        return {"status": "COMPLETED", "role": "candles", "final_collection_complete": True}
    renewal.checkpoint({}, (instant(clock()) + timedelta(seconds=1)).isoformat())
    raise RuntimeError("Renewal checkpoint must end the worker")
