"""Scoped minute refresh and explicit session coverage for 15m model supervision.

Provider access occurs only in ``refresh_training_data``. Auditing and dataset
construction are offline. Missing observations remain missing; a later row never
proves no trades in an omitted interval.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from .data import InsufficientTrainingData, build_dataset, equity_sessions, utc, validate_candles
from .prediction import ContractError, STEP


def refresh_training_data(repository, datastore, symbols, as_of):
    """Continue only canonical EQUS.MINI OHLCV minutes through existing writers.

The native batch helper supplies provider Retry-After handling, per-symbol
continuation and the existing one-hour overlap. The persistence helper performs
raw/normalized upserts and completed-bar filtering. Derived bars, archive
materialization, CME, options and account endpoints are outside this refresh.
    """
    from dotenv import load_dotenv
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from datafetching.databento_fetch import _fetch_native_results_many, _persist_native_results
    from datafetching.parquet_store import ParquetStore

    cutoff = utc(as_of)
    symbols = tuple(str(s).strip().upper() for s in symbols)
    if (not symbols or len(set(symbols)) != len(symbols)
            or any(not s or not s.replace(".", "").isalnum() for s in symbols)):
        raise ContractError("Refresh requires unique explicit local stock symbols")
    load_dotenv(Path(repository) / ".env", override=False)
    provider = DatabentoMarketDataProvider(dataset="EQUS.MINI", schema="ohlcv-1m",
                                          native_schemas=("ohlcv-1m",))
    specs = tuple(provider.native_specs())
    if (provider.dataset != "EQUS.MINI" or len(specs) != 1
            or specs[0].schema != "ohlcv-1m" or specs[0].frequency != "1m"):
        raise ContractError("Supervision refresh requires exactly EQUS.MINI OHLCV minutes")
    store = ParquetStore(Path(datastore))
    fetched = _fetch_native_results_many(provider, symbols, "continuation", store)
    results, requests = {}, []
    for symbol in symbols:
        native = fetched[symbol]
        if (len(native) != 1 or native[0][0].schema != "ohlcv-1m"
                or native[0][0].frequency != "1m"):
            raise ContractError("Refresh escaped the exact minute-only source scope")
        result = _persist_native_results(symbol, store, provider=provider,
            profile="continuation", observed_at=cutoff.to_pydatetime(), native_results=native,
            batch_watchlist_symbol_count=len(symbols), run_derived=False)
        results[symbol] = asdict(result)
        spec, bars, raw, available, error = native[0]
        requests.append({"symbol": symbol, "source_schema": spec.schema,
            "provider_available_start": utc(available.start).isoformat() if available else None,
            "provider_available_end": utc(available.end).isoformat() if available else None,
            "returned_native_rows": len(bars),
            "status": "failed" if error is not None else "fetched",
            "error_type": type(error).__name__ if error is not None else None})
    return {"schema_version": "intraday-supervision-refresh-v1",
        "status": "partial" if any(r["error_files"] for r in results.values()) else "complete",
        "as_of": cutoff.isoformat(), "source_dataset": "EQUS.MINI",
        "source_schema": "ohlcv-1m", "symbols": list(symbols), "results": results,
        "requests": requests, "continuation_overlap_minutes": 60,
        "freshness_requires_history_audit": True}


def _available_frame(history, as_of):
    frame = validate_candles(history.frame if hasattr(history, "frame") else history)
    cutoff = utc(as_of)
    frame = frame.loc[frame.available_at.le(cutoff) & (frame.opened_at + STEP).le(cutoff)].copy()
    if not {"session_open", "session_close"} <= set(frame):
        raise ContractError("Supervision requires explicit exchange session boundaries")
    return frame


def assessment_readiness(refresh, *, symbol, completed_session):
    """Prior-session assessment needs published coverage, not today's live bar.

    Advertised availability is not proof that every minute traded. Missing
    observations remain in the independent history audit and sample gates.
    """
    result = {"ready": False, "policy": "successful_refresh_through_completed_session_close",
              "session_id": completed_session["session_id"],
              "required_available_end": completed_session["closed_at"],
              "provider_available_end": None}
    requests = [row for row in refresh.get("requests", []) if row.get("symbol") == symbol]
    persisted = refresh.get("results", {}).get(symbol, {})
    if (refresh.get("status") not in {"complete", "partial"}
            or refresh.get("source_dataset") != "EQUS.MINI"
            or refresh.get("source_schema") != "ohlcv-1m" or len(requests) != 1
            or persisted.get("error_files") != 0):
        return {**result, "reason": "successful_scoped_refresh_required"}
    request = requests[0]
    result["provider_available_end"] = request.get("provider_available_end")
    if (request.get("status") != "fetched" or request.get("error_type") is not None
            or request.get("source_schema") != "ohlcv-1m"
            or not result["provider_available_end"]):
        return {**result, "reason": "successful_scoped_refresh_required"}
    if utc(result["provider_available_end"]) < utc(completed_session["closed_at"]):
        return {**result, "reason": "completed_session_not_yet_published"}
    return {**result, "ready": True, "reason": "completed_session_available"}


def _training_reference(frame):
    if frame.empty:
        return None
    eligible = frame.loc[frame.opened_at.add(3 * STEP).le(pd.to_datetime(frame.session_close, utc=True))]
    return eligible.opened_at.max() if len(eligible) else None


def audit_history(history, *, as_of):
    """Audit the most recent completed and active exchange sessions, with holes.

The existing XNAS calendar defines standard extended hours and conservative
regular hours on short sessions. A holiday or pre-open wake expects the previous
session's last candle, never a weekend/closed-session candle.
    """
    cutoff = utc(as_of)
    frame = _available_frame(history, cutoff)
    sessions = equity_sessions(cutoff - pd.Timedelta(days=21), cutoff + pd.Timedelta(days=1))
    past = [s for s in sessions if utc(s.closed_at) <= cutoff]
    completed = max(past, key=lambda s: utc(s.closed_at)) if past else None
    active = [s for s in sessions if utc(s.opened_at) <= cutoff < utc(s.closed_at)]
    selected = ([completed] if completed else []) + active
    observed = set(frame.opened_at)
    gaps, expected = [], []
    for session in selected:
        openings = pd.date_range(utc(session.opened_at), utc(session.closed_at) - STEP, freq=STEP)
        openings = openings[openings + STEP <= cutoff]
        missing = [t.isoformat() for t in openings if t not in observed]
        expected.extend(openings)
        gaps.append({"session_id": session.session_id, "expected_count": len(openings),
            "observed_count": len(openings) - len(missing), "count": len(missing),
            "missing_reference_at": missing})
    expected_reference = max(expected) if expected else None
    latest = frame.opened_at.max() if len(frame) else None
    reference = _training_reference(frame)
    terminal = int(frame.opened_at.gt(reference).sum()) if reference is not None else len(frame)
    manifest = getattr(history, "manifest", {})
    total_gaps = sum(g["count"] for g in gaps)
    return {"schema_version": "intraday-supervision-history-audit-v1",
        "as_of": cutoff.isoformat(), "source_dataset": manifest.get("source_dataset"),
        "freshness_ok": bool(expected_reference is not None and latest == expected_reference),
        "coverage_complete": total_gaps == 0,
        "latest_bar_fresh": bool(expected_reference is not None and latest == expected_reference),
        "expected_reference_at": expected_reference.isoformat() if expected_reference is not None else None,
        "latest_reference_at": latest.isoformat() if latest is not None else None,
        "completed_session": ({"session_id": completed.session_id, "opened_at": completed.opened_at,
            "closed_at": completed.closed_at} if completed else None),
        "active_session_ids": [s.session_id for s in active], "gaps": gaps,
        "missing_interval_count": total_gaps, "training_reference_at": reference.isoformat() if reference is not None else None,
        "terminal_reference_rows_excluded": terminal,
        "historical_incomplete_intervals": manifest.get("aggregation", {}).get("incomplete_intervals"),
        "availability_limitation": manifest.get("availability_limitation"),
        "missing_policy": "explicit_unresolved_no_fill_no_no_trade_inference"}


def build_supervision_dataset(history, *, as_of, completed_session=None, lookback_days=None):
    """Build exact-target training rows at the latest policy-eligible reference.

When the current session has a completed eligible candle, it admits all matured
prior-session labels. Before that candle exists the existing inference contract
requires a reference at least 45 minutes before session close, so terminal data
and the resulting omitted label count are explicitly reported. This helper
creates research data only; it issues no forecast or trading instruction.
    """
    cutoff = utc(as_of)
    frame = _available_frame(history, cutoff)
    reference = _training_reference(frame)
    if reference is None:
        raise InsufficientTrainingData("No completed session-eligible supervision reference")
    manifest = getattr(history, "manifest", {})
    frame.attrs["intraday_history"] = manifest
    dataset = build_dataset(frame, cutoff=cutoff, reference_at=reference,
                            lookback_days=lookback_days, synthetic_policy="exclude")
    session_id = (completed_session.get("session_id") if isinstance(completed_session, dict)
                  else completed_session)
    terminal = frame.loc[frame.opened_at.gt(reference)]
    # Count otherwise mature exact labels lost solely to inference truncation.
    lookup = frame.set_index("opened_at", drop=False)
    omitted = 0
    admitted = set(dataset.training_reference_at)
    for row in frame.itertuples():
        target = row.opened_at + 2 * STEP
        if row.opened_at in admitted or target not in lookup.index:
            continue
        outcome = lookup.loc[target]
        if lookback_days is not None and row.opened_at < reference - pd.Timedelta(days=lookback_days):
            continue
        if (row.opened_at + 3 * STEP <= cutoff and str(row.session_id) == str(outcome.session_id)
                and not row.synthetic and not outcome.synthetic and target > reference
                and (session_id is None or str(row.session_id) == str(session_id))):
            omitted += 1
    current_labels_excluded = 0
    if session_id is not None:
        declared_close = completed_session.get("closed_at") if isinstance(completed_session, dict) else None
        session_rows = frame.loc[frame.session_id.astype(str).eq(str(session_id))]
        if declared_close is None and session_rows.empty:
            raise ContractError("Requested completed supervision session has no observed history")
        completed_close = utc(declared_close or session_rows.iloc[-1].session_close)
        if completed_close > cutoff:
            raise ContractError("Supervision holdout session is not completed as of the cutoff")
        mask = (dataset.training_reference_at < completed_close) & (dataset.target_close_at <= completed_close)
        current_labels_excluded = int((~mask).sum())
        if not mask.any():
            raise InsufficientTrainingData("No matured training labels through the completed supervision session")
        dataset = replace(dataset, X=dataset.X[mask], y=dataset.y[mask],
            label_available_at=dataset.label_available_at[mask],
            training_reference_at=dataset.training_reference_at[mask],
            target_close_at=dataset.target_close_at[mask], manifest=dict(dataset.manifest))
        dataset.manifest.update(training_rows=len(dataset.y),
            label_available_at_max=dataset.label_available_at.max().isoformat(),
            first_training_reference=dataset.training_reference_at.min().isoformat(),
            last_training_reference=dataset.training_reference_at.max().isoformat())
    dataset.manifest.update(supervision_reference_at=reference.isoformat(),
        supervision_as_of=cutoff.isoformat(), completed_session_id=session_id,
        terminal_reference_rows_excluded=len(terminal), terminal_matured_labels_excluded=omitted,
        incomplete_session_labels_excluded=current_labels_excluded,
        selection_label_scope="completed_sessions_only" if session_id is not None else "all_matured_labels",
        terminal_label_limitation="Existing exact inference target may not cross session closure; omitted labels remain explicit")
    return dataset
