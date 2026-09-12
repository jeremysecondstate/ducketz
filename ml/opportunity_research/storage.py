"""Immutable snapshots and atomic, hash-chained weekly publications."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from filelock import FileLock

from .policy import VERSION


def encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ResearchStore:
    def __init__(self, datastore: Path):
        self.root = Path(datastore).resolve() / "research" / "opportunities"

    def snapshot(self, payload: dict) -> dict:
        data = encode(payload)
        sha = digest(data)
        path = self.root / "snapshots" / f"{sha}.json"
        if path.exists():
            if digest(path.read_bytes()) != sha:
                raise ValueError("Snapshot checksum mismatch")
        else:
            atomic_write(path, data)
        return {"path": path.relative_to(self.root).as_posix(), "sha256": sha}

    def read_snapshot(self, reference: dict) -> dict:
        relative = reference["path"]
        if not re.fullmatch(r"snapshots/[0-9a-f]{64}\.json", relative):
            raise ValueError("Invalid research snapshot path")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Snapshot path leaves research directory")
        data = path.read_bytes()
        if digest(data) != reference["sha256"] or path.stem != reference["sha256"]:
            raise ValueError("Snapshot checksum mismatch")
        return json.loads(data)

    def history(self) -> list[dict]:
        parent = self.root / "editions"
        index_path = self.root / "latest.json"
        index = json.loads(index_path.read_bytes()) if index_path.exists() else {}
        if not parent.exists():
            if index.get("edition_count", 0):
                raise ValueError("Previously published research editions are missing")
            return []
        publications = []
        previous = None
        for directory in sorted(parent.iterdir()):
            if not directory.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", directory.name):
                raise ValueError("Unexpected object in immutable research editions")
            payload_bytes = (directory / "publication.json").read_bytes()
            payload = json.loads(payload_bytes)
            receipt = json.loads((directory / "receipt.json").read_bytes())
            if (receipt.get("publication_sha256") != digest(payload_bytes)
                    or receipt.get("report_sha256") != digest((directory / "report.md").read_bytes())
                    or payload.get("previous_publication_sha256") != previous
                    or payload.get("policy_version") != VERSION
                    or payload.get("week") != directory.name):
                raise ValueError(f"Research edition failed verification: {directory.name}")
            for reference in payload["snapshot_refs"]:
                self.read_snapshot(reference)
            previous = digest(payload_bytes)
            publications.append({**payload, "publication_sha256": previous,
                                 "report_path": str(directory / "report.md")})
        if index.get("edition_count", 0) > len(publications) or (
                index.get("week") and (not publications or index["week"] > publications[-1]["week"])):
            raise ValueError("Previously published research editions are missing")
        return publications

    def calls(self, history: list[dict] | None = None) -> list[dict]:
        calls = {}
        for edition in self.history() if history is None else history:
            for call in edition["new_calls"]:
                identity = call["security_id"]
                if identity in calls:
                    raise ValueError("A recommendation inception was reset")
                calls[identity] = call
        return list(calls.values())

    def frozen(self, call_id: str, history: list[dict]) -> dict:
        outcomes = {}
        for edition in history:
            for result in edition["tracking"]:
                if result["call_id"] != call_id:
                    continue
                for horizon in ("6m", "12m"):
                    value = result["windows"][horizon]
                    if value["status"] == "EVALUATED":
                        if horizon in outcomes and outcomes[horizon] != value:
                            raise ValueError("A matured research outcome changed")
                        outcomes[horizon] = value
        return outcomes

    def publish(self, payload: dict, report: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / ".publication.lock"), timeout=0):
            history = self.history()
            existing = next((e for e in history if e["week"] == payload["week"]), None)
            if existing:
                if existing["input_sha256"] != payload["input_sha256"]:
                    raise ValueError("This week is already published; preserve its original record")
                self.export_ledger(history)
                return Path(existing["report_path"])
            expected = history[-1]["publication_sha256"] if history else None
            if payload["previous_publication_sha256"] != expected:
                raise ValueError("Research history advanced; prepare and validate again")
            if history and payload["week"] <= history[-1]["week"]:
                raise ValueError("Research editions must advance chronologically")
            stage = self.root / "staging" / uuid.uuid4().hex
            stage.mkdir(parents=True)
            publication = encode(payload)
            report_bytes = report.encode("utf-8")
            atomic_write(stage / "publication.json", publication)
            atomic_write(stage / "report.md", report_bytes)
            atomic_write(stage / "receipt.json", encode({
                "publication_sha256": digest(publication), "report_sha256": digest(report_bytes)}))
            final = self.root / "editions" / payload["week"]
            final.parent.mkdir(parents=True, exist_ok=True)
            # Atomic commit; no publication pointer or mutable database can orphan a call.
            stage.rename(final)
            committed = self.history()
            self.export_ledger(committed)
            return final / "report.md"

    def export_ledger(self, history: list[dict] | None = None) -> None:
        history = self.history() if history is None else history
        calls = self.calls(history)
        updates = [dict(update, week=e["week"]) for e in history for update in e["updates"]]
        atomic_write(self.root / "recommendations.json", encode({"policy_version": VERSION,
                     "calls": calls, "updates": updates}))
        atomic_write(self.root / "latest.json", encode({
            "week": history[-1]["week"] if history else None,
            "report_path": history[-1]["report_path"] if history else None,
            "recommendation_count": len(calls), "edition_count": len(history)}))
