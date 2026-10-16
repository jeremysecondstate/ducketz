"""Read-only, local 15-minute forecast diagnostics; never selects or fits models.

Run against an exact supervisor report. Output is restricted to this checkout's
ignored scratch directory. Only the local profile's symbols and actor are read.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3


COHORTS = ("timely_observed", "during_target_observed", "after_close_observed",
           "synthetic", "historical_reconstructed", "missing_outcome", "unknown")
MAX_ROWS_PER_SYMBOL = 10000
QUERY = """
SELECT f.id,f.pc,f.symbol,f.reference_at,f.issued_at,f.payload,o.payload
FROM forecasts AS f INDEXED BY forecasts_target
LEFT JOIN outcomes AS o ON o.rowid=(
 SELECT rowid FROM outcomes INDEXED BY outcomes_forecast
 WHERE forecast_id=f.id ORDER BY rowid DESC LIMIT 1)
WHERE f.pc=? AND f.symbol=? AND f.reference_at>=? AND f.reference_at<?
ORDER BY f.reference_at,f.id LIMIT ?
"""


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a timezone-aware ISO string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def object_json(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 1 else None


def metrics(rows):
    scored = [(row["label"], row["probability"]) for row in rows
              if row["label"] is not None and row["probability"] is not None]
    result = {"forecasts": len(rows), "scored": len(scored), "accuracy": None,
              "brier": None, "log_loss": None, "positive_label_fraction": None,
              "mean_probability": None, "causal_baseline": None,
              "baseline_limitation": "No per-forecast causal baseline was recorded; no retrospective baseline is fitted."}
    if scored:
        count = len(scored)
        result.update(accuracy=sum((p >= .5) == bool(y) for y, p in scored) / count,
                      brier=sum((p-y)**2 for y, p in scored) / count,
                      log_loss=-sum(math.log(max(1e-15, p if y else 1-p)) for y, p in scored) / count,
                      positive_label_fraction=sum(y for y, _ in scored) / count,
                      mean_probability=sum(p for _, p in scored) / count)
    return result


def input_coverage(forecast):
    provenance = forecast.get("reference_provenance")
    if not isinstance(provenance, dict):
        return "unknown"
    observed, synthetic = (provenance.get(key) for key in ("observed_constituents", "synthetic_constituents"))
    if (type(observed) is not int or type(synthetic) is not int or
            not 0 <= observed <= 15 or not 0 <= synthetic <= 15 or observed + synthetic != 15):
        return "unknown"
    return "zero_observed" if observed == 0 else "fully_observed" if observed == 15 else "partly_observed"


def period(reference, session):
    # A standard extended-hours policy is not an explicit regular-hours calendar.
    # Never assume 09:30--16:00 on holidays or shortened sessions.
    opened, closed = session.get("regular_opened_at"), session.get("regular_closed_at")
    if opened is None or closed is None:
        return "unknown"
    start, finish = instant(opened), instant(closed)
    if not instant(session["opened_at"]) <= start < finish <= instant(session["closed_at"]):
        raise ValueError("Regular-hours metadata escapes the declared session")
    return "regular" if start <= reference < finish else "extended"


def classify(forecast, outcome):
    if outcome is None or outcome.get("status") in {"missing", "pending"}:
        return "missing_outcome", None
    label = outcome.get("target_not_down")
    if type(label) not in (int, bool) or label not in (0, 1):
        return "unknown", None
    if outcome.get("status") == "synthetic":
        return "synthetic", int(label)
    if outcome.get("status") != "scored":
        return "unknown", None
    if outcome.get("availability_basis") in {"assumed_historical_close", "historical_reconstruction"}:
        return "historical_reconstructed", int(label)
    if outcome.get("availability_basis") != "actual" or forecast.get("availability_basis") != "observed":
        return "unknown", int(label)
    opened, closed, issued = (instant(forecast[key]) for key in ("target_open", "target_close", "issued_at"))
    if closed <= opened:
        raise ValueError("Target close must follow target open")
    for key in ("information_available_at", "training_completed_at"):
        if forecast.get(key) is not None and instant(forecast[key]) > issued:
            return "unknown", int(label)
    return ("timely_observed" if issued < opened else
            "during_target_observed" if issued < closed else "after_close_observed"), int(label)


def diagnose(datastore, profile_path, report_path):
    profile_raw, report_raw = Path(profile_path).read_bytes(), Path(report_path).read_bytes()
    profile, report = object_json(profile_raw), object_json(report_raw)
    actor, symbols = profile.get("actor"), profile.get("symbols")
    if (not isinstance(actor, str) or not actor or not isinstance(symbols, list) or not symbols
            or any(not isinstance(s, str) or not s for s in symbols) or len(set(symbols)) != len(symbols)):
        raise ValueError("Profile requires one actor and unique explicit local symbols")
    if report.get("actor") != actor:
        raise ValueError("Supervisor report actor differs from local profile")
    if not isinstance(report.get("symbols"), dict) or not isinstance(report.get("previous_session"), dict):
        raise ValueError("Supervisor report requires symbols and an exact previous session")
    session = report["previous_session"]
    start, finish = instant(session["opened_at"]), instant(session["closed_at"])
    if (not isinstance(session.get("session_id"), str) or not session["session_id"]
            or not start < finish <= start + timedelta(days=2)):
        raise ValueError("Invalid previous-session identity or bounds")
    period(start, session)
    selected = [s for s in symbols if s in report["symbols"]]
    if not selected:
        raise ValueError("Report contains no local-profile symbols")
    # ISO strings may carry different offsets. This bounded date envelope uses
    # the existing index; aware timestamps below enforce the exact interval.
    lower, upper = (start - timedelta(days=1)).date().isoformat(), (finish + timedelta(days=2)).date().isoformat()
    database = Path(datastore).resolve() / "intraday/shadow.sqlite3"
    if not database.is_file():
        raise ValueError("Local shadow database is missing")
    result = {"schema_version": "intraday-read-only-diagnostics-v1", "actor": actor,
              "created_at": datetime.now(timezone.utc).isoformat(), "status": "complete",
              "report_run_id": report.get("run_id"), "report_sha256": hashlib.sha256(report_raw).hexdigest(),
              "profile_sha256": hashlib.sha256(profile_raw).hexdigest(), "previous_session": session,
              "profile_symbols_absent_from_report": [s for s in symbols if s not in selected],
              "source_database": str(database), "symbols": {}, "errors": [],
              "policy": "diagnostics_only_no_selection_activation_or_operational_gate",
              "denominator": "Local saved live forecasts whose reference open is in the report's previous session; latest saved outcome revision only.",
              "timing_boundaries": "Before target open is timely; equality with open is during; equality with close is after-close.",
              "limitations": ["A high probability alone does not demonstrate overfitting, accuracy or profit.",
                  "Observed refers to target receipt provenance; input coverage is reported separately.",
                  "Synthetic not-down labels include unchanged prices; no fills or profitability are inferred.",
                  "Report training population and saved live fits can differ; missing synthetic labels do not prove every feature was observed.",
                  "No historical outcomes are reconstructed and no model or inspected holdout is changed.",
                  "Regular/extended hours remain unknown unless exact regular-session bounds are recorded."]}
    all_rows = []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        for symbol in selected:
            records = db.execute(QUERY, (actor, symbol, lower, upper, MAX_ROWS_PER_SYMBOL + 1)).fetchall()
            if len(records) > MAX_ROWS_PER_SYMBOL:
                raise ValueError(f"Bounded forecast query exceeded {MAX_ROWS_PER_SYMBOL} rows for {symbol}")
            rows, errors = [], []
            for key, pc, indexed_symbol, reference_at, issued_at, raw, outcome_raw in records:
                reference = instant(reference_at)
                if not start <= reference < finish:
                    continue
                try:
                    forecast = object_json(raw)
                    if any(forecast.get(field) != value for field, value in (
                            ("forecast_id", key), ("pc", pc), ("symbol", indexed_symbol),
                            ("reference_at", reference_at), ("issued_at", issued_at))):
                        raise ValueError("Forecast payload differs from indexed identity")
                    if forecast.get("mode") != "live":
                        continue
                    outcome = object_json(outcome_raw) if outcome_raw is not None else None
                    if outcome is not None and outcome.get("forecast_id") != key:
                        raise ValueError("Outcome belongs to a different forecast")
                    cohort, label = classify(forecast, outcome)
                    p = probability(forecast.get("probability_not_down"))
                    if p is None:
                        raise ValueError("Forecast probability is invalid")
                    rows.append({"forecast_id": key, "cohort": cohort, "label": label, "probability": p,
                                 "input_coverage": input_coverage(forecast), "session_period": period(reference, session)})
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({"forecast_id": key, "error": str(exc)})
                    rows.append({"forecast_id": key, "cohort": "unknown", "label": None, "probability": None,
                                 "input_coverage": "unknown", "session_period": "unknown"})
            entry = report["symbols"][symbol]
            if not isinstance(entry, dict) or not isinstance(entry.get("training_snapshot"), dict):
                raise ValueError(f"Report lacks training snapshot for {symbol}")
            snapshot = entry["training_snapshot"]
            training = {k: snapshot.get(k) for k in ("training_rows", "synthetic_labels_included", "synthetic_labels_excluded", "synthetic_policy")}
            if type(training["training_rows"]) is not int or training["training_rows"] < 0:
                raise ValueError(f"Report training-row count is invalid for {symbol}")
            for name in ("synthetic_labels_included", "synthetic_labels_excluded"):
                if training[name] is not None and (type(training[name]) is not int or training[name] < 0):
                    raise ValueError(f"Report {name} is invalid for {symbol}")
            if (training["synthetic_labels_included"] is not None
                    and training["synthetic_labels_included"] > training["training_rows"]):
                raise ValueError(f"Report synthetic-label count exceeds training rows for {symbol}")
            coverage = {kind: sum(r["input_coverage"] == kind for r in rows)
                        for kind in ("zero_observed", "partly_observed", "fully_observed", "unknown")}
            sparse_live = coverage["zero_observed"] + coverage["partly_observed"]
            support = ("not_established_no_synthetic_training_labels_with_synthetic_live_inputs"
                       if training["synthetic_labels_included"] == 0 and sparse_live
                       else "not_established_by_population_counts_alone")
            result["symbols"][symbol] = {"training_report": training, "training_live_support": support,
                "input_minute_coverage": coverage, "session_period": {kind: sum(r["session_period"] == kind for r in rows)
                    for kind in ("regular", "extended", "unknown")},
                "all_saved_only": metrics(rows), "cohorts": {kind: metrics([r for r in rows if r["cohort"] == kind]) for kind in COHORTS},
                "forecast_classifications": [{k: r[k] for k in ("forecast_id", "cohort", "input_coverage", "session_period")} for r in rows],
                "errors": errors}
            result["errors"].extend({"symbol": symbol, **error} for error in errors)
            all_rows.extend(rows)
        db.rollback()
    result["all_saved_only"] = metrics(all_rows)
    result["cohorts"] = {kind: metrics([r for r in all_rows if r["cohort"] == kind]) for kind in COHORTS}
    if result["errors"] or result["profile_symbols_absent_from_report"]:
        result["status"] = "incomplete"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("datastore", "profile", "report", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    scratch = Path(__file__).resolve().parents[1] / "scratch"
    output = args.output.resolve()
    if not output.is_relative_to(scratch) or output.suffix.lower() != ".json":
        parser.error("--output must be a new JSON file under this checkout's ignored scratch directory")
    if output.exists():
        parser.error("--output already exists; preserve previous evidence and choose a new path")
    try:
        result = diagnose(args.datastore, args.profile, args.report)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        result = {"schema_version": "intraday-read-only-diagnostics-v1", "status": "error",
                  "created_at": datetime.now(timezone.utc).isoformat(), "error": f"{type(exc).__name__}: {exc}"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "output": str(output)}))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
