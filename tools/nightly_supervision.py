"""Read-first continuity for existing nightly repair and handoff owners.

This helper observes saved evidence and optionally records one bounded private
ledger. Decisions confer no repair, installation, takeover or execution authority.
It never dispatches work, calls a model/provider, or changes operating evidence.
Use it before daily/intake gates on the existing five-minute native task wake.
An active owner continues its retained repair; healthy workers keep running.
"""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re

import pandas as pd
from filelock import FileLock

from ml import nightly_workflow as workflow
from ml import nightly_repair_registry as registry
from ml.artifacts import file_checksum, utc_timestamp

VERSION = "nightly-supervision-v1"


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _read(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Supervision evidence cannot be a symbolic link")
    return workflow._json(path)


def _time(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("An actual zoned observation time is required")
    return result.tz_convert("UTC")


def _selection(action_date, now):
    from ml.nightly_dispatch import intended_session
    if action_date is None:
        return intended_session(now)
    if date.fromisoformat(action_date).isoformat() != action_date:
        raise ValueError("An exact ISO action date is required")
    # Midnight on a trading session has that session as the successor. This
    # validates an explicit date without pretending that it is the current time.
    midnight = pd.Timestamp(action_date).tz_localize("America/Los_Angeles")
    selected = intended_session(midnight)
    if selected["action_date"] != action_date:
        raise ValueError("Explicit action date is not an exchange session")
    return {**selected, "eligible": now >= _time(selected["kickoff_at"])}


def _worker_evidence(native, state, now):
    """A RUNNING label or assigned repair owner never establishes liveness."""
    from ml.overnight_runtime import overnight_status
    from datafetching.runtime_lock import _process_created_at
    entry = state.get("steps", {}).get(state.get("current_step"), {})
    run = entry.get("native_run")
    if not run:
        return None
    report = overnight_status(Path(native["datastore"]))
    if (report.get("status") != "RUNNING" or report.get("receipt_present") is not False
            or Path(report.get("run_path", "")).resolve() != Path(run).resolve()
            or report.get("owner_pid") != state.get("owner_pid")):
        return None
    pid, birth = report.get("owner_pid"), report.get("owner_created_at")
    if type(pid) is not int or pid <= 0 or type(birth) not in (int, float) or birth <= 0:
        return None
    observed = _time(report.get("heartbeat_at"))
    if not 0 <= (now - observed).total_seconds() <= 120 or _process_created_at(pid) != birth:
        return None
    return {"native_run": run, "owner_pid": pid, "owner_created_at": birth,
            "heartbeat_at": observed.isoformat(), "verified_at": now.isoformat()}


def _completed(native, exchange_config, action, review):
    from tools.nightly_exchange import _completed_result
    common = {"action_date": action, "review_session": review, "actor": native["actor"],
              "orders_placed": 0, "activation_changed": False, "execution_authorized": False,
              "joint_ready": False, "ui_ready": False, "peer_verified": False}
    return _completed_result(exchange_config, native, action, review, common)


def _repair_phase(native, exchange_config, state, owner):
    """Locate partial transactions without repairing or releasing any claim."""
    domain, action, repair_id = (owner[key] for key in ("domain", "action_date", "repair_id"))
    root = ((Path(native["state_root"]) / "runs") if domain == "preparation"
            else (Path(exchange_config["state_root"]) / "sessions"))
    directory = root / action / "source-repairs" / repair_id
    evidence = {}
    claim_path = directory / "claim.json"
    if not claim_path.exists():
        return "CLAIM_INCOMPLETE", 0, evidence
    claim = _read(claim_path)
    if domain == "preparation":
        from ml.nightly_stage_repair import _repository_claim
        saved = claim["claim"]
        expected = _repository_claim(saved["owner"], saved["repair_id"], claim["action_date"],
                                     saved["state_sha256"], owner["completion_record"])
    else:
        expected = {key: claim[key] for key in ("owner", "repair_id", "action_date")}
        expected.update(domain=domain, completion_record=owner["completion_record"], token=file_checksum(claim_path))
    if expected != owner:
        raise ValueError("Retained repair claim does not match the global owner")
    evidence[str(claim_path)] = file_checksum(claim_path)
    spec_path = directory / "spec.json"
    if not spec_path.exists():
        return "REPAIR_CLAIMED", 1, evidence
    spec = _read(spec_path)
    if spec.get("repository_claim") != owner:
        raise ValueError("Prepared repair specification differs from the retained owner")
    evidence[str(spec_path)] = file_checksum(spec_path)
    applied_path = directory / "applied.json"
    if not applied_path.exists():
        return "REPAIR_PREPARED", 2, evidence
    if domain == "preparation":
        from ml.nightly_stage_repair import verified_resume
        verified = verified_resume(native, owner, state)
    else:
        from ml.nightly_exchange_repair import verify_transition, original_binding, pending_claim
        verified = (pending_claim(exchange_config, action) is None and
                    verify_transition(exchange_config, state, original_binding(exchange_config, action))["verified"])
    if not verified:
        raise ValueError("Applied repair has not passed its existing continuation verifier")
    evidence[str(applied_path)] = file_checksum(applied_path)
    return "REPAIR_APPLIED", 3, evidence


def _partial_exchange_claim(config, action):
    from ml.nightly_exchange_repair import pending_claim
    return pending_claim(config, action) is not None


def _liveness(attestation, result, now, current_thread, *, recorded=False):
    """Validate an explicit agent attestation, not cryptographic native proof."""
    if attestation is None:
        return None
    for key in ("actor", "action_date", "incident_id", "automation_id"):
        expected = result[key]
        if attestation.get(key) != expected:
            raise ValueError("Owner liveness attestation identity differs")
    if attestation.get("repair_id") != (result.get("repository_owner") or {}).get("repair_id"):
        raise ValueError("Owner liveness refers to a different repair attempt")
    if not all(isinstance(attestation.get(key), str) and attestation[key].strip()
               for key in ("thread_id", "turn_id")):
        raise ValueError("Owner liveness needs exact native thread and turn identities")
    observed = _time(attestation.get("observed_at"))
    if not 0 <= (now - observed).total_seconds() <= 300:
        raise ValueError("Owner liveness attestation is future or stale")
    provenance = attestation.get("provenance")
    if provenance == "current_chat":
        if not recorded and (not current_thread or attestation["thread_id"] != current_thread):
            raise ValueError("Current-chat attestation differs from CODEX_THREAD_ID")
    elif provenance == "native_read_thread":
        proof = attestation.get("native_observation", {})
        if (proof.get("tool") != "read_thread" or proof.get("status") != "running"
                or proof.get("thread_id") != attestation["thread_id"]
                or proof.get("turn_id") != attestation["turn_id"]
                or proof.get("automation_id") != attestation["automation_id"]
                or _time(proof.get("observed_at")) != observed):
            raise ValueError("Native read_thread observation does not attest this active owner turn")
    else:
        raise ValueError("Unknown owner liveness provenance")
    return {**attestation, "evidence_kind": "owner_liveness_attestation", "retained_from_ledger": recorded}


def inspect(native, exchange_config, *, owner, action_date=None, now=None,
            owner_liveness_attestation=None, current_thread=None):
    """Return a next action; all operating mutations remain with native owners."""
    now = utc_timestamp(now)
    if not isinstance(owner, str) or not owner.strip() or native["actor"] != exchange_config["actor"]:
        raise ValueError("Explicit owner and matching local actor required")
    selected = _selection(action_date, now)
    retained = registry.read(native["state_root"])
    requested = selected["action_date"]
    if retained:
        selected = _selection(retained["action_date"], now)
    action, review = selected["action_date"], selected["source_session"]
    state = workflow.status(native, action_date=action)
    if state.get("status") != "NOT_STARTED" and state.get("source_session") != review:
        raise ValueError("Preparation source session differs from the selected action date")
    session = Path(exchange_config["state_root"]) / "sessions" / action
    status_path, failure_path = session / "status.json", session / "failure.json"
    exchange = _read(status_path) if status_path.exists() else {}
    for value in (exchange, _read(failure_path) if failure_path.exists() else {}):
        if value and (value.get("actor") != native["actor"] or value.get("action_date") != action):
            raise ValueError("Exchange evidence belongs to another actor or action date")
    failure = _read(failure_path) if failure_path.exists() else {}
    from ml.nightly_dispatch import TERMINAL, retry_disposition
    domain = retained["domain"] if retained else "exchange" if state["status"] in TERMINAL else "preparation"
    result = {"schema_version": VERSION, "actor": native["actor"], "action_date": action,
              "requested_action_date": requested, "review_session": review, "domain": domain,
              "automation_id": owner,
              "owner": retained["owner"] if retained else owner, "repository_owner": retained,
              "observed_at": now.isoformat(), "overall_complete": False, "mutation_authorized": False,
              "deadline_at": state.get("deadline_at", selected["deadline_at"]),
              "effective_deadline_at": state.get("effective_deadline_at", state.get("recovery_deadline_at")),
              "phase": "OBSERVED", "phase_rank": 0, "phase_evidence": {}, "completed_steps": {},
              "preparation_status": state["status"], "exchange_status": exchange.get("status", "NOT_STARTED"),
              "next_action": "INSPECT_FAILURE", "reason": "Saved evidence requires review"}
    result["incident_id"] = _digest({key: result[key] for key in ("actor", "action_date", "domain")})
    try:
        for step, entry in state.get("steps", {}).items():
            if entry.get("status") == "COMPLETE":
                workflow._verify_outputs(entry["output"])
                result["completed_steps"][step] = _digest(entry["output"])
        if retained:
            phase, rank, evidence = _repair_phase(native, exchange_config, state, retained)
            result.update(phase=phase, phase_rank=rank, phase_evidence=evidence,
                          next_action="CONTINUE_SAME_OWNER", reason="Resume the retained transaction before routine intake")
        elif state.get("repair_claim") or ((session / "repair-claim.json").exists() and
                                           _partial_exchange_claim(exchange_config, action)):
            result.update(next_action="RECONCILE_PARTIAL_CLAIM", reason="Preserve partial claim evidence; do not acquire a replacement")
        elif state["status"] not in TERMINAL:
            disposition = retry_disposition(native, state, now)
            if disposition:
                result.update(next_action="WAIT_BACKOFF" if disposition == "RETRY_BACKOFF" else "REPAIR_REQUIRED",
                              reason=disposition, failure=state.get("failure"))
            elif state["status"] == "RUNNING":
                live = _worker_evidence(native, state, now)
                result.update(next_action="WAIT_FOR_HEALTHY_WORKER" if live else "INSPECT_RUNNING_OWNER",
                              reason="Native owner liveness verified" if live else "Running label lacks fresh exact process/heartbeat proof",
                              worker_evidence=live)
            else:
                result.update(next_action="DISPATCH_PREPARATION" if selected["eligible"] else "WAIT_KICKOFF",
                              reason="Use the ordinary prerequisite-aware dispatcher within its fixed deadline")
        elif exchange.get("status") == "COMPLETE":
            verified = _completed(native, exchange_config, action, review)
            if (not verified or verified.get("status") != "COMPLETE" or verified.get("action_date") != action
                    or verified.get("review_session") != review or verified.get("actor") != native["actor"]
                    or any(verified.get(key) is not True for key in ("joint_ready", "ui_ready", "peer_verified"))):
                raise ValueError("Fresh intended-date combined Gameplan/Stats verification is incomplete")
            result.update(next_action="COMPLETE", overall_complete=True, phase="FINAL_VERIFIED", phase_rank=4,
                          final_verification={"observed_at": now.isoformat(), "result": verified},
                          reason="Exact dated combined publications and receipts freshly verified")
        elif failure:
            from ml.nightly_exchange_repair import dispatch_guard
            gate = dispatch_guard(exchange_config, action, now=now)
            result.update(next_action=("WAIT_BACKOFF" if gate and gate.get("reason") == "TRANSIENT_RETRY_COOLDOWN"
                                       else "REPAIR_REQUIRED" if gate else "RESUME_EXCHANGE"),
                          reason=gate["reason"] if gate else "Eligible ordinary exchange continuation", failure=failure)
        elif exchange.get("status") == "FAILED":
            result.update(next_action="REPAIR_REQUIRED", reason="Failed exchange lacks a usable failure record; inspect its preserved diagnostics")
        else:
            result.update(next_action="ADVANCE_EXCHANGE", reason=exchange.get("reason", "Local preparation alone is not overall completion"))
        # A retained applied repair remains owned until the ordinary dispatcher
        # verifies recovery and releases it; never bypass that final resolution.
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result.update(next_action="CONTINUE_SAME_OWNER" if retained else "REPAIR_REQUIRED",
                      reason="Saved evidence verification failed", verification_error=f"{type(error).__name__}: {error}")
    inherited_attestation = False
    if owner_liveness_attestation is None:
        ledger_path = Path(native["state_root"]) / "supervision" / (action + ".json")
        saved = _read(ledger_path) if ledger_path.exists() else {}
        if saved and (saved.get("schema_version") != VERSION or saved.get("actor") != result["actor"]
                      or saved.get("action_date") != action):
            raise ValueError("Saved supervision ledger identity differs")
        inherited = saved.get("owner_liveness_attestation")
        if inherited and all(inherited.get(key) == result[key] for key in
                             ("actor", "action_date", "incident_id", "automation_id")):
            age = (now - _time(inherited.get("observed_at"))).total_seconds()
            if age < 0:
                raise ValueError("Saved owner liveness observation is in the future")
            if age <= 300 and inherited.get("repair_id") == (retained or {}).get("repair_id"):
                # Keep its honest original provenance. A retained current-chat
                # attestation is not a new native read_thread observation.
                owner_liveness_attestation = inherited
                inherited_attestation = True
    attestation = _liveness(owner_liveness_attestation, result, now, current_thread, recorded=inherited_attestation)
    result["owner_liveness_attestation"] = attestation
    if attestation and result["next_action"] in {"CONTINUE_SAME_OWNER", "REPAIR_REQUIRED", "RECONCILE_PARTIAL_CLAIM"}:
        same_turn = attestation["thread_id"] == current_thread
        if not same_turn:
            result.update(resume_action=result["next_action"], next_action="WAIT_FOR_LIVE_OWNER",
                          reason="Fresh explicit native-owner attestation; retain the same owner and transaction")
    return result


def record(native, result, *, wake_id):
    """Idempotent bounded continuity only; no domain claim or timer is created."""
    if not isinstance(wake_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,160}", wake_id) is None:
        raise ValueError("Stable native wake/turn identity required")
    action = result["action_date"]
    if date.fromisoformat(action).isoformat() != action or result["actor"] != native["actor"]:
        raise ValueError("Ledger actor or date differs")
    root = Path(native["state_root"]) / "supervision"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (action + ".json")
    with FileLock(str(root / "ledger.lock"), timeout=0):
        previous = _read(path) if path.exists() else None
        now = _time(result["observed_at"])
        if previous and (previous.get("schema_version") != VERSION or previous.get("actor") != result["actor"]
                         or previous.get("action_date") != action or now < _time(previous["observed_at"])):
            raise ValueError("Ledger identity changed or observation moved backwards")
        progressed = False
        if previous:
            old_steps, new_steps = previous["completed_steps"], result["completed_steps"]
            progressed = (all(new_steps.get(key) == value for key, value in old_steps.items())
                          and len(new_steps) > len(old_steps))
            keys = ("owner", "repair_id", "action_date", "domain", "token")
            same_repair = all((previous.get("repository_owner") or {}).get(key) ==
                              (result.get("repository_owner") or {}).get(key) for key in keys)
            progressed |= (same_repair and previous["incident_id"] == result["incident_id"]
                           and result["phase_rank"] > previous["phase_rank"])
            progressed |= result["overall_complete"] and not previous["overall_complete"]
        changed_wake = previous is None or previous["last_wake_id"] != wake_id
        value = {**result, "first_seen_at": previous["first_seen_at"] if previous else now.isoformat(),
                 "last_progress_at": now.isoformat() if progressed or not previous else previous["last_progress_at"],
                 "last_wake_id": wake_id, "unchanged_wakes": (0 if progressed else
                     (previous["unchanged_wakes"] if previous else 0) + int(changed_wake)),
                 "progressed": progressed}
        workflow._write(path, value)
    return {"ledger": str(path), "ledger_sha256": file_checksum(path), **value}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--exchange-config", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--action-date")
    parser.add_argument("--owner-liveness-attestation", type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--inspect", action="store_true")
    modes.add_argument("--record", action="store_true")
    parser.add_argument("--wake-id")
    args = parser.parse_args(argv)
    if args.record != bool(args.wake_id):
        parser.error("--record requires --wake-id; inspect writes nothing")
    try:
        from tools.nightly_exchange import load_config
        for path in (args.config, args.exchange_config):
            if not path.is_absolute():
                raise ValueError("Explicit absolute configuration paths required")
        native = workflow.load_config(args.config)
        workflow.verify_installation(native)
        exchange_config = load_config(args.exchange_config)
        if Path(exchange_config["workflow_config"]).resolve() != args.config.resolve():
            raise ValueError("Exchange references another workflow configuration")
        result = inspect(native, exchange_config, owner=args.owner, action_date=args.action_date,
                         owner_liveness_attestation=_read(args.owner_liveness_attestation) if args.owner_liveness_attestation else None,
                         current_thread=os.environ.get("CODEX_THREAD_ID"))
        if args.record:
            result = record(native, result, wake_id=args.wake_id)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"schema_version": VERSION, "overall_complete": False, "mutation_authorized": False,
                          "next_action": "REPAIR_REQUIRED", "error": f"{type(error).__name__}: {error}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
