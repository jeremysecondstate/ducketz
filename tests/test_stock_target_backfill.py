"""Bounded native backfill using synthetic bytes and fake metadata only."""
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from datafetching import databento_cold_start as cold
from ml import stock_target_backfill as backfill


class FakeMetadata:
    def __init__(self, *, charged_request=None, bounds_start="2018-01-01"):
        self.events = []
        self.charged_request = charged_request
        self.bounds_start = bounds_start

    def list_schemas(self, **kwargs):
        self.events.append(("schemas", kwargs))
        return ["ohlcv-1m"]

    def get_dataset_range(self, **kwargs):
        self.events.append(("range", kwargs))
        return {"schema": {"ohlcv-1m": {"start": self.bounds_start, "end": "2026-09-05"}}}

    def get_cost(self, **kwargs):
        self.events.append(("cost", kwargs))
        return .01 if len([event for event in self.events if event[0] == "cost"]) == self.charged_request else 0.

    def get_billable_size(self, **kwargs):
        self.events.append(("size", kwargs))
        return 100

    def get_record_count(self, **kwargs):
        self.events.append(("count", kwargs))
        return 1


def publish_fixture(request):
    directory = Path(request["storage_path"])
    directory.mkdir(parents=True)
    raw, normalized = directory / "provider.dbn.zst", directory / "normalized.parquet"
    raw.write_bytes(b"synthetic native provider bytes")
    pd.DataFrame({"ts_event": [pd.Timestamp(request["start"], tz="UTC") + pd.Timedelta(hours=11)],
                  "open": [100.], "close": [101.], "symbol": request["symbol_scope"]}).to_parquet(normalized, index=False)
    manifest = {"schema_version": cold.PARTITION_VERSION, "request": dict(request),
        "raw": {"path": raw.name, "size_bytes": raw.stat().st_size, "checksum_sha256": cold.file_checksum(raw)},
        "normalized": {"path": normalized.name, "size_bytes": normalized.stat().st_size,
                       "checksum_sha256": cold.file_checksum(normalized),
                       **cold._validate_generic_parquet(normalized, request)}}
    cold._write_json_atomic(directory / "manifest.json", manifest)
    cold._write_json_atomic(directory / "receipt.json", {
        "schema_version": cold.RECEIPT_VERSION, "request_id": request["request_id"],
        "manifest_checksum_sha256": cold.file_checksum(directory / "manifest.json"),
        "normalized_checksum_sha256": cold.file_checksum(normalized)})


def latest_attempt(run):
    return run / json.loads((run / "latest-attempt.json").read_text())["attempt_path"]


def install_fake_download(monkeypatch, metadata, *, fail_call=None):
    state = {"calls": [], "fail_call": fail_call}
    def download(client, *, datastore_root, request, reporter=None):
        assert sum(event[0] == "cost" for event in metadata.events) >= 35
        assert sum(event[0] == "count" for event in metadata.events) >= 35
        assert sum(event[0] == "size" for event in metadata.events) >= 35
        state["calls"].append(request["request_id"])
        if len(state["calls"]) == state["fail_call"]:
            raise RuntimeError("synthetic interruption")
        publish_fixture(request)
    monkeypatch.setattr(cold, "_download_generic_entry", download)
    return state


def test_scope_is_five_original_native_chunks_with_current_entitlement(tmp_path):
    manifests = backfill.build_backfill_manifests(tmp_path)
    assert len(manifests) == 5
    assert sum(len(manifest["requests"]) for manifest in manifests) == 35
    assert manifests[0]["requests"][0]["start"] == "2025-01-13"
    assert manifests[-1]["as_of"] == "2026-05-28"
    for index, manifest in enumerate(manifests):
        cold._validate_manifest_checksum(manifest)
        cold._validate_manifest_included_scope(manifest)
        assert len(manifest["requests"]) == 7
        for request in manifest["requests"]:
            cold._validate_execution_request_identity(tmp_path, request)
            assert (request["dataset"], request["schema"], request["stype_in"]) == ("XNAS.ITCH", "ohlcv-1m", "raw_symbol")
            assert request["fetch_mode"] == "initial-baseline"
            assert date.fromisoformat(request["end"]) - date.fromisoformat(request["start"]) == pd.Timedelta(days=100)
        if index:
            assert manifests[index - 1]["as_of"] == manifest["requests"][0]["start"]


def test_historical_anchor_cannot_bypass_actual_current_eight_year_access(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "utc_timestamp", lambda: pd.Timestamp("2034-09-08T12:00Z"))
    with pytest.raises(ValueError, match="actual current eight-year"):
        backfill.build_backfill_manifests(tmp_path)


@pytest.mark.parametrize("arguments", [{"start": date(2023, 1, 1)}, {"through": date(2026, 9, 5)},
                                      {"start": date(2026, 5, 28)}])
def test_scope_cannot_expand_to_unbounded_history(tmp_path, arguments):
    with pytest.raises(ValueError, match="bounded"):
        backfill.build_backfill_manifests(tmp_path, **arguments)


def test_plan_only_has_no_client_and_preserves_plan_on_resume(tmp_path):
    run = backfill.run_stock_target_backfill(tmp_path, plan_only=True)
    before = (run / "plan.json").read_bytes()
    first = latest_attempt(run) / "receipt.json"
    first_bytes = first.read_bytes()
    assert json.loads(first_bytes)["status"] == "PLANNED"
    assert backfill.run_stock_target_backfill(tmp_path, plan_only=True, resume_run=run) == run
    assert (run / "plan.json").read_bytes() == before
    assert first.read_bytes() == first_bytes


def test_nonzero_cost_on_last_request_blocks_every_download_and_capacity_query(tmp_path, monkeypatch):
    metadata = FakeMetadata(charged_request=35)
    monkeypatch.setattr(cold, "execute_manifest", lambda *a, **kw: pytest.fail("paid data cannot download"))
    with pytest.raises(ValueError, match="zero cost"):
        backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), execute=True)
    assert sum(event[0] == "cost" for event in metadata.events) == 35
    assert not any(event[0] == "size" for event in metadata.events)
    costs = next((tmp_path / "ml/stock-target-backfill-runs").glob("*/attempts/*/cost-preflight.json"))
    assert json.loads(costs.read_text())["total_cost_usd"] == .01


def test_provider_range_is_checked_before_any_download(tmp_path, monkeypatch):
    metadata = FakeMetadata(bounds_start="2025-03-04")
    monkeypatch.setattr(cold, "execute_manifest", lambda *a, **kw: pytest.fail("uncovered data cannot download"))
    with pytest.raises(ValueError, match="every exact backfill request"):
        backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), execute=True)
    assert not any(event[0] == "cost" for event in metadata.events)


def test_aggregate_capacity_checks_all_chunks_before_any_download(tmp_path, monkeypatch):
    metadata = FakeMetadata()
    available = cold.required_free_bytes(700)  # Enough for one chunk, insufficient for all five.
    monkeypatch.setattr(backfill, "shutil", SimpleNamespace(disk_usage=lambda path: SimpleNamespace(free=available)))
    monkeypatch.setattr(cold, "execute_manifest", lambda *a, **kw: pytest.fail("aggregate capacity failed"))
    with pytest.raises(ValueError, match="Aggregate backfill capacity"):
        backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), execute=True)
    assert sum(event[0] == "count" for event in metadata.events) == 35
    path = next((tmp_path / "ml/stock-target-backfill-runs").glob("*/attempts/*/preflight-summary.json"))
    assert json.loads(path.read_text())["total_estimated_download_size_bytes"] == 3500


def test_metadata_only_preflight_does_not_publish_native_data_or_cursors(tmp_path):
    metadata = FakeMetadata()
    run = backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), reporter=None)
    receipt = json.loads((latest_attempt(run) / "receipt.json").read_text())
    assert receipt["status"] == "PREFLIGHTED"
    assert receipt["estimated_cost_usd"] == 0.
    assert receipt["total_estimated_download_size_bytes"] == 3500
    assert not (tmp_path / "market-data").exists()
    assert not (tmp_path / "state/databento/history-cursors").exists()


def test_execution_preflights_all_chunks_then_preserves_later_native_cursors(tmp_path, monkeypatch):
    recent = backfill._baseline_manifest(tmp_path, date(2026, 9, 5))
    cursors = {}
    for request in recent["requests"]:
        publish_fixture(request)
        path = cold._write_request_cursor(tmp_path, manifest_id=recent["manifest_id"], request=request, status="PUBLISHED")
        cursors[path] = path.read_bytes()
    metadata = FakeMetadata()
    state = install_fake_download(monkeypatch, metadata)
    run = backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), execute=True, reporter=None)
    receipt = json.loads((latest_attempt(run) / "receipt.json").read_text())
    assert receipt["status"] == "COMPLETE"
    assert len(receipt["completed_chunks"]) == 5
    assert len(state["calls"]) == 35
    assert all(path.read_bytes() == content for path, content in cursors.items())
    assert len(list((run / "chunks").glob("*/progress.json"))) == 5


def test_interrupted_native_backfill_resumes_without_replacing_evidence_or_redownloading(tmp_path, monkeypatch):
    metadata = FakeMetadata()
    state = install_fake_download(monkeypatch, metadata, fail_call=10)
    with pytest.raises(cold.ColdStartInfrastructureError, match="synthetic interruption"):
        backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), execute=True, reporter=None)
    run = next((tmp_path / "ml/stock-target-backfill-runs").iterdir())
    plan = (run / "plan.json").read_bytes()
    failed_receipt = latest_attempt(run) / "receipt.json"
    failed_bytes = failed_receipt.read_bytes()
    assert json.loads(failed_bytes)["status"] == "FAILED"
    state["fail_call"] = None
    assert backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata),
        execute=True, resume_run=run, reporter=None) == run
    assert json.loads((latest_attempt(run) / "receipt.json").read_text())["status"] == "COMPLETE"
    assert (run / "plan.json").read_bytes() == plan
    assert failed_receipt.read_bytes() == failed_bytes
    assert len(state["calls"]) == 36  # Thirty-five publications plus the one interrupted attempt.
    assert sum(event[0] == "cost" for event in metadata.events) == 70


@pytest.mark.parametrize("damage", ["cursor", "raw", "normalized"])
def test_later_cursor_is_not_silently_preserved_when_native_evidence_is_corrupt(tmp_path, damage):
    later = backfill._baseline_manifest(tmp_path, date(2026, 9, 5))
    request = later["requests"][0]
    publish_fixture(request)
    path = cold._write_request_cursor(tmp_path, manifest_id=later["manifest_id"], request=request, status="PUBLISHED")
    if damage == "cursor":
        cursor = json.loads(path.read_text())
        cursor["end"] = "2026-09-06"
        path.write_text(json.dumps(cursor))
    else:
        payload = Path(request["storage_path"]) / ("provider.dbn.zst" if damage == "raw" else "normalized.parquet")
        with payload.open("ab") as stream:
            stream.write(b"changed")
    before = path.read_bytes()
    older = backfill.build_backfill_manifests(tmp_path)[0]
    with pytest.raises(cold.ColdStartError):
        cold._write_request_cursor(tmp_path, manifest_id=older["manifest_id"], request=older["requests"][0], status="PUBLISHED")
    assert path.read_bytes() == before


def test_resume_rejects_changed_manifest_before_provider_metadata(tmp_path):
    run = backfill.run_stock_target_backfill(tmp_path, plan_only=True)
    manifest = run / "chunks/000/manifest.json"
    manifest.write_text(manifest.read_text() + " ")
    metadata = FakeMetadata()
    with pytest.raises(ValueError, match="chunk manifest changed"):
        backfill.run_stock_target_backfill(tmp_path, client=SimpleNamespace(metadata=metadata), resume_run=run)
    assert metadata.events == []


def test_nonregression_fix_does_not_change_unrelated_options_cursor_behavior(tmp_path, monkeypatch):
    manifest = cold.build_manifest(datastore_root=tmp_path, equities_symbols=("COST",),
        cme_dataset="GLBX.MDP3", cme_scopes=(), equities_dataset="XNAS.ITCH", as_of=date(2026, 9, 5))
    request = next(row for row in manifest["requests"] if row["dataset"] == cold.OPRA_DATASET)
    monkeypatch.setattr(cold, "_read_request_cursor", lambda *a, **kw: pytest.fail("new preservation logic must not touch OPRA"))
    path = cold._write_request_cursor(tmp_path, manifest_id=manifest["manifest_id"], request=request, status="PUBLISHED")
    assert json.loads(path.read_text())["request_id"] == request["request_id"]
