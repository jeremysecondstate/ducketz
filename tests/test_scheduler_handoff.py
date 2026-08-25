from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ml.scheduler_handoff import commit_handoff, main, read_current_handoff


def _commit(root: Path, *, hour: int, summary: str = "Healthy wake") -> dict[str, object]:
    return commit_handoff(
        root,
        wake_id=f"2026-08-25T{hour:02d}:42:00Z",
        monitor_mode="hourly",
        lane="STANDARD_OPERATIONS",
        stage_id="NONE",
        eligible_session="2026-08-24",
        checked_at=f"2026-08-25T{hour:02d}:42:00Z",
        final_status="HEALTHY",
        stage_disposition="NOT_APPLICABLE",
        incident_status="NONE",
        summary=summary,
        next_action="Run the next scheduled guardian baseline.",
        actions=("guardian executed exactly once",),
        evidence_paths=(r"C:\DATASTORE\ml\latest\run.json",),
        created_at=datetime(2026, 8, 25, hour, 43, tzinfo=timezone.utc),
    )


def test_empty_handoff_is_valid_first_run_state(tmp_path: Path) -> None:
    result = read_current_handoff(tmp_path)

    assert result["status"] == "EMPTY"
    assert result["pointer_path"].endswith("scheduler-handoff\\current.json") or result[
        "pointer_path"
    ].endswith("scheduler-handoff/current.json")


def test_commits_checksum_verified_chain_and_advances_pointer(tmp_path: Path) -> None:
    first = _commit(tmp_path, hour=18)
    second = _commit(tmp_path, hour=19, summary="Second healthy wake")
    current = read_current_handoff(tmp_path)

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert current["status"] == "VALID"
    assert current["handoff"]["sequence"] == 2
    assert current["handoff"]["previous"] == {
        "sequence": 1,
        "receipt_path": first["receipt_path"],
        "receipt_sha256": first["receipt_sha256"],
    }
    pointer = json.loads(Path(second["pointer_path"]).read_text(encoding="utf-8"))
    receipt_bytes = Path(second["receipt_path"]).read_bytes()
    assert pointer["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert current["handoff"]["safety"] == {
        "authority": "ADVISORY_HANDOFF_ONLY",
        "orders_placed": 0,
        "resume_requires_live_revalidation": True,
        "routing_authority": "guardian schedule metadata and verified receipts",
    }


def test_tampered_current_receipt_blocks_chain_extension(tmp_path: Path) -> None:
    committed = _commit(tmp_path, hour=18)
    receipt_path = Path(committed["receipt_path"])
    receipt_path.write_text("{}\n", encoding="utf-8")

    current = read_current_handoff(tmp_path)

    assert current["status"] == "INVALID"
    assert "checksum" in current["error"]
    with pytest.raises(ValueError, match="invalid handoff chain"):
        _commit(tmp_path, hour=19)


def test_tampered_predecessor_invalidates_current_chain(tmp_path: Path) -> None:
    first = _commit(tmp_path, hour=18)
    _commit(tmp_path, hour=19)
    Path(first["receipt_path"]).write_text("{}\n", encoding="utf-8")

    current = read_current_handoff(tmp_path)

    assert current["status"] == "INVALID"
    assert "receipt 1 checksum" in current["error"]


def test_cli_commits_and_reads_compact_handoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "commit",
            "--root-dir",
            str(tmp_path),
            "--wake-id",
            "2026-08-25T20:42:00Z",
            "--monitor-mode",
            "hourly",
            "--lane",
            "STANDARD_OPERATIONS",
            "--stage-id",
            "NONE",
            "--eligible-session",
            "2026-08-24",
            "--checked-at",
            "2026-08-25T20:42:00Z",
            "--final-status",
            "HEALTHY",
            "--stage-disposition",
            "NOT_APPLICABLE",
            "--incident-status",
            "NONE",
            "--summary",
            "Healthy wake",
            "--next-action",
            "Run the next scheduled guardian baseline.",
            "--action",
            "guardian executed exactly once",
            "--evidence",
            r"C:\DATASTORE\ml\latest\run.json",
            "--compact",
        ]
    )
    committed = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert committed["status"] == "COMMITTED"
    assert main(["read", "--root-dir", str(tmp_path), "--compact"]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["status"] == "VALID"
    assert current["handoff"]["continuity"]["actions"] == [
        "guardian executed exactly once"
    ]


def test_duplicate_wake_commit_is_idempotent(tmp_path: Path) -> None:
    first = _commit(tmp_path, hour=18)
    duplicate = _commit(tmp_path, hour=18, summary="Late duplicate summary")
    current = read_current_handoff(tmp_path)

    assert duplicate == {
        "schema_version": "loops-hourly-scheduler-handoff-pointer-v1",
        "status": "ALREADY_COMMITTED",
        "wake_id": "2026-08-25T18:42:00Z",
        "sequence": 1,
        "pointer_path": first["pointer_path"],
        "receipt_path": first["receipt_path"],
        "receipt_sha256": first["receipt_sha256"],
    }
    assert current["handoff"]["continuity"]["summary"] == "Healthy wake"
    assert list(Path(first["receipt_path"]).parent.glob("*.json")) == [
        Path(first["receipt_path"])
    ]
