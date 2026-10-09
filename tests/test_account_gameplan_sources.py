from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.account_gameplan.sources import export_source_bundle, read_source_bundle, combined_forecast_id
from ml.artifacts import file_checksum, write_manifest, verify_manifest
from ml.independent_stock_targets import stock_target_windows
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date

DAY = "2026-10-05"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def native_case(tmp_path, monkeypatch, *, producer="Atlas", symbol="AAPL"):
    from ml import nightly_gameplan
    root = tmp_path / producer
    source = root / "ml/nightly-gameplan-runs/frozen"
    trade = root / "ml/gameplan-trade-plan-runs/planned"
    source.mkdir(parents=True)
    trade.mkdir(parents=True)
    fallback = policy_for_action_date(DAY)
    rows = []
    for window in stock_target_windows(date.fromisoformat(DAY)):
        hour = pd.Timestamp(window["target_window_start"]).tz_convert("America/Los_Angeles").strftime("%H:%M")
        rows.append({**window, "id": f"{DAY}:{symbol}:{window['route']}", "symbol": symbol,
            "action_date": DAY, "decision_timestamp": "2026-10-03T00:00:00Z",
            "information_available_at": "2026-10-03T00:00:00Z", "frozen_at": "2026-10-05T04:30:00Z",
            "calibrated_probability": .6, "model_family": "fixture", "model_status": "RESEARCH_NOT_PROMOTED",
            "direction": "BULLISH", "execution_authority": "SCHEDULED_REQUIRES_HUMAN_ENABLE",
            "broker_orders_enabled": False, "action_anchor_local": hour if window["execution_eligible"] else None,
            "target_price_source_contract": "xnas-itch-archive-v1", "target_price_dataset": "XNAS.ITCH",
            "probability_target_contract": "raw-price-direction-v1", "gameplan_variant": "YG",
            "direction_policy_version": "stock-direction-50-v2", "direction_up_threshold": .5,
            "direction_down_threshold": .5})
    # Use the existing native authority constant, not a test-specific relaxation.
    from ml.nightly_gameplan import EXECUTION_AUTHORITY
    for row in rows:
        row["execution_authority"] = EXECUTION_AUTHORITY
    forecasts = pd.DataFrame(rows)
    forecasts.to_parquet(source / "forecasts.parquet", index=False)
    config = {"action_date": DAY, "symbols": [symbol], "preparation_scope": "STOCK_ONLY",
        "target_contract_version": "independent-stock-targets-v1", "target_price_source_contract": "xnas-itch-archive-v1",
        "probability_target_contract": "raw-price-direction-v1", "cross_horizon_fallback_policy": fallback}
    save(source / "gameplan.json", config)
    write_manifest(source, run_timestamp="2026-10-05T04:30:00Z", input_files=[],
        output_files=["gameplan.json", "forecasts.parquet"], configuration=config, datastore_root=root)
    receipt = {"action_date": DAY, "run_path": source.relative_to(root).as_posix(), "published_at": "2026-10-05T05:00:00Z",
        "manifest_checksum_sha256": file_checksum(source / "manifest.json"), "cross_horizon_fallback_policy": fallback}
    save(source / "receipt.json", receipt)
    forecasts.to_parquet(trade / "trade-plan.parquet", index=False)
    price_path = {"contract_version": "conditional-hourly-planning-price-path-v3", "observed_at": "2026-10-05T05:15:00Z",
        "price_source_contract": "xnas-itch-archive-v1", "price_dataset": "XNAS.ITCH",
        "lookback_sessions": 504, "minimum_samples": 2,
        # A deliberately wide offline fixture keeps its simple 9/10/11 prices;
        # the production native builder retains its default 20 basis points.
        "working_half_width_bps": 1000,
        "reference_completion": {"contract_version": "bounded-planning-reference-completion-sparse-session-v2",
            "references": {}, "historical_references": {}, "synthetic_bars": []}, "points": {}}
    for hour in range(4, 18):
        clock = f"{hour:02d}:00"
        price_path["points"][f"{symbol}|{DAY}|{clock}"] = {"symbol": symbol, "action_date": DAY, "clock_local": clock,
            "timestamp": f"{DAY}T{clock}:00-07:00", "status": "AVAILABLE", "reason": "Observed fixture",
            "endpoint_kind": "planning_close" if hour == 17 else "observed_open",
            "sample_count": 2, "ratio_median": 1., "reference_price": 10.,
            "reference_session": "2026-10-02", "reference_observed_at": "2026-10-02T17:00:00-07:00",
            "reference_is_synthetic": False,
            "planned_price_low": 9, "planned_price_mid": 10, "planned_price_high": 11}
    save(trade / "planning-price-path.json", price_path)
    snapshot = {"observed_at": "2026-10-05T05:10:00Z", "available_cash": 999, "account_equity": 99999,
        "held_shares": {symbol: 0}, "pending_buy_shares": {}, "pending_sell_shares": {},
        "ownership": {"safe_for_planning": True, "active_allocations": [], "blocked_symbols": []}}
    save(trade / "account-snapshot.json", snapshot)
    ledger = {"status": "COMPLETE", "holding_policy": "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1",
        "cross_horizon_fallback_policy": fallback, "events": [], "hourly": []}
    save(trade / "direction-ledger.json", ledger)
    trade_config = {"action_date": DAY, "source_gameplan_run": source.relative_to(root).as_posix(),
        "source_receipt_sha256": file_checksum(source / "receipt.json"), "orders_placed": 0,
        "broker_orders_enabled": False, "cross_horizon_fallback_policy": fallback, "holding_policy": ledger["holding_policy"]}
    report = {**trade_config, "status": "COMPLETE", "snapshot": snapshot, "direction_based_projection": ledger,
        "observed_at": "2026-10-05T05:05:00Z", "completed_at": "2026-10-05T05:19:00Z"}
    save(trade / "report.json", report)
    write_manifest(trade, run_timestamp="2026-10-05T05:05:00Z", input_files=[source / "receipt.json"],
        output_files=["trade-plan.parquet", "planning-price-path.json", "direction-ledger.json", "report.json", "account-snapshot.json"],
        configuration=trade_config, datastore_root=root)
    save(trade / "receipt.json", {**trade_config, "status": "COMPLETE", "completed_at": "2026-10-05T05:20:00Z",
        "run_path": trade.relative_to(root).as_posix(), "manifest_sha256": file_checksum(trade / "manifest.json")})
    def verified_native(_root, path):
        # Native source generation/training is outside this offline fixture;
        # still exercise real immutable native file-integrity verification.
        return SimpleNamespace(run_directory=path, manifest=verify_manifest(path),
            receipt=json.loads((path / "receipt.json").read_text()))
    monkeypatch.setattr(nightly_gameplan, "read_gameplan_run", verified_native)
    return SimpleNamespace(root=root, source=source, trade=trade, producer=producer, symbol=symbol)


@pytest.fixture
def native(tmp_path, monkeypatch):
    return native_case(tmp_path, monkeypatch)


def export(case, destination=None):
    return export_source_bundle(case.root, gameplan_run=case.source, trade_plan_run=case.trade,
        destination=destination or case.root / "portable", producer_id=case.producer, expected_symbols=[case.symbol])


def read(bundle, **changes):
    kwargs = dict(expected_manifest_sha256=bundle.manifest_sha256, expected_producer=bundle.metadata["producer_id"],
        expected_symbols=bundle.metadata["symbols"], expected_action_date=DAY)
    kwargs.update(changes)
    return read_source_bundle(bundle.path, **kwargs)


def resign(bundle, mutate):
    path = bundle.path / "manifest.json"
    value = json.loads(path.read_text())
    mutate(value)
    save(path, value)
    return file_checksum(path)


def test_portable_bundle_preserves_originals_and_excludes_cash(native):
    before = {str(p): file_checksum(p) for folder in (native.source, native.trade) for p in folder.iterdir()}
    bundle = export(native)
    assert bundle.metadata["producer_id"] == "Atlas" and len(bundle.forecasts) == 24
    assert bundle.metadata["deployment_status"] == "NO_REGISTERED_HANDOFF"
    assert (bundle.path / "forecasts.parquet").read_bytes() == (native.source / "forecasts.parquet").read_bytes()
    assert "available_cash" not in json.dumps(bundle.ownership) and "account_equity" not in json.dumps(bundle.ownership)
    assert bundle.ownership["held_shares"] == {"AAPL": 0}
    assert read(bundle).metadata == bundle.metadata
    assert {str(p): file_checksum(p) for folder in (native.source, native.trade) for p in folder.iterdir()} == before
    with pytest.raises(FileExistsError):
        export(native)


def test_legacy_native_holding_policy_remains_explicit_exporter_attestation(native):
    manifest_path, receipt_path = native.trade / "manifest.json", native.trade / "receipt.json"
    manifest, receipt = json.loads(manifest_path.read_text()), json.loads(receipt_path.read_text())
    manifest["configuration"].pop("holding_policy")
    receipt.pop("holding_policy")
    report_path = native.trade / "report.json"
    report = json.loads(report_path.read_text())
    report.pop("holding_policy")
    save(report_path, report)
    manifest["output_files"]["report.json"].update(checksum_sha256=file_checksum(report_path), size=report_path.stat().st_size)
    save(manifest_path, manifest)
    receipt["manifest_sha256"] = file_checksum(manifest_path)
    save(receipt_path, receipt)
    bundle = export(native)
    assert bundle.metadata["holding_policy_proof"] == "EXPORTER_ATTESTED_LEGACY"
    assert read(bundle).metadata["holding_policy"] == "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"


def test_portable_fixture_prices_satisfy_existing_joint_composer_contract(tmp_path, monkeypatch):
    from ml.joint_capital_plan import build_owner_package
    native = native_case(tmp_path, monkeypatch, producer="pc-original")
    bundle = export(native)
    package = build_owner_package(owner_id=bundle.metadata["producer_id"], run_id=native.source.name,
        source_revision=None, action_date=DAY, frozen_symbols=[native.symbol],
        created_at=bundle.metadata["trade_plan_completed_at"],
        source_hashes={"receipt_sha256": bundle.metadata["source_receipt_sha256"],
            "manifest_sha256": bundle.metadata["source_manifest_sha256"],
            "forecasts_sha256": file_checksum(native.source / "forecasts.parquet"),
            "price_path_sha256": file_checksum(native.trade / "planning-price-path.json")},
        forecasts=bundle.forecasts.to_dict("records"), price_path=bundle.price_path,
        cross_horizon_fallback_policy=bundle.metadata["cross_horizon_fallback_policy"])
    assert package["source_revision_status"] == "UNRECORDED"
    assert len(package["forecasts"]) == 24 and package["price_path"]["lookback_sessions"] == 504


@pytest.mark.parametrize("field,value", [("expected_manifest_sha256", "0" * 64), ("expected_producer", "Scout"),
    ("expected_symbols", ["MU"]), ("expected_action_date", "2026-10-06")])
def test_explicit_registry_and_hash_cannot_be_substituted(native, field, value):
    with pytest.raises(ValueError):
        read(export(native), **{field: value})


@pytest.mark.parametrize("mutation", ["price_source", "fallback", "forecast_ids", "holding", "alternate_holding", "holding_proof", "published", "receipt_hash", "proof", "universe"])
def test_semantic_tampering_rejected_even_with_recomputed_bundle_hash(native, mutation):
    bundle = export(native)
    def mutate(value):
        metadata = value["metadata"]
        if mutation == "price_source": metadata["target_price_source_contract"] = "invented"
        elif mutation == "fallback": metadata["cross_horizon_fallback_policy"]["daily_cap_numerator"] = 2
        elif mutation == "forecast_ids": metadata["original_forecast_ids"].pop()
        elif mutation == "holding": metadata["holding_policy"] = "new-unapproved-policy"
        elif mutation == "alternate_holding": metadata["holding_policy"] = "fixed_target_expiry"
        elif mutation == "holding_proof": metadata["holding_policy_proof"] = "EXPORTER_ATTESTED_LEGACY"
        elif mutation == "published": metadata["source_published_at"] = f"{DAY}T12:00:00Z"
        elif mutation == "receipt_hash": metadata["source_receipt_sha256"] = "0" * 64
        elif mutation == "proof": value["native"]["source"]["receipt_json"] += " "
        else: metadata["symbols"] = ["MU"]
    digest = resign(bundle, mutate)
    with pytest.raises(ValueError):
        read(bundle, expected_manifest_sha256=digest)


@pytest.mark.parametrize("mutation", ["changed_bytes", "extra", "missing", "duplicate_json", "path_escape"])
def test_bundle_inventory_and_json_fail_closed(native, mutation):
    bundle = export(native)
    expected = bundle.manifest_sha256
    if mutation == "changed_bytes":
        with (bundle.path / "forecasts.parquet").open("ab") as stream: stream.write(b"tamper")
    elif mutation == "extra": (bundle.path / "not-reviewed.txt").write_text("extra")
    elif mutation == "missing": (bundle.path / "ownership.json").unlink()
    elif mutation == "duplicate_json":
        path = bundle.path / "manifest.json"
        text = path.read_text()
        path.write_text('{"schema_version":"wrong",' + text[1:])
        expected = file_checksum(path)
    else:
        expected = resign(bundle, lambda value: value["files"].update({"../outside": {}}))
    with pytest.raises((ValueError, FileNotFoundError)):
        read(bundle, expected_manifest_sha256=expected)


def test_forecast_identity_retains_producer_source_and_original():
    assert combined_forecast_id("Atlas", "a" * 64, "date:AAPL:1h@04:00") == "Atlas:" + "a" * 64 + ":date:AAPL:1h@04:00"
    with pytest.raises(ValueError): combined_forecast_id("Atlas:Scout", "a" * 64, "id")


def test_export_rejects_unbound_or_mutated_native_trade_plan(native):
    report = json.loads((native.trade / "report.json").read_text())
    report["source_receipt_sha256"] = "0" * 64
    save(native.trade / "report.json", report)
    with pytest.raises(RuntimeError, match="checksum"):
        export(native)
    assert not (native.root / "portable").exists()


def test_export_does_not_trust_claimed_promotion_without_native_model_evidence(native):
    forecasts = pd.read_parquet(native.source / "forecasts.parquet")
    forecasts["model_status"] = "PROMOTED"
    forecasts.to_parquet(native.source / "forecasts.parquet", index=False)
    manifest = json.loads((native.source / "manifest.json").read_text())
    manifest["output_files"]["forecasts.parquet"] = {
        "checksum_sha256": file_checksum(native.source / "forecasts.parquet"),
        "size": (native.source / "forecasts.parquet").stat().st_size}
    save(native.source / "manifest.json", manifest)
    with pytest.raises(ValueError, match="model reports"):
        export(native)
    assert not (native.root / "portable").exists()


def deployment(case, *, status="ACTIVE", selected=True):
    version = "gameplan-variant-deployment-v1"
    run = case.root / "ml/gameplan-deployment-runs/selected"
    run.mkdir(parents=True)
    report = {"schema_version": version, "action_date": DAY, "status": status,
        "updated_at": "2026-10-05T06:00:00Z", "orders_placed": 0, "broker_orders_enabled": False,
        "YG": {"run_path": case.source.relative_to(case.root).as_posix() if selected else "ml/nightly-gameplan-runs/other",
               "receipt_sha256": file_checksum(case.source / "receipt.json")},
        "YG_trade_plan": {"run_path": case.trade.relative_to(case.root).as_posix(),
                          "receipt_sha256": file_checksum(case.trade / "receipt.json")}}
    save(run / "deployment.json", report)
    write_manifest(run, run_timestamp=report["updated_at"], input_files=[], output_files=["deployment.json"],
        configuration={"schema_version": version, "action_date": DAY, "status": status}, datastore_root=case.root)
    save(run / "receipt.json", {"schema_version": version, "action_date": DAY, "status": status,
        "run_path": run.relative_to(case.root).as_posix(), "manifest_sha256": file_checksum(run / "manifest.json")})
    pointer = case.root / f"ml/gameplan-deployment-by-date/{DAY}/run.json"
    pointer.parent.mkdir(parents=True)
    save(pointer, {"schema_version": version, "current": {"run_path": run.relative_to(case.root).as_posix(),
        "receipt_sha256": file_checksum(run / "receipt.json")}})
    return run


def test_portable_source_retains_verified_active_native_selection(native):
    run = deployment(native)
    bundle = export(native)
    assert bundle.metadata["deployment_status"] == "ACTIVE"
    assert bundle.metadata["deployment_report_sha256"] == file_checksum(run / "deployment.json")
    assert read(bundle).metadata == bundle.metadata
    digest = resign(bundle, lambda doc: doc["metadata"].update(deployment_status="NO_REGISTERED_HANDOFF"))
    with pytest.raises(ValueError, match="unbound deployment"):
        read(bundle, expected_manifest_sha256=digest)


@pytest.mark.parametrize("failure", ["preparing", "unselected", "tampered"])
def test_native_selection_gate_cannot_be_bypassed_by_export(native, failure):
    run = deployment(native, status="PREPARING" if failure == "preparing" else "ACTIVE", selected=failure != "unselected")
    if failure == "tampered":
        with (run / "deployment.json").open("a") as stream:
            stream.write(" ")
    with pytest.raises((ValueError, RuntimeError)):
        export(native)
    assert not (native.root / "portable").exists()
