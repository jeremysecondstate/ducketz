from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ml.loop_c.weekly_scheduler_memory import (
    commit_memory,
    main,
    read_current_memory,
)


def _commit(
    root: Path,
    *,
    day: int,
    summary: str = "Weekly review completed",
) -> dict[str, object]:
    return commit_memory(
        root,
        wake_id=f"2026-08-{day:02d}T16:00:00Z",
        review_window=f"2026-08-{day - 5:02d}_to_2026-08-{day - 1:02d}",
        final_status="WEEKLY_OPERATOR_DISCUSSION_READY",
        incident_status="NONE",
        summary=summary,
        next_action="Wait for the next scheduled weekly review.",
        actions=("weekly review executed exactly once",),
        evidence_paths=(r"C:\DATASTORE\ml\loop-c-weekly-reviews\run\receipt.json",),
        created_at=datetime(2026, 8, day, 16, 1, tzinfo=timezone.utc),
    )


def test_empty_memory_is_valid_first_run_state(tmp_path: Path) -> None:
    result = read_current_memory(tmp_path)

    assert result["status"] == "EMPTY"
    normalized = result["pointer_path"].replace("\\", "/")
    assert normalized.endswith("loop-c/weekly-scheduler-memory/current.json")


def test_commits_checksum_verified_chain_and_advances_pointer(tmp_path: Path) -> None:
    first = _commit(tmp_path, day=22)
    second = _commit(tmp_path, day=29, summary="Second weekly review completed")
    current = read_current_memory(tmp_path)

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert current["status"] == "VALID"
    assert current["memory"]["sequence"] == 2
    assert current["memory"]["previous"] == {
        "sequence": 1,
        "receipt_path": Path(first["receipt_path"])
        .relative_to(Path(first["pointer_path"]).parent)
        .as_posix(),
        "receipt_sha256": first["receipt_sha256"],
    }
    pointer = json.loads(Path(second["pointer_path"]).read_text(encoding="utf-8"))
    receipt_bytes = Path(second["receipt_path"]).read_bytes()
    assert pointer["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert current["memory"]["safety"] == {
        "authority": "ADVISORY_MEMORY_ONLY",
        "automatic_change_allowed": False,
        "orders_enabled": False,
        "orders_placed": 0,
        "resume_requires_live_revalidation": True,
        "review_authority": (
            "verified weekly review receipts and explicit operator controls"
        ),
    }


def test_tampered_predecessor_invalidates_chain_and_blocks_extension(
    tmp_path: Path,
) -> None:
    first = _commit(tmp_path, day=22)
    _commit(tmp_path, day=29)
    Path(first["receipt_path"]).write_text("{}\n", encoding="utf-8")

    current = read_current_memory(tmp_path)

    assert current["status"] == "INVALID"
    assert "receipt 1 checksum" in current["error"]
    with pytest.raises(ValueError, match="invalid weekly scheduler memory chain"):
        _commit(tmp_path, day=30)


def test_duplicate_wake_commit_is_idempotent(tmp_path: Path) -> None:
    first = _commit(tmp_path, day=29)
    duplicate = _commit(tmp_path, day=29, summary="Duplicate summary")
    current = read_current_memory(tmp_path)

    assert duplicate["status"] == "ALREADY_COMMITTED"
    assert duplicate["sequence"] == 1
    assert duplicate["receipt_path"] == first["receipt_path"]
    assert current["memory"]["continuity"]["summary"] == "Weekly review completed"
    assert list(Path(first["receipt_path"]).parent.glob("*.json")) == [
        Path(first["receipt_path"])
    ]


def test_cli_commits_and_reads_compact_memory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "commit",
            "--root-dir",
            str(tmp_path),
            "--wake-id",
            "2026-08-29T16:00:00Z",
            "--review-window",
            "2026-08-24_to_2026-08-28",
            "--final-status",
            "INSUFFICIENT_LOOP_C_OBSERVATIONS",
            "--incident-status",
            "NONE",
            "--summary",
            "Review completed with insufficient observations.",
            "--next-action",
            "Wait for more observe-only evidence.",
            "--action",
            "weekly review executed exactly once",
            "--evidence",
            r"C:\DATASTORE\ml\loop-c-weekly-review-latest\review.json",
            "--compact",
        ]
    )
    committed = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert committed["status"] == "COMMITTED"
    assert main(["read", "--root-dir", str(tmp_path), "--compact"]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["status"] == "VALID"
    assert current["memory"]["continuity"]["actions"] == [
        "weekly review executed exactly once"
    ]
