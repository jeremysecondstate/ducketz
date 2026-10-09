"""Deterministic responsibility routing and immutable missed-night authority.

No providers, account adapters or model clients are imported here. Scheduling
chooses an owner; the existing workflow and native runtime still own execution.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp

LAYOUT = "nightly-responsibilities-v1"
RECOVERY = "scheduled-nightly-recovery-v1"
STEPS = ("datastore_catchup", "prepare_stats", "model_review", "train_and_plan",
         "local_gameplan", "verify_display", "local_handoff")
OWNERS = {"datastore_catchup": "datastore", "prepare_stats": "stats",
          "model_review": "model", "train_and_plan": "model",
          "local_gameplan": "gameplan", "verify_display": "display",
          "local_handoff": "display"}
RESPONSIBILITIES = tuple(dict.fromkeys(OWNERS.values()))
TERMINAL = {"LOCAL_COMPLETE_PEER_SETUP_PENDING", "COMPLETE", "HANDOFF_VERIFIED_LOCAL"}


def intended_session(now=None):
    from ml.gameplan_actuals_review import completed_session_context
    now = utc_timestamp(now)
    context = completed_session_context(now)
    action = context["successor_action_date"]
    original = pd.Timestamp(action).tz_localize("America/Los_Angeles").replace(hour=4).tz_convert("UTC")
    kickoff = pd.Timestamp(context["action_date"]).tz_localize("America/Los_Angeles").replace(hour=21, minute=5).tz_convert("UTC")
    return {"action_date": action, "source_session": context["action_date"],
            "deadline_at": original.isoformat(), "kickoff_at": kickoff.isoformat(),
            "eligible": now >= kickoff}


def next_responsibility(state):
    sequence = STEPS if state.get("workflow_layout") == LAYOUT else (
        "prepare_stats", "model_review", "train_and_plan", "verify_display", "local_handoff")
    for step in sequence:
        if state.get("steps", {}).get(step, {}).get("status") != "COMPLETE":
            # A pre-upgrade combined fetch/Stats segment keeps its original owner.
            return ("datastore" if step == "prepare_stats" and state.get("workflow_layout") != LAYOUT
                    else OWNERS[step])
    # Recover a crash after the last stage save but before terminal state save.
    return "display" if state.get("status") not in TERMINAL else None


def authority(config):
    policy = config.get("automatic_recovery", {})
    if (policy.get("enabled") is not True or not isinstance(policy.get("authorization"), str)
            or not policy["authorization"].strip()):
        raise ValueError("Automatic recovery needs recorded local human authority")
    attempts = policy.get("max_attempts", 3)
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("Automatic retries must be bounded to one through three attempts")
    return policy


def recovery_record(config, state, now):
    """Freeze one recovery expiry; callers write the record once under workflow.lock."""
    policy = authority(config)
    now = utc_timestamp(now)
    original = utc_timestamp(state["deadline_at"])
    local = now.tz_convert("America/Los_Angeles")
    expiry = min(now + pd.Timedelta(hours=7), local.normalize().replace(hour=17).tz_convert("UTC"))
    if (state["action_date"] != local.date().isoformat() or not 4 <= local.hour < 17
            or original > now):
        raise ValueError("Missed-night recovery is limited to its exact current action session before 17:00 Pacific")
    attempts = {}
    for entry in state.get("steps", {}).values():
        if entry.get("native_run"):
            run = Path(entry["native_run"])
            receipt = run / "receipt.json"
            if receipt.exists():
                attempts[str(run.resolve())] = file_checksum(receipt)
    return {"schema_version": RECOVERY, "actor": state["actor"], "workflow_run_id": state["run_id"],
            "source_identity": state["source_identity"], "datastore": str(Path(config["datastore"]).resolve()),
            "action_date": state["action_date"], "source_session": state["source_session"],
            "original_deadline_at": original.isoformat(), "approved_at": now.isoformat(),
            "expires_at": expiry.isoformat(), "authorization": policy["authorization"],
            "orders_authorized": False, "native_attempts": attempts}


def validate_recovery(path, *, root, report=None, run=None, now=None):
    path = Path(path).resolve()
    record = json.loads(path.read_text(encoding="utf-8"))
    now = utc_timestamp(now)
    action = record.get("action_date")
    original = pd.Timestamp(action).tz_localize("America/Los_Angeles").replace(hour=4).tz_convert("UTC")
    close = original + pd.Timedelta(hours=13)
    approved, expiry = utc_timestamp(record.get("approved_at")), utc_timestamp(record.get("expires_at"))
    from ml.gameplan_actuals_review import completed_session_context
    session = completed_session_context(approved)
    if (record.get("schema_version") != RECOVERY or record.get("actor") not in {"Atlas", "Scout"}
            or not record.get("workflow_run_id") or record.get("orders_authorized") is not False
            or not str(record.get("authorization", "")).strip()
            or Path(record.get("datastore", "")).resolve() != Path(root).resolve()
            or session["successor_action_date"] != action or session["action_date"] != record.get("source_session")
            or utc_timestamp(record.get("original_deadline_at")) != original
            or not original <= approved <= now < expiry <= min(close, approved + pd.Timedelta(hours=7))):
        raise ValueError("Frozen nightly recovery is invalid or expired")
    evidence = {"path": str(path), "sha256": file_checksum(path)}
    if report is not None:
        inherited = report.get("workflow_recovery")
        if (report.get("stats_first") is not True or report.get("stock_only") is not True
                or report.get("review_action_date") != record["source_session"]
                or utc_timestamp(report["deadline_at"]) != original
                or (inherited != evidence and record.get("native_attempts", {}).get(str(Path(run).resolve()))
                    != file_checksum(Path(run) / "receipt.json"))):
            raise ValueError("Frozen recovery differs from the exact original native attempt")
    return record, evidence


def failure_record(error, *, step, owner, now, state):
    """Conservative classification: unknown deterministic errors need one repair."""
    message = f"{type(error).__name__}: {error}"
    entry = state.get("steps", {}).get(step, {})
    detail = ""
    if entry.get("native_run"):
        run = Path(entry["native_run"])
        report = run / "stage-report.json"
        if report.is_file():
            report = json.loads(report.read_text(encoding="utf-8"))
            failed_stage = report.get("failed_stage") or report.get("current_stage")
            if isinstance(failed_stage, str) and Path(failed_stage).name == failed_stage:
                log = run / (failed_stage + ".log")
                if log.is_file():
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 8192))
                        detail = stream.read().decode("utf-8", errors="replace")
    elif entry.get("feedback_run"):
        logs = sorted(Path(entry["feedback_run"]).glob("codex-answer-*.jsonl"), key=lambda path: path.stat().st_mtime)
        if logs:
            with logs[-1].open("rb") as stream:
                stream.seek(max(0, logs[-1].stat().st_size - 8192))
                detail = stream.read().decode("utf-8", errors="replace")
    text = (message + "\n" + detail).lower()
    transient = isinstance(error, (ConnectionError, TimeoutError)) or any(term in text for term in (
        "temporarily unavailable", "connection reset", "connection aborted", "rate limit", "timed out"))
    external = any(term in text for term in ("unauthorized", "authentication", "credentials", "quota", "not found",
        "usage limit", "usage exceeded", "cli is unavailable"))
    kind = "EXTERNAL_DEPENDENCY" if external else "TRANSIENT" if transient else "SOURCE_DEFECT"
    fingerprint = sha256((str(step) + "\n" + message).encode()).hexdigest()
    return {"at": utc_timestamp(now).isoformat(), "step": step, "owner": owner,
            "kind": kind, "error": message, "fingerprint": fingerprint,
            "local_diagnostic_tail": detail,
            "retry_after": (utc_timestamp(now) + pd.Timedelta(minutes=5)).isoformat(),
            "source_identity": state["source_identity"], "disposition": "OPEN",
            "corrective_action": ("Retry the same unfinished stage within its fixed deadline" if transient
                                  else "Inspect evidence, repair or restore the dependency, verify, and resume this stage")}


def retry_disposition(config, state, now=None):
    failure = state.get("failure")
    if not failure or failure.get("disposition") == "RESOLVED":
        return None
    entry = state.get("steps", {}).get(failure.get("step"), {})
    limit = authority(config).get("max_attempts", 3)
    attempts, epoch_start = entry.get("attempts", 0), entry.get("retry_epoch_attempt_start", 0)
    epoch_id = entry.get("retry_epoch_id")
    if (type(attempts) is not int or type(epoch_start) is not int
            or not 0 <= epoch_start <= attempts
            or (epoch_start and epoch_id is None)
            or (epoch_id is not None and (not isinstance(epoch_id, str) or len(epoch_id) != 64
                or any(character not in "0123456789abcdef" for character in epoch_id)))):
        raise ValueError("Invalid audited retry epoch; preserve its cumulative attempt evidence")
    if failure["kind"] != "TRANSIENT" or attempts - epoch_start >= limit:
        return "REPAIR_REQUIRED" if failure["kind"] == "SOURCE_DEFECT" else "DEPENDENCY_REQUIRED" if failure["kind"] == "EXTERNAL_DEPENDENCY" else "RETRY_LIMIT_REACHED"
    if failure.get("retry_after") and utc_timestamp(now) < utc_timestamp(failure["retry_after"]):
        return "RETRY_BACKOFF"
    return None
