"""Finalize saved own-session outcomes without rereading unchanged model trees."""
from __future__ import annotations

from .prediction import ContractError, digest, encoded, instant, number


def _session_forecast_headers(store, *, session_id, actor, membership, stopped):
    """Use the store's exact revision proof before filtering eligible forecasts.

    Scope by the original cycle and requested symbols, rather than indexed
    forecast PC: a damaged indexed PC must still reach semantic validation.
    Missing and future targets retain the same original validation as ready ones.
    Only scalar validated projections survive the pinned source read snapshot.
    """
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        cycles = db.execute("SELECT id FROM cycles WHERE pc=? AND json_extract(payload,'$.mode')='live' "
                            "AND json_extract(payload,'$.experiment')=?", (actor, session_id)).fetchall()
        if not cycles or stopped():
            return []
        cycle_order = {row[0]: index for index, row in enumerate(cycles)}
        headers = store._revision_forecast_headers(db,
            ["f.cycle_id IN (SELECT value FROM json_each(?))",
             "f.symbol IN (SELECT value FROM json_each(?))"],
            [encoded(list(cycle_order)), encoded(sorted(membership))], stopped=stopped)
        return sorted(headers, key=lambda row: (cycle_order[row["cycle_id"]], row["symbol"]))


def _cached_outcome(store, projection, candle, *, now, stopped):
    """Recheck the exact original and latest outcome in one fresh WAL snapshot."""
    with store._execution_forecast_lock, store.connect() as db:
        db.execute("BEGIN")
        headers = store._revision_forecast_headers(db, ["f.id=?"],
            [projection["forecast_id"]], stopped=stopped)
        if stopped():
            return None
        if len(headers) != 1 or headers[0] != projection:
            raise ContractError("Final scoring original changed after indexed selection")
        raw = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? "
                         "ORDER BY rowid DESC LIMIT 1", (projection["forecast_id"],)).fetchone()
        previous = store._outcome_value(raw) if raw else None
        if previous is not None and instant(previous["observed_at"]) > now:
            return "newer_observation_retained"
        if previous is not None and "reference_open" in projection:
            reference_open = number(projection["reference_open"], positive=True)
            price = str(candle["close"])
            expected = {"forecast_id": projection["forecast_id"],
                "status": "synthetic" if candle.get("synthetic") else "scored",
                "target_not_down": number(price, positive=True) >= reference_open,
                "reference_to_target_return": str(number(price) / reference_open - 1),
                "return_basis": "reference_open_to_exact_target_close_not_trade_pnl",
                "target_close_price": price, "candle_id": digest(candle),
                "unavailable_reason": None, "availability_basis": candle.get("availability_kind")}
            if previous["revision_id"] == digest(expected):
                return expected["status"]
        return None


def score_saved_session_outcomes(store, *, session_id, actor, symbols, as_of, stopped=lambda: False):
    """Score persisted own targets and preserve the original final-pass counts.

    Already scored identical targets still count in this invocation. Their
    validated revision headers and latest outcome supply the same proof
    without loading unchanged model-bearing originals again. New or corrected
    targets retain ordinary immutable-original validation and atomic observation.
    No acquisition, model, execution client, queue or worker is constructed.
    """
    from .data import completed_15m_observation, synthetic_continuity_verified
    now, membership = instant(as_of), set(symbols)
    if actor not in {"Atlas", "Scout"} or not membership:
        raise ContractError("Final scoring requires this manual session's actor and symbols")
    result = {"session_id": session_id, "scored": 0, "synthetic": 0, "unavailable": 0}
    if stopped():
        return result
    headers = _session_forecast_headers(store, session_id=session_id, actor=actor,
                                       membership=membership, stopped=stopped)
    latest, loaded_symbols = {}, set()
    for projection in headers:
        if stopped():
            return result
        if (projection["pc"] != actor or projection["mode"] != "live"
                or instant(projection["issued_at"]) > now
                or instant(projection["target_close"]) > now):
            continue
        symbol = projection["symbol"]
        if symbol not in loaded_symbols:
            # Preserve validation of every saved candle for this eligible
            # symbol, including older/future/unrelated corrupt evidence.
            for candle in store.observed_candles(symbol):
                if (instant(candle["available_at"]) <= now and completed_15m_observation(candle)
                        and synthetic_continuity_verified(candle)):
                    latest[(symbol, candle.get("source_id"), candle.get("source_dataset"),
                            candle.get("population_contract"), instant(candle["opened_at"]))] = candle
            loaded_symbols.add(symbol)
        if stopped():
            return result
        candle = latest.get((symbol, projection.get("source_id"), projection.get("source_dataset"),
                             projection.get("population_contract"), instant(projection["target_open"])))
        if candle is None:
            result["unavailable"] += 1
            continue
        cached = _cached_outcome(store, projection, candle, now=now, stopped=stopped)
        if stopped():
            return result
        if cached is not None:
            result[cached] = result.get(cached, 0) + 1
            continue
        if stopped():
            return result
        # Only newly ready/corrected evidence needs the full original. Keep at
        # most one between reads and retain the atomic concurrent-writer check.
        forecast = store.forecast(projection["cycle_id"], symbol)
        if forecast is None or forecast["forecast_id"] != projection["forecast_id"]:
            raise ContractError("Final scoring original changed after indexed selection")
        if stopped():
            return result
        outcome = store.observe(forecast, as_of=now, candle=candle, skip_newer=True)
        del forecast
        if outcome is None:
            result["newer_observation_retained"] = result.get("newer_observation_retained", 0) + 1
        else:
            result[outcome["status"]] += 1
    return result
