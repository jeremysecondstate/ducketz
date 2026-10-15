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
SCOUT_RUN_NAME = re.compile(r"\d{8}T\d{12}Z\.json")
MAX_REPORT_BYTES = 10 * 1024 * 1024


def _is_scout(report):
    return report.get("schema") == "intraday-nightly-supervision-v1"


def _text(value, label, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"Malformed saved research {label}")


def _score(value, label):
    if value is not None and (not isinstance(value, (int, float))
                             or isinstance(value, bool) or not math.isfinite(value)):
        raise ValueError(f"Malformed saved research {label}")


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"Malformed saved research {label}")
    return value


def _reasons(value):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError("Malformed saved research reasons")


def _identity(report):
    return report.get("pc") if _is_scout(report) else report.get("actor")


def _dataset(report, item):
    # A refresh attempt alone cannot establish the dataset of a reused decision.
    return item.get("data", {}).get("source_dataset") if _is_scout(report) else report.get("source_dataset")


def _validate_report(report):
    scout = _is_scout(report)
    time_key = "as_of" if scout else "started_at"
    if time_key in report and not isinstance(report[time_key], str):
        raise ValueError("Malformed saved research start time")
    _text(report.get("status"), "status", optional=True)
    _text(report.get("source_dataset"), "source dataset", optional=True)
    for symbol, item in report["symbols"].items():
        _text(symbol, "symbol")
        if not isinstance(item, dict):
            raise ValueError("Malformed saved symbol research result")
        _text(item.get("status"), "symbol status", optional=True)
        if scout:
            data = _mapping(item.get("data", {}), "symbol data")
            _text(data.get("source_dataset"), "source dataset", optional=True)
            installation = _mapping(item.get("installation", {}), "installation")
            _text(installation.get("status"), "installation status", optional=True)
        selection = item.get("selection", {})
        if not isinstance(selection, dict):
            raise ValueError("Malformed saved model selection")
        _text(selection.get("status"), "selection status", optional=True)
        _text(selection.get("holdout_session"), "holdout session", optional=True)
        _reasons(selection.get("reasons", []))
        selected_key = "selected_specs" if scout else "selected_models"
        for key in ("candidates", selected_key, "model_catalog"):
            values = selection.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise ValueError(f"Malformed saved selection {key}")
        for candidate in selection.get("candidates", []):
            if not isinstance(candidate.get("spec", {}), dict):
                raise ValueError("Malformed saved candidate specification")
            if not isinstance(candidate.get("spec", {}).get("name"), str):
                raise ValueError("Malformed saved candidate name")
            _text(candidate.get("spec", {}).get("family"), "candidate family", optional=True)
            _text(candidate.get("status"), "candidate status", optional=True)
            _text(candidate.get("selection_reason"), "selection reason", optional=True)
            scores = (candidate.get("metrics") or {}) if scout else candidate.get("summary", {})
            if scout and candidate.get("metrics") is not None:
                _mapping(candidate["metrics"], "candidate scores")
            if not isinstance(scores, dict):
                raise ValueError("Malformed saved candidate scores")
            reasons = candidate.get("reasons" if scout else "rejection_reasons", [])
            if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
                raise ValueError("Malformed saved candidate rejection reasons")
            if not isinstance(candidate.get("error", {}), dict):
                raise ValueError("Malformed saved candidate error")
            _text(candidate.get("error", {}).get("message"), "candidate error message", optional=True)
            values = [scores.get("brier"), scores.get("balanced_accuracy")] if scout else scores.values()
            for value in [candidate.get("seconds"), *values]:
                if value is not None and (not isinstance(value, (int, float))
                                          or isinstance(value, bool) or not math.isfinite(value)):
                    raise ValueError("Nonfinite or malformed saved research score")
        for model in selection.get(selected_key, []):
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
        if scout:
            proposal = _mapping(holdout.get("proposal_metrics", {}), "holdout proposal metrics")
            _score(proposal.get("brier"), "holdout ensemble Brier")
            baselines = _mapping(selection.get("cv_baselines", {}), "validation baselines")
            prevalence = _mapping(baselines.get("training_prevalence", {}), "prevalence baseline")
            _score(prevalence.get("brier"), "prevalence Brier")
        coverage = selection.get("candidate_coverage")
        if coverage is not None:
            needed = ("attempted_variant_count", "configured_variant_count", "configured_family_count", "implemented_family_count")
            if (not isinstance(coverage, dict)
                    or any(type(coverage.get(k)) is not int or coverage[k] < 0 for k in needed)):
                raise ValueError("Malformed saved candidate coverage")
        for family in selection.get("model_catalog", []):
            if not isinstance(family.get("family"), str) or not isinstance(family.get("status"), str):
                raise ValueError("Malformed saved model catalog entry")
            if "configured" in family and type(family["configured"]) is not bool:
                raise ValueError("Malformed saved model catalog configuration")
            _text(family.get("nonselection_reason"), "catalog reason", optional=True)


def load_model_research(directory, *, actor):
    if directory is None or actor not in {"Atlas", "Scout"}:
        return {"status": "unavailable", "message": "Local model research identity is unavailable."}
    directory = Path(directory)
    if not directory.is_dir():
        return {"status": "missing", "message": "No saved nightly model research report."}
    # Scout's atomic checkpoint is authoritative, including incomplete runs.
    # Legacy Atlas reports use their original immutable run filenames.
    latest = directory / "latest.json"
    if actor == "Scout" and latest.exists():
        paths = [latest]
    elif actor == "Scout":
        paths = sorted((p for p in (directory / "runs").glob("*.json")
                        if SCOUT_RUN_NAME.fullmatch(p.name)), reverse=True)
    else:
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
        if (not isinstance(report, dict) or _identity(report) != actor
                or not (_is_scout(report) or report.get("schema_version") == "intraday-model-supervision-v1")
                or not isinstance(report.get("symbols"), dict)):
            raise ValueError("Saved model research report differs from this PC's research identity")
        _validate_report(report)
        return {"status": "loaded", "report": report}
    except OSError:
        return {"status": "error", "message": "Saved model research report could not be read."}
    except OverflowError:
        return {"status": "error", "message": "Malformed saved research numeric value."}
    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}


def research_choices(saved):
    """Expose only validated identities for research before any live forecast."""
    if saved.get("status") != "loaded":
        return {"pcs": (), "symbols": (), "datasets": ()}
    report = saved["report"]
    return {"pcs": (_identity(report),), "symbols": tuple(sorted(report["symbols"])),
            "datasets": tuple(sorted({source for item in report["symbols"].values()
                                      if (source := _dataset(report, item))}))}


def research_view(saved, *, pc, symbol, source_dataset=None):
    """Project saved scores and exclusions without inventing live participation."""
    if saved.get("status") != "loaded":
        return {"summary": saved.get("message", "No saved model research"), "rows": ()}
    report = saved["report"]
    scout = _is_scout(report)
    if pc != _identity(report):
        return {"summary": f"This report covers {_identity(report)}'s local model research.", "rows": ()}
    item = report["symbols"].get(symbol, {})
    dataset = _dataset(report, item)
    if source_dataset and source_dataset != dataset:
        return {"summary": "No local research report for the selected source dataset.", "rows": ()}
    if symbol not in report["symbols"]:
        return {"summary": "Choose a local symbol with saved model research.", "rows": ()}
    selection = item.get("selection", {})
    candidates = selection.get("candidates", [])
    winners = {spec.get("name") for spec in selection.get("selected_specs" if scout else "selected_models", [])}
    holdout = {model.get("name"): model.get("metrics", {})
               for model in (selection.get("holdout") or {}).get("models", [])}
    rows = []
    for candidate in candidates:
        spec = candidate.get("spec", {})
        scores = (candidate.get("metrics") or {}) if scout else candidate.get("summary", {})
        selected = spec.get("name") in winners
        reason = candidate.get("selection_reason")
        if reason is None:
            reason = ("selected" if selected else "; ".join(candidate.get("reasons" if scout else "rejection_reasons", []))
                      or (candidate.get("error") or {}).get("message")
                      or "Not selected by saved legacy policy")
        brier = scores.get("brier" if scout else "mean_brier")
        baseline = selection.get("cv_baselines", {}).get("training_prevalence", {}).get("brier") if scout else None
        advantage = (baseline - brier if baseline is not None and brier is not None else None) if scout else scores.get("mean_baseline_brier_improvement")
        rows.append({"family": spec.get("family", spec.get("name", "unknown")),
                     "name": spec.get("name", "unknown"), "status": candidate.get("status", "unknown"),
                     "selected": selected, "reason": reason,
                     "validation_brier": brier,
                     "validation_balanced_accuracy": scores.get("balanced_accuracy" if scout else "mean_balanced_accuracy"),
                     "baseline_improvement": advantage,
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
    elif scout:
        detail = f"{len(candidates)} saved candidate results · candidate coverage not recorded"
    else:
        detail = f"{len(candidates)} saved candidate results · report predates full-catalog coverage"
    try:
        started = datetime.fromisoformat(report["as_of" if scout else "started_at"].replace("Z", "+00:00"))
        when = started.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%b %d %H:%M %Z") if started.tzinfo else "unknown"
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        when = "unknown"
    summary = (f"{symbol} · {dataset or 'Unrecorded'} · research {when}"
               f" · {detail} · {len(winners)} selected for fresh fits."
               " These are research results; the consensus bars show only models fitted for that saved forecast.")
    if item.get("status") in {"error", "incumbent_retained", "no_selection"}:
        summary += f" Latest research status: {item['status']}; earlier live selections may remain in use."
    if scout:
        summary += (f" Latest assessment: {item.get('status', 'unrecorded')}; selection: {selection.get('status', 'unrecorded')};"
                    f" saved installation: {item.get('installation', {}).get('status', 'unrecorded')}."
                    " Selected specifications do not establish activation in a running trader; earlier selections may remain in use."
                    " Brier advantage compares validation with its saved training-prevalence baseline."
                    f" Final holdout session: {selection.get('holdout_session', 'unrecorded')}.")
        ensemble = (selection.get("holdout") or {}).get("proposal_metrics", {}).get("brier")
        if ensemble is not None:
            summary += f" Final holdout ensemble Brier: {ensemble:.3f}."
        if selection.get("reasons"):
            summary += " Selection reasons: " + "; ".join(selection["reasons"]) + "."
    return {"summary": summary, "rows": tuple(rows),
            "holdout_label": "Final holdout Brier" if scout else "Report-only holdout Brier"}
