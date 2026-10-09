"""Dated readiness notification ledger shared by existing native supervisors.

This helper does not send notices. A caller supplies fresh verified readiness,
claims a due event, and includes its event_id in the native final notice. A later
native read_thread outcome confirms delivery (or confirms that no notice was
published). Unknown outcomes remain pending; elapsed time never grants takeover.
Private readiness/outcome snapshots stay beside this local ledger, never in Git.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import uuid

from filelock import FileLock
import pandas as pd

VERSION = "nightly-notifications-v1"
THRESHOLDS = {"readiness-risk": (3, 0), "missed-confirmation": (3, 35)}


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _time(value):
    timestamp = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("Notification time must include its timezone")
    return timestamp.tz_convert("UTC")


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Notification evidence must be a regular file")
    if path.stat().st_size > 65536:
        raise ValueError("Notification evidence exceeds its bounded size")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Notification evidence exceeds its bounded size")
    return json.loads(raw), raw


def _immutable(path, value):
    raw = _bytes(value)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError("Immutable notification evidence differs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        # Publish without an overwrite window, including interrupted saves.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _root(config):
    if config.get("actor") not in {"Atlas", "Scout"}:
        raise ValueError("Notification actor differs")
    root = Path(config["state_root"]) / "notifications"
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Notification root may not be a link")
    return root


def _session(now):
    from ml.nightly_dispatch import intended_session
    return intended_session(now)["action_date"]


def _readiness(value, action, now):
    if (value.get("action_date") != action or type(value.get("confirmed")) is not bool
            or not isinstance(value.get("evidence"), str) or not value["evidence"].strip()):
        raise ValueError("Readiness must bind the intended date and exact verified evidence")
    if not isinstance(value.get("observed_at"), str):
        raise ValueError("Readiness observation time is required")
    observed = _time(value["observed_at"])
    if not now - pd.Timedelta(minutes=10) <= observed <= now:
        raise ValueError("Readiness observation is stale or from the future")


def _identity(actor, action, kind):
    return sha256(f"{VERSION}:{actor}:{action}:{kind}".encode()).hexdigest()


def _view(folder):
    event, _ = _read(folder / "event.json")
    claims = sorted((folder / "claims").glob("*.json"))
    if not claims:
        return {"event": event, "status": "UNCLAIMED"}
    claim, _ = _read(claims[-1])
    outcome_path = folder / "outcomes" / (claim["token"] + ".json")
    if not outcome_path.exists():
        return {"event": event, "claim": claim, "status": "PENDING_NATIVE_CONFIRMATION"}
    outcome, _ = _read(outcome_path)
    return {"event": event, "claim": claim, "outcome": outcome,
            "status": "DELIVERED" if outcome["notice_visible"] else "CONFIRMED_UNDELIVERED"}


def inspect(config, readiness, *, now=None):
    now = _time(now)
    action = _session(now)
    _readiness(readiness, action, now)
    root = _root(config)
    events = []
    with FileLock(str(root / "ledger.lock"), timeout=0):
        for kind, (hour, minute) in THRESHOLDS.items():
            event_id = _identity(config["actor"], action, kind)
            due = pd.Timestamp(action).tz_localize("America/Los_Angeles").replace(hour=hour, minute=minute)
            folder = root / event_id
            current = _view(folder) if (folder / "event.json").exists() else None
            if current:
                events.append({**current, "eligible": now >= due and not readiness["confirmed"]})
            elif now >= due and not readiness["confirmed"]:
                events.append({"status": "DUE", "eligible": True, "event": {
                    "schema_version": VERSION, "event_id": event_id, "actor": config["actor"],
                    "action_date": action, "kind": kind, "due_at": due.isoformat()}})
    return {"action_date": action, "observed_at": now.isoformat(), "events": events}


def claim(config, readiness, *, kind, owner, thread_id, now=None):
    if kind not in THRESHOLDS or not isinstance(owner, str) or not owner.strip() or not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Exact threshold, owner and native thread identity are required")
    now = _time(now)
    action = _session(now)
    _readiness(readiness, action, now)
    root = _root(config)
    event_id = _identity(config["actor"], action, kind)
    folder = root / event_id
    with FileLock(str(root / "ledger.lock"), timeout=0):
        hour, minute = THRESHOLDS[kind]
        due = pd.Timestamp(action).tz_localize("America/Los_Angeles").replace(hour=hour, minute=minute)
        if (folder / "event.json").exists():
            current = _view(folder)
            if current["status"] == "DELIVERED":
                return current
            if current["status"] == "PENDING_NATIVE_CONFIRMATION":
                same = current["claim"]["owner"] == owner and current["claim"]["thread_id"] == thread_id
                return {**current, "claimed": same, "eligible": now >= due and not readiness["confirmed"]}
        if now < due or readiness["confirmed"]:
            return {"status": "NOT_DUE", "action_date": action}
        event = {"schema_version": VERSION, "event_id": event_id, "actor": config["actor"],
                 "action_date": action, "kind": kind, "due_at": due.isoformat()}
        _immutable(folder / "event.json", event)
        count = len(list((folder / "claims").glob("*.json")))
        record = {"event_id": event_id, "owner": owner, "thread_id": thread_id,
                  "token": uuid.uuid4().hex, "claimed_at": now.isoformat(),
                  "readiness_sha256": sha256(_bytes(readiness)).hexdigest()}
        _immutable(folder / "readiness" / (record["readiness_sha256"] + ".json"), readiness)
        _immutable(folder / "claims" / f"{count + 1:08d}.json", record)
        return {**_view(folder), "claimed": True, "eligible": True}


def confirm(config, *, event_id, token, proof_path, now=None):
    """Persist explicit native outcome evidence; never guess delivery from a PID."""
    if not re.fullmatch(r"[0-9a-f]{64}", event_id) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid notification identity")
    proof, raw = _read(Path(proof_path))
    now = _time(now)
    root = _root(config)
    with FileLock(str(root / "ledger.lock"), timeout=0):
        folder = root / event_id
        current = _view(folder)
        claimed = current.get("claim", {})
        if (claimed.get("token") != token or proof.get("event_id") != event_id
                or proof.get("thread_id") != claimed.get("thread_id")
                or proof.get("owner") != claimed.get("owner")
                or proof.get("source") != "native_thread_readback"
                or proof.get("run_status") not in {"completed", "failed", "interrupted"}
                or type(proof.get("notice_visible")) is not bool
                or not isinstance(proof.get("readback_reference"), str) or not proof["readback_reference"].strip()):
            raise ValueError("Exact native terminal outcome and original claim are required")
        if not isinstance(proof.get("observed_at"), str):
            raise ValueError("Native outcome observation time is required")
        observed = _time(proof["observed_at"])
        if not _time(claimed["claimed_at"]) <= observed <= now:
            raise ValueError("Native outcome time differs from the retained claim")
        if not isinstance(proof.get("completed_at"), str):
            raise ValueError("Native terminal completion time is required")
        completed = _time(proof["completed_at"])
        if not _time(claimed["claimed_at"]) <= completed <= observed:
            raise ValueError("Native run has no verified terminal completion")
        outcome = {**proof, "proof_sha256": sha256(raw).hexdigest()}
        _immutable(folder / "outcomes" / (token + ".json"), outcome)
        return _view(folder)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--inspect", action="store_true")
    commands.add_argument("--claim", choices=THRESHOLDS)
    commands.add_argument("--confirm", action="store_true")
    parser.add_argument("--readiness", type=Path)
    parser.add_argument("--owner")
    parser.add_argument("--thread-id")
    parser.add_argument("--event-id")
    parser.add_argument("--token")
    parser.add_argument("--proof", type=Path)
    args = parser.parse_args(argv)
    config, _ = _read(args.config)
    if args.confirm:
        if not args.event_id or not args.token or not args.proof:
            parser.error("Confirm requires exact event ID/token and native readback proof")
        result = confirm(config, event_id=args.event_id, token=args.token, proof_path=args.proof)
    else:
        if not args.readiness:
            parser.error("Inspect/claim requires a fresh verified readiness observation")
        readiness, _ = _read(args.readiness)
        result = (inspect(config, readiness) if args.inspect else
                  claim(config, readiness, kind=args.claim, owner=args.owner, thread_id=args.thread_id))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
