"""Bounded overnight 15-minute model review. Never starts a trader or sends orders."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from filelock import FileLock, Timeout
import numpy as np

from .model_policy import install_selection, load_active_models, write_json
from .prediction import ContractError, PACIFIC, TARGET_VERSION, digest, instant

SCHEMA = "intraday-nightly-supervision-v1"


def _insufficient_selection(session_id, now, reason):
    return {"status": "insufficient_data", "holdout_session": session_id,
            "as_of": now.isoformat(), "target_version": TARGET_VERSION,
            "holdout_policy": "intended_completed_session_only_no_older_substitution",
            "holdout_reuse_policy": "persist_and_reuse_this_report_for_session_do_not_retune_on_same_holdout",
            "selected_specs": [], "reasons": [reason], "holdout_rows": 0}


def scheduled_window(as_of):
    local = instant(as_of).astimezone(PACIFIC)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 60 <= minutes <= 210


def local_scope(repository, datastore):
    """Reuse the current host's research membership, without broker construction."""
    from .runtime import local_symbols
    repository = Path(repository)
    return local_symbols(repository / "scratch/cross-pc/local-profile.json",
                         repository / "datafetching/watchlist.local.txt", datastore)


def review_session(datastore, *, pc, symbol, session_id, as_of):
    """Read saved own-PC prediction and execution evidence in a consistent snapshot."""
    path = Path(datastore) / "intraday/shadow.sqlite3"
    result = dict(session_id=session_id, pc=pc, symbol=symbol, forecasts=0, scored=0,
                  missing_outcomes=0, late_forecasts=0, accuracy=None, brier=None,
                  extreme_probability_count=0, wrong_extreme_probability_count=0,
                  submitted_quantity=0, filled_quantity=0, submission_unknown=0,
                  realized_pnl=None, trade_result_basis="saved_execution_projections_only",
                  limitations=["Forecast correctness and reference returns are not trading profit.",
                               "No broker refresh; missing fill/P&L evidence remains unknown."])
    if not path.is_file():
        return {**result, "status": "no_saved_session"}
    probabilities, labels = [], []
    from .store import ShadowStore
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE pc=? AND symbol=?",
                          (pc, symbol)).fetchall()
        for row in rows:
            forecast = ShadowStore._forecast_value(row)
            if (forecast.get("mode") != "live" or forecast.get("target_version") != TARGET_VERSION
                    or instant(forecast["reference_at"]).astimezone(PACIFIC).date().isoformat() != session_id
                    or instant(forecast["issued_at"]) > instant(as_of)):
                continue
            result["forecasts"] += 1
            result["late_forecasts"] += instant(forecast["issued_at"]) >= instant(forecast["target_close"])
            p = float(forecast["probability_not_down"])
            result["extreme_probability_count"] += p <= .01 or p >= .99
            outcomes = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? ORDER BY rowid DESC",
                                  (forecast["forecast_id"],)).fetchall()
            outcome = next((ShadowStore._outcome_value(o) for o in outcomes if instant(o[2]) <= instant(as_of)), None)
            if outcome and outcome["status"] == "scored" and outcome["target_not_down"] is not None:
                y = int(outcome["target_not_down"])
                probabilities.append(p)
                labels.append(y)
                result["wrong_extreme_probability_count"] += (p <= .01 or p >= .99) and int(p >= .5) != y
            latest = db.execute("SELECT id,payload FROM intents WHERE forecast_id=? AND recorded_at<=? ORDER BY recorded_at DESC,rowid DESC LIMIT 1",
                                (forecast["forecast_id"], instant(as_of).isoformat())).fetchone()
            if latest:
                intent = json.loads(latest[1])
                if digest(intent) != latest[0]:
                    raise ContractError("Saved execution projection integrity changed")
                # These are latest cumulative values per forecast, not sums of revisions.
                result["submitted_quantity"] += intent.get("submitted_quantity") or 0
                result["filled_quantity"] += intent.get("filled_quantity") or 0
                result["submission_unknown"] += intent.get("submitted_quantity") is None
    result["scored"] = len(labels)
    result["missing_outcomes"] = result["forecasts"] - len(labels)
    if labels:
        p, y = np.asarray(probabilities), np.asarray(labels)
        result.update(accuracy=float(np.mean((p >= .5) == y)), brier=float(np.mean((p - y) ** 2)))
    result["status"] = "reviewed" if result["forecasts"] else "no_saved_session"
    return result


def supervise(*, repository, datastore, as_of=None, execute=False, force=False,
              refresh=None, loader=None, auditor=None, builder=None, evaluator=None,
              clock=time.monotonic, budget_seconds=24 * 60):
    """One own-host wake, checkpointed per session/symbol; safe offline by default.

    Repeated wakes refresh inputs and results. A final decision is frozen for a
    completed session even if later data revisions arrive, avoiding adaptive
    reuse of that session as another hyperparameter test.
    """
    now = instant(as_of or datetime.now(timezone.utc))
    pc, symbols = local_scope(repository, datastore)
    root = Path(datastore) / "intraday/supervision"
    report = dict(schema=SCHEMA, pc=pc, as_of=now.isoformat(), execute=execute,
                  schedule="Monday-Friday 01:00,01:30,02:00,02:30,03:00,03:30 America/Los_Angeles",
                  completion_scope="completed_session_assessment_only_not_live_input_readiness",
                  status="inspected", symbols={})
    if not execute:
        return {**report, "status": "dry_run", "local_symbols": list(symbols),
                "provider_calls": 0, "training_runs": 0, "policy_changes": 0}
    if not force and not scheduled_window(now):
        return {**report, "status": "outside_window"}
    root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(root / "supervisor.lock"), timeout=0):
            from .data import FEATURE_NAMES, InsufficientTrainingData, load_local_history
            from .model_selection import evaluate_candidates
            from .supervision_data import assessment_readiness, audit_history, build_supervision_dataset, refresh_training_data
            refresh = refresh or refresh_training_data
            loader = loader or load_local_history
            auditor = auditor or audit_history
            builder = builder or build_supervision_dataset
            evaluator = evaluator or evaluate_candidates
            started = clock()
            try:
                report["refresh"] = refresh(repository, datastore, symbols, as_of=now)
            except Exception as exc:
                report["refresh"] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
            for symbol in symbols:
                if clock() - started >= budget_seconds:
                    report["symbols"][symbol] = {"status": "deferred_budget", "resumes_next_wake": True}
                    continue
                try:
                    history = loader(datastore, symbol, source_dataset="EQUS.MINI")
                    audit = auditor(history, as_of=now)
                    session = audit["completed_session"]
                    if session is None:
                        raise ContractError("No completed trading session available")
                    sid = session["session_id"]
                    readiness = assessment_readiness(report["refresh"], symbol=symbol, completed_session=session)
                    item = {"data": audit, "assessment_readiness": readiness,
                            "current_inputs_fresh": bool(audit["freshness_ok"]),
                            "session_review": review_session(datastore, pc=pc, symbol=symbol,
                                session_id=sid, as_of=now)}
                    report["symbols"][symbol] = item
                    cache_path = root / "decisions" / pc / symbol / f"{sid}.json"
                    if cache_path.exists():
                        cached = json.loads(cache_path.read_text(encoding="utf-8"))
                        checksum = cached.pop("sha256")
                        if checksum != digest(cached) or (cached["pc"], cached["symbol"], cached["session_id"]) != (pc, symbol, sid):
                            raise ContractError("Frozen session decision changed")
                        selection = cached["selection"]
                        feature_names = cached["feature_names"]
                        item["decision_reused"] = True
                    elif not readiness["ready"]:
                        item.update(status="waiting_for_fresh_data", decision_reused=False)
                        continue
                    else:
                        selection = None
                        try:
                            dataset = builder(history, as_of=now, completed_session=session, lookback_days=None)
                            feature_names = list(dataset.feature_names)
                            dataset_manifest = dataset.manifest
                        except InsufficientTrainingData as exc:
                            feature_names = list(FEATURE_NAMES)
                            dataset_manifest = {"completed_session_id": sid,
                                                "selection_label_scope": "completed_sessions_only",
                                                "insufficient_evidence": str(exc)}
                            selection = _insufficient_selection(sid, now, str(exc))
                        incumbent = load_active_models(root, pc=pc, symbol=symbol, feature_names=feature_names)
                        if incumbent is None:
                            from .models import configured_models
                            incumbent = configured_models()
                        if selection is None:
                            intended_rows = sum(instant(value).astimezone(PACIFIC).date().isoformat() == sid
                                                for value in dataset.training_reference_at)
                            selection = (evaluator(dataset, as_of=now, incumbent_specs=incumbent) if intended_rows
                                         else _insufficient_selection(sid, now, "intended_completed_session_has_no_matured_labels"))
                        # Never accidentally evaluate an incomplete current session as the final holdout.
                        if str(selection.get("holdout_session")) != sid:
                            raise ContractError("Model assessment holdout differs from most recent completed session")
                        if selection.get("status") in {"failed", "budget_exhausted"}:
                            item.update(status="failed", selection=selection, resumes_next_wake=True)
                            continue
                        cached = dict(pc=pc, symbol=symbol, session_id=sid, selection=selection,
                                      feature_names=feature_names, dataset_manifest=dataset_manifest,
                                      decided_at=now.isoformat())
                        cached["sha256"] = digest(cached)
                        write_json(cache_path, cached)
                        item["decision_reused"] = False
                    item["selection"] = selection
                    # Install once. Recovery after decision persistence may finish its pending installation.
                    receipt_path = cache_path.with_suffix(".installation.json")
                    if receipt_path.exists():
                        item["installation"] = json.loads(receipt_path.read_text(encoding="utf-8"))
                    else:
                        item["installation"] = install_selection(root, pc=pc, symbol=symbol, report=selection,
                            feature_names=feature_names, installed_at=cached["decided_at"],
                            dataset_manifest=cached.get("dataset_manifest"))
                        write_json(receipt_path, item["installation"])
                    item["status"] = "complete" if readiness["ready"] else "waiting_for_fresh_data"
                except Exception as exc:
                    item = report["symbols"].setdefault(symbol, {})
                    item.update(status="failed", error_type=type(exc).__name__, error=str(exc), resumes_next_wake=True)
                # Preserve completed work even if a later symbol or process fails.
                write_json(root / "latest.json", report)
            report["status"] = "complete" if all(v.get("status") == "complete" for v in report["symbols"].values()) else "incomplete"
            run_name = now.strftime("%Y%m%dT%H%M%S%fZ")
            write_json(root / "runs" / f"{run_name}.json", report)
            write_json(root / "latest.json", report)
            return report
    except Timeout:
        return {**report, "status": "already_running"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--datastore", type=Path, default=Path("C:/DATASTORE"))
    parser.add_argument("--execute", action="store_true", help="Perform authorized data refresh, assessment and policy installation")
    parser.add_argument("--force", action="store_true", help="Explicit catch-up outside the normal nightly window")
    args = parser.parse_args(argv)
    report = supervise(repository=args.repository, datastore=args.datastore, execute=args.execute, force=args.force)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 1 if report["status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
