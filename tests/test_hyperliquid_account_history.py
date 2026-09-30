from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.models.portfolio import PortfolioSnapshot
from app.services.hyperliquid_account_history import HyperliquidAccountHistoryService, opening_equities

START = 1790732700.0
SEED = {"timestamp_utc": datetime.fromtimestamp(START, timezone.utc).isoformat(),
        "metadata": {"accounts": {"alex": {"source_equity": 100},
                                  "jeremy": {"source_equity": 200},
                                  "clearpond": {"source_equity": 300}}}}


def snapshots():
    return [PortfolioSnapshot(source="hyperliquid", account_label=key,
            synced_at=datetime.fromtimestamp(START, timezone.utc), reported_total_value=value)
            for key, value in (("Alex", 110), ("Jeremy", 210), ("Clearpond", 320))]


def test_real_totals_persist_across_service_instances_without_touching_paper(tmp_path):
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: START+60)
    assert history.record_snapshots(snapshots())
    rows = HyperliquidAccountHistoryService(tmp_path, clock=lambda: START+120).load_history(SEED)
    assert len(rows) == 4
    assert next(row for row in rows if row["account"] == "pooled")["equity"] == 640
    assert next(row for row in rows if row["account"] == "alex")["source_at_utc"] == SEED["timestamp_utc"]
    assert not (tmp_path / "_paper").exists()
    assert not (tmp_path / "_powder").exists()


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True])
def test_partial_or_invalid_equity_never_becomes_a_complete_total(tmp_path, bad):
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: START+60)
    rows = snapshots()
    rows[1] = replace(rows[1], reported_total_value=bad)
    assert not history.record_snapshots(rows)
    values = {row["account"]: row["equity"] for row in history.load_history(SEED)}
    assert values == {"alex": 110, "jeremy": None, "clearpond": 320, "pooled": None}


def test_error_duplicate_and_missing_accounts_are_gaps(tmp_path):
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: START+60)
    rows = snapshots()
    rows[0] = replace(rows[0], account_facts={"sync_error": "Unavailable"})
    assert not history.record_snapshots(rows)
    assert not history.record_snapshots([*snapshots(), snapshots()[0]])
    rows = history.load_history(SEED)
    assert next(row for row in rows if row["account"] == "pooled")["equity"] is None


def test_history_only_reads_existing_samples_after_current_opening(tmp_path):
    now = [START-60]
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: now[0])
    assert history.load_history(SEED) == []
    assert not history.path.parent.exists()
    history.record_snapshots(snapshots())
    now[0] = START+60
    history.record_snapshots(snapshots())
    rows = history.load_history(SEED)
    assert len(rows) == 4
    assert all(row["observed_epoch"] == START+60 for row in rows)


def test_paper_rollover_selects_new_opening_without_resetting_real_history(tmp_path):
    now = [START+60]
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: now[0])
    history.record_snapshots(snapshots())
    now[0] = START+600
    history.record_snapshots(snapshots())
    next_seed = {**SEED, "timestamp_utc": datetime.fromtimestamp(START+300, timezone.utc).isoformat()}
    assert len(history.load_history(next_seed)) == 4
    assert len(history.load_history(SEED)) == 8


def test_downsampling_preserves_endpoints_and_marks_missing_buckets(tmp_path):
    now = [START]
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: now[0], history_limit=32)
    for index in range(100):
        now[0] = START+index*60
        history.record_snapshots([] if index == 50 else snapshots())
    rows = history.load_history(SEED)
    assert len(rows) <= 36
    for account in ("alex", "jeremy", "clearpond", "pooled"):
        selected = [row for row in rows if row["account"] == account]
        assert selected[0]["observed_epoch"] == START
        assert selected[-1]["observed_epoch"] == now[0]
        assert any(row["had_gap"] for row in selected)


def test_automatic_sync_throttles_retries_and_reuses_manual_observations(tmp_path):
    now, calls = [START], []
    def loader():
        calls.append(now[0])
        return snapshots()
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: now[0], loader=loader)
    assert history.sync() == snapshots()
    now[0] += 10
    assert history.sync() is None
    history.sync(loader, force=True)
    now[0] += 59
    assert history.sync() is None
    now[0] += 1
    history.sync()
    assert len(calls) == 3
    def unavailable():
        raise TimeoutError("sample provider failure")
    now[0] += 60
    with pytest.raises(TimeoutError):
        history.sync(unavailable)
    assert "TimeoutError" in history.error
    assert history.sync(loader) is None
    latest = history.load_history(SEED)[-4:]
    assert all(row["equity"] is None for row in latest)


def test_storage_failure_preserves_successful_live_sync(tmp_path):
    history = HyperliquidAccountHistoryService(tmp_path, clock=lambda: START)
    history.path.parent.mkdir(parents=True)
    history.path.mkdir()
    assert history.sync(snapshots, force=True) == snapshots()
    assert "history unavailable" in history.error


def test_opening_uses_real_source_equity_and_refuses_partial_pool():
    seed = {**SEED, "baseline_equity": {"alex": 1000, "jeremy": 2000, "clearpond": 3000}}
    assert opening_equities(seed) == {"alex": 100, "jeremy": 200, "clearpond": 300, "pooled": 600}
    assert opening_equities({"metadata": {"accounts": {"alex": {"source_equity": 100}}}}) == {"alex": 100}
    assert opening_equities({"metadata": {"accounts": []}}) == {}
