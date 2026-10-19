"""Offline receipt policy metadata in the optional nightly synthesis lineage."""
from pathlib import Path

from ml.gameplan_execution_policy import current_gameplan_policy_metadata, saved_gameplan_policy_metadata
from ml.nightly_synthesis import run_synthesis
from test_nightly_synthesis import specification, NOW


def test_new_receipt_carries_policy_and_completed_retry_preserves_exact_bytes(specification):
    receipt = run_synthesis(specification, now=NOW)
    assert saved_gameplan_policy_metadata(receipt) == current_gameplan_policy_metadata(specification["action_date"])
    path = Path(receipt["receipt_path"])
    before = path.read_bytes()
    assert run_synthesis(specification, now=NOW) == receipt
    assert path.read_bytes() == before
