"""Rollover must preserve evidence and fail closed across unsafe interruptions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from filelock import FileLock, Timeout
import pytest

from ml import hyperliquid_paper_review as review
from ml.hyperliquid_paper_ledger import PaperLedger


def publish(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    review.write_json(path, value)


def make_paper(root, *, stamp="2026-09-28T20:00:00+00:00", experiment="prior", mirror=False):
    paper = root / "_paper"
    records, accounts = [], {}
    cash = {"alex": 1000.0, "jeremy": 2000.0, "clearpond": 3000.0}
    clock = datetime.fromisoformat(stamp) - timedelta(seconds=30)
    for index, account in enumerate(sorted(cash)):
        observations = {}
        for offset, (kind, response) in enumerate({
            "clearinghouseState": {"assetPositions": [], "marginSummary": {"accountValue": "0"}},
            "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": str(cash[account])}]},
            "userAbstraction": "unifiedAccount", "frontendOpenOrders": [],
        }.items()):
            start = clock + timedelta(seconds=index * 6 + offset)
            end = start + timedelta(milliseconds=500)
            observations[kind] = {"started_at_utc": start.isoformat(), "completed_at_utc": end.isoformat()}
            records.append({"type": kind, "public_owner_sha256": str(index) * 64,
                            "started_at_utc": (start + timedelta(milliseconds=50)).isoformat(),
                            "completed_at_utc": (end - timedelta(milliseconds=50)).isoformat(), "response": response})
        accounts[account] = {"read_observations": observations, "source_equity": cash[account]}
    with PaperLedger(paper / "ledger.sqlite3", cash, now=stamp,
                     metadata={"seed_mode": "mirror" if mirror else "cash", "accounts": accounts}) as ledger:
        publish(paper / "opening_snapshot.json", ledger.seed())
    publish(paper / "experiment.json", {"experiment_id": experiment, "seed_at_utc": stamp, "analysis_eligible": True})
    publish(paper / "policy.json", {"policy_id": "test-policy"})
    publish(paper / "_runtime/status.json", {"status": "stopped", "pid": 999999})
    publish(root / "_models/_runtime/status.json", {"status": "stopped", "pid": 999999})
    (root / "_models/weights.bin").write_bytes(b"unchanged estimator archive")
    return records


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    root, project = tmp_path / "data", tmp_path / "project"
    root.mkdir()
    project.mkdir()
    publish(project / "configs/hyperliquid-paper.json", {"mode": "paper", "seed_mode": "mirror"})
    publish(project / "configs/hyperliquid-models.json", {"recipe": "incumbent"})
    (project / "ml").mkdir()
    (project / "ml/hyperliquid_models.py").write_text("# incumbent source\n")
    make_paper(root)
    monkeypatch.setattr(review, "runtime_processes", lambda: [])
    return review.ReviewCycle(root, project, "review-01")


def test_begin_retains_prechange_evidence_and_preserves_incumbent(cycle):
    cycle.directory.mkdir()
    evidence = cycle.directory / "prechange-health.json"
    publish(evidence, {"result": "passed"})
    before = evidence.read_bytes()
    operation = cycle.begin()
    assert operation["phase"] == "begun"
    assert evidence.read_bytes() == before
    assert review.read_json(cycle.guard)["status"] == "in_progress"
    assert (cycle.directory / "before/ml/hyperliquid_models.py").read_text() == "# incumbent source\n"
    with pytest.raises(ValueError, match="Existing maintenance"):
        cycle.begin()


def test_other_owner_and_concurrent_stage_block(cycle):
    with FileLock(str(cycle.operations / ".paper-review.lock"), timeout=0):
        with pytest.raises(Timeout):
            cycle.begin()
    publish(cycle.guard, {"status": "in_progress", "owner": "other"})
    with pytest.raises(ValueError, match="Existing maintenance"):
        cycle.begin()


def test_archive_checksums_both_trees_without_touching_research_or_powder(cycle):
    publish(cycle.root / "_model_research/keep.json", {"research": "preserved"})
    publish(cycle.root / "_powder/keep.json", {"orders": "never touched"})
    original = review.inventory(cycle.root)
    cycle.begin()
    result = cycle.archive()
    assert result["result"] == "passed"
    assert review.inventory(cycle.archive_path) == original
    assert all(not (cycle.root / name).exists() for name in review.NAMESPACES)
    assert (cycle.root / "_model_research/keep.json").exists()
    assert (cycle.root / "_powder/keep.json").exists()
    assert cycle.archive() == result


def test_archive_rejects_live_process_and_preserves_namespace(cycle, monkeypatch):
    cycle.begin()
    monkeypatch.setattr(review, "runtime_processes", lambda: [{"pid": 42, "module": "ml.hyperliquid_paper_runtime"}])
    with pytest.raises(ValueError, match="runtime processes"):
        cycle.archive()
    assert (cycle.root / "_paper/ledger.sqlite3").exists()
    assert not cycle.archive_path.exists()


def test_archive_resumes_exact_interrupted_move(cycle, monkeypatch):
    cycle.begin()
    original = Path.rename
    failed = False

    def interrupted(source, destination):
        nonlocal failed
        if source == cycle.root / "_models" and not failed:
            failed = True
            raise OSError("simulated power loss between namespaces")
        return original(source, destination)

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(OSError, match="power loss"):
        cycle.archive()
    assert (cycle.archive_path / "_paper/ledger.sqlite3").exists()
    assert (cycle.root / "_models/weights.bin").exists()
    result = cycle.archive()
    assert result["result"] == "passed"
    assert review.read_json(cycle.operation_path)["phase"] == "archived"


def test_archive_rejects_changed_tree_after_interruption(cycle, monkeypatch):
    cycle.begin()
    original = Path.rename

    def interrupted(source, destination):
        if source == cycle.root / "_models":
            raise OSError("interrupted")
        return original(source, destination)

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(OSError):
        cycle.archive()
    (cycle.root / "_models/weights.bin").write_bytes(b"changed")
    monkeypatch.setattr(Path, "rename", original)
    with pytest.raises(ValueError, match="Archive source changed"):
        cycle.archive()
    assert review.read_json(cycle.guard)["status"] == "in_progress"


def test_rejects_path_traversal_and_dynamic_exclusions(cycle):
    with pytest.raises(ValueError, match="simple unique"):
        review.ReviewCycle(cycle.root, cycle.project, "../../powder")
    publish(cycle.operations / "excluded-paper-runs.json", {"excluded_runs": [{"experiment_id": "prior"}]})
    with pytest.raises(ValueError, match="exclusion registry"):
        cycle.begin()


def test_archive_rejects_held_runtime_lock(cycle):
    cycle.begin()
    lock_path = cycle.root / "_paper/_runtime/.paper.lock"
    with FileLock(str(lock_path), timeout=0):
        with pytest.raises(OSError):
            cycle.archive()
    assert (cycle.root / "_paper/ledger.sqlite3").exists()


def test_verified_source_opening_and_mismatch_detection(cycle):
    cycle.begin()
    cycle.archive()
    records = make_paper(cycle.root, stamp="2026-09-28T21:00:00+00:00", mirror=True)
    result = review.verify_opening(cycle.root / "_paper", records)
    assert result["result"] == "passed" and result["opening_equity"] == 6000
    altered = deepcopy(records)
    next(row for row in altered if row["type"] == "spotClearinghouseState")["response"]["balances"][0]["total"] = "9999"
    with pytest.raises(ValueError, match="collateral"):
        review.verify_opening(cycle.root / "_paper", altered)
    with pytest.raises(ValueError, match="Fresh Paper namespace"):
        cycle.prepare()


def test_source_matching_uses_one_owner_across_overlapping_concurrent_reads(cycle):
    cycle.begin()
    cycle.archive()
    records = make_paper(cycle.root, stamp="2026-09-28T21:00:00+00:00", mirror=True)
    # Alex's slow spot request also contains another account's quicker spot
    # response. The other three intervals uniquely establish Alex's owner.
    alex_spot = next(row for row in records if row["type"] == "spotClearinghouseState"
                     and row["public_owner_sha256"] == "0" * 64)
    other_spot = next(row for row in records if row["type"] == "spotClearinghouseState"
                      and row["public_owner_sha256"] == "1" * 64)
    nested = deepcopy(other_spot)
    nested["started_at_utc"] = alex_spot["started_at_utc"]
    nested["completed_at_utc"] = alex_spot["completed_at_utc"]
    records.append(nested)
    result = review.verify_opening(cycle.root / "_paper", records)
    assert result["public_owner_sha256"]["alex"] == "0" * 64
    assert result["opening_equity"] == 6000
    # If every interval supports both owners, timing cannot establish identity.
    for kind in ("clearinghouseState", "userAbstraction", "frontendOpenOrders"):
        original = next(row for row in records if row["type"] == kind and row["public_owner_sha256"] == "0" * 64)
        nested = deepcopy(original)
        nested["public_owner_sha256"] = "1" * 64
        records.append(nested)
    with pytest.raises(ValueError, match="ambiguous account owners"):
        review.verify_opening(cycle.root / "_paper", records)


def test_accept_leaves_guard_and_complete_requires_advancement(cycle):
    cycle.begin()
    cycle.archive()
    records = make_paper(cycle.root, stamp="2026-09-28T21:00:00+00:00", mirror=True)
    opening = review.verify_opening(cycle.root / "_paper", records)
    publish(cycle.directory / "opening-verification.json", opening)
    operation = review.read_json(cycle.operation_path)
    cycle.phase(operation, "prepared")
    accepted = cycle.accept()
    assert cycle.accept() == accepted
    assert review.read_json(cycle.guard)["status"] == "in_progress"
    assert accepted["opening_equity"] == 6000
    health_path = cycle.directory / "health.json"
    publish(health_path, {"result": "passed", "expectation": "trading", "read_at_utc": review.utc(),
                          "paper_seed_at_utc": accepted["seed_at_utc"]})
    with pytest.raises(ValueError, match="post-opening"):
        cycle.complete(health_path)


def test_powder_activity_blocks_begin(cycle, monkeypatch):
    monkeypatch.setattr(review, "runtime_processes", lambda: [{"pid": 10, "module": "ml.hyperliquid_powder_runtime"}])
    with pytest.raises(ValueError, match="Powder is active"):
        cycle.begin()
    assert not cycle.operation_path.exists()


def test_prepare_native_entrypoint_captures_source_and_is_idempotent(cycle, monkeypatch):
    cycle.begin()
    cycle.archive()
    from ml import hyperliquid_paper_runtime as runtime
    from ml import hyperliquid_paper_seed as seed_module
    from tests.test_hyperliquid_paper_runtime import FakeMarket
    from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES
    publish(cycle.project / "configs/hyperliquid-markets.json", {
        "version": 1, "symbols": ["BTC"], "interval": "15m", "output_root": str(cycle.root)})
    publish(cycle.project / "configs/hyperliquid-models.json", {
        "version": 1, "markets_config": "hyperliquid-markets.json", "horizons_bars": [4]})
    publish(cycle.project / "configs/hyperliquid-paper.json", {
        "version": 1, "mode": "paper", "seed_mode": "mirror", "data_root": str(cycle.root),
        "model_config": "hyperliquid-models.json"})
    values = {profile.wallet_address_env_keys[0]: "0x" + str(index + 1) * 40
              for index, profile in enumerate(HYPERLIQUID_ACCOUNT_PROFILES.values())}

    def read(_reader, payload):
        return {"clearinghouseState": {"assetPositions": [], "marginSummary": {"accountValue": "0"}},
                "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": "1000"}]},
                "userAbstraction": "unifiedAccount", "frontendOpenOrders": []}[payload["type"]]

    monkeypatch.setattr(seed_module.AccountReader, "post_info", read)
    original = seed_module.mirror_accounts
    monkeypatch.setattr(runtime, "mirror_accounts", lambda market, symbols, clock: original(market, symbols, clock=clock, values=values))
    monkeypatch.setattr(runtime, "PublicPaperMarket", lambda clock: FakeMarket(clock))
    result = cycle.prepare()
    assert result["opening_equity"] == 3000
    assert len(review.read_json(cycle.directory / "opening-public-account-reads.json")) == 12
    seed_hashes = result["immutable_opening_hashes"]
    assert cycle.prepare()["immutable_opening_hashes"] == seed_hashes


def test_complete_receipt_is_idempotent_and_requires_trading_health(cycle, monkeypatch):
    cycle.begin()
    cycle.archive()
    records = make_paper(cycle.root, stamp="2026-09-28T21:00:00+00:00", mirror=True)
    publish(cycle.directory / "opening-verification.json", review.verify_opening(cycle.root / "_paper", records))
    cycle.phase(review.read_json(cycle.operation_path), "prepared")
    accepted = cycle.accept()
    with PaperLedger(cycle.root / "_paper/ledger.sqlite3", open_existing=True) as ledger:
        ledger.execute_cycle("observation-after-opening", "2026-09-28T21:01:00+00:00", {}, [])
    health_path = cycle.directory / "health.json"
    health = {"result": "passed", "expectation": "prepared", "read_at_utc": review.utc(),
              "paper_seed_at_utc": accepted["seed_at_utc"]}
    publish(health_path, health)
    with pytest.raises(ValueError, match="trading health"):
        cycle.complete(health_path)
    health["expectation"] = "trading"
    publish(health_path, health)
    monkeypatch.setattr(review, "verify_running", lambda *args: {"paper": {"pid": 123}})
    completed = cycle.complete(health_path)
    assert review.read_json(cycle.guard)["status"] == "completed"
    assert review.read_json(cycle.guard)["phase"] == "completed"
    assert completed["status"] == "running"
    assert cycle.complete(health_path) == completed
    assert (cycle.directory.parent / "completed/review-01.json").exists()


def test_symlink_archive_source_is_rejected(cycle):
    link = cycle.root / "_models/escape"
    try:
        link.symlink_to(cycle.project, target_is_directory=True)
    except OSError:
        pytest.skip("Platform does not permit symlink creation")
    cycle.begin()
    with pytest.raises(ValueError, match="Symlink"):
        cycle.archive()
    assert (cycle.root / "_paper/ledger.sqlite3").exists()
