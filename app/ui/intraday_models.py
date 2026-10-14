"""Read-only display of this PC's saved nightly candidate research.

No model, broker, acquisition or execution modules are imported. A researched
candidate is not an output of the displayed live forecast.
"""
from __future__ import annotations

import json
from datetime import datetime
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

REPORT_NAME = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{12}\.json")
MAX_REPORT_BYTES = 10 * 1024 * 1024


def _validate_report(report):
    if "started_at" in report and not isinstance(report["started_at"], str):
        raise ValueError("Malformed saved research start time")
    for item in report["symbols"].values():
        if not isinstance(item, dict):
            raise ValueError("Malformed saved symbol research result")
        selection = item.get("selection", {})
        if not isinstance(selection, dict):
            raise ValueError("Malformed saved model selection")
        for key in ("candidates", "selected_models", "model_catalog"):
            values = selection.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise ValueError(f"Malformed saved selection {key}")
        for candidate in selection.get("candidates", []):
            if not isinstance(candidate.get("spec", {}), dict):
                raise ValueError("Malformed saved candidate specification")
            if not isinstance(candidate.get("spec", {}).get("name"), str):
                raise ValueError("Malformed saved candidate name")
            scores = candidate.get("summary", {})
            if not isinstance(scores, dict):
                raise ValueError("Malformed saved candidate scores")
            reasons = candidate.get("rejection_reasons", [])
            if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
                raise ValueError("Malformed saved candidate rejection reasons")
            if not isinstance(candidate.get("error", {}), dict):
                raise ValueError("Malformed saved candidate error")
            for value in [candidate.get("seconds"), *scores.values()]:
                if value is not None and (not isinstance(value, (int, float))
                                          or isinstance(value, bool) or not math.isfinite(value)):
                    raise ValueError("Nonfinite or malformed saved research score")
        for model in selection.get("selected_models", []):
            if not isinstance(model.get("name"), str):
                raise ValueError("Malformed saved selected model name")
        holdout = selection.get("holdout")
        if holdout is None:
            holdout = {}
        if not isinstance(holdout, dict) or not isinstance(holdout.get("models", []), list):
            raise ValueError("Malformed saved holdout result")
        for model in holdout.get("models", []):
            if (not isinstance(model, dict) or not isinstance(model.get("name"), str)
                    or not isinstance(model.get("metrics", {}), dict)):
                raise ValueError("Malformed saved holdout model")
            brier = model.get("metrics", {}).get("brier")
            if brier is not None and (not isinstance(brier, (int, float))
                                       or isinstance(brier, bool) or not math.isfinite(brier)):
                raise ValueError("Malformed saved holdout Brier score")
        coverage = selection.get("candidate_coverage")
        if coverage is not None:
            needed = ("attempted_variant_count", "configured_variant_count", "configured_family_count", "implemented_family_count")
            if (not isinstance(coverage, dict)
                    or any(type(coverage.get(k)) is not int or coverage[k] < 0 for k in needed)):
                raise ValueError("Malformed saved candidate coverage")
        for family in selection.get("model_catalog", []):
            if not isinstance(family.get("family"), str) or not isinstance(family.get("status"), str):
                raise ValueError("Malformed saved model catalog entry")


def load_model_research(directory, *, actor):
    if directory is None or actor not in {"Atlas", "Scout"}:
        return {"status": "unavailable", "message": "Local model research identity is unavailable."}
    directory = Path(directory)
    if not directory.is_dir():
        return {"status": "missing", "message": "No saved nightly model research report."}
    paths = sorted((p for p in directory.glob("*.json") if REPORT_NAME.fullmatch(p.name)), reverse=True)
    if not paths:
        return {"status": "missing", "message": "No saved nightly model research report."}
    # Show a damaged newest report as an error rather than silently presenting
    # older work as the newest successful research.
    path = paths[0]
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("Saved model research report exceeds the display limit")
        report = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(report, dict) or report.get("actor") != actor
                or report.get("schema_version") != "intraday-model-supervision-v1"
                or not isinstance(report.get("symbols"), dict)):
            raise ValueError("Saved model research report differs from this PC's research identity")
        _validate_report(report)
        return {"status": "loaded", "report": report}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}


def research_view(saved, *, pc, symbol, source_dataset=None):
    """Project saved scores and exclusions without inventing live participation."""
    if saved.get("status") != "loaded":
        return {"summary": saved.get("message", "No saved model research"), "rows": ()}
    report = saved["report"]
    if pc != report["actor"]:
        return {"summary": f"This report covers {report['actor']}'s local model research.", "rows": ()}
    if source_dataset and source_dataset != report.get("source_dataset"):
        return {"summary": "No local research report for the selected source dataset.", "rows": ()}
    item = report["symbols"].get(symbol, {})
    selection = item.get("selection", {})
    candidates = selection.get("candidates", [])
    winners = {spec.get("name") for spec in selection.get("selected_models", [])}
    holdout = {model.get("name"): model.get("metrics", {})
               for model in (selection.get("holdout") or {}).get("models", [])}
    rows = []
    for candidate in candidates:
        spec, scores = candidate.get("spec", {}), candidate.get("summary", {})
        selected = spec.get("name") in winners
        reason = candidate.get("selection_reason")
        if reason is None:
            reason = ("selected" if selected else "; ".join(candidate.get("rejection_reasons", []))
                      or (candidate.get("error") or {}).get("message")
                      or "Not selected by saved legacy policy")
        rows.append({"family": spec.get("family", spec.get("name", "unknown")),
                     "name": spec.get("name", "unknown"), "status": candidate.get("status", "unknown"),
                     "selected": selected, "reason": reason,
                     "validation_brier": scores.get("mean_brier"),
                     "validation_balanced_accuracy": scores.get("mean_balanced_accuracy"),
                     "baseline_improvement": scores.get("mean_baseline_brier_improvement"),
                     "holdout_brier": holdout.get(spec.get("name"), {}).get("brier"),
                     "seconds": candidate.get("seconds")})
    for family in selection.get("model_catalog", []):
        if not family.get("configured"):
            rows.append({"family": family["family"], "name": "—", "status": family["status"],
                         "selected": False, "reason": family.get("nonselection_reason", "not_configured")})
    coverage = selection.get("candidate_coverage")
    if coverage:
        detail = (f"{coverage['attempted_variant_count']}/{coverage['configured_variant_count']} configurations attempted"
                  f" · {coverage['configured_family_count']}/{coverage['implemented_family_count']} implemented families in research")
    else:
        detail = f"{len(candidates)} saved candidate results · report predates full-catalog coverage"
    try:
        started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
        when = started.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%b %d %H:%M %Z") if started.tzinfo else "unknown"
    except (KeyError, TypeError, ValueError):
        when = "unknown"
    summary = (f"{symbol} · {report.get('source_dataset', 'Unrecorded')} · research {when}"
               f" · {detail} · {len(winners)} selected for fresh fits."
               " These are research results; the consensus bars show only models fitted for that saved forecast.")
    if item.get("status") in {"error", "incumbent_retained", "no_selection"}:
        summary += f" Latest research status: {item['status']}; earlier live selections may remain in use."
    return {"summary": summary, "rows": tuple(rows)}
