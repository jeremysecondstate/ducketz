"""Synthetic saved artifacts; no provider, broker, model or account reads."""
from hashlib import sha256
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pandas as pd
import pytest

from ml import joint_capital_handoff as handoff
from ml.independent_stock_targets import stock_target_windows
from ml.gameplan_price_bands import PLANNING_PRICE_PATH_CONTRACT, SPARSE_PLANNING_PRICE_PATH_CONTRACT
from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, stock_price_dataset

DAY = "2026-09-09"
CREATED = "2026-09-09T10:30:00Z"
FROZEN = "2026-09-09T10:00:00Z"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Handoff tests must not make network requests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def write(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def inventory(path):
    return {"size": path.stat().st_size, "checksum_sha256": digest(path)}


def rebind(case):
    case.gm["output_files"]["forecasts.parquet"] = inventory(case.game / "forecasts.parquet")
    write(case.game / "manifest.json", case.gm)
    case.gr["manifest_checksum_sha256"] = digest(case.game / "manifest.json")
    write(case.game / "receipt.json", case.gr)
    case.tm["configuration"]["source_receipt_sha256"] = digest(case.game / "receipt.json")
    case.tr["source_receipt_sha256"] = digest(case.game / "receipt.json")
    case.tm["output_files"]["planning-price-path.json"] = inventory(case.trade / "planning-price-path.json")
    write(case.trade / "manifest.json", case.tm)
    case.tr["manifest_sha256"] = digest(case.trade / "manifest.json")
    write(case.trade / "receipt.json", case.tr)


@pytest.fixture
def saved(tmp_path):
    game = tmp_path / "ml/nightly-gameplan-runs/native-source"
    trade = tmp_path / "ml/gameplan-trade-plan-runs/native-review"
    out = tmp_path / "handoff"
    for path in (game, trade, out):
        path.mkdir(parents=True)
    rows = []
    for index, spec in enumerate(stock_target_windows(pd.Timestamp(DAY).date())):
        start = pd.Timestamp(spec["target_window_start"])
        rows.append({**spec, "id": f"native-{index}", "symbol": "ABCL", "action_date": DAY,
            "target_window_start": start, "target_window_end": pd.Timestamp(spec["target_window_end"]),
            "decision_timestamp": FROZEN, "information_available_at": FROZEN, "frozen_at": FROZEN,
            "action_anchor_local": start.tz_convert("America/Los_Angeles").strftime("%H:%M") if spec["execution_eligible"] else None,
            "calibrated_probability": .6, "direction": "BULLISH", "model_status": "PROMOTED",
            "model_family": "fixture", "model_artifact": None, "execution_authority": "ADVISORY_PAPER_ONLY",
            "broker_orders_enabled": False, "target_contract_version": "independent-stock-targets-v1",
            "target_price_source_contract": CANONICAL_STOCK_PRICE_SOURCE,
            "target_price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
            "private_account_balance": 123456, "raw_probability": float("nan")})
    frame = pd.DataFrame(rows)
    frame.to_parquet(game / "forecasts.parquet", index=False)
    prices = {"contract_version": PLANNING_PRICE_PATH_CONTRACT, "observed_at": FROZEN,
        "price_source_contract": CANONICAL_STOCK_PRICE_SOURCE, "price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
        "lookback_sessions": 504, "minimum_samples": 2, "working_half_width_bps": 20,
        "available_cash": 123456, "points": {f"ABCL|{DAY}|{h:02d}:00": {
            "symbol": "ABCL", "action_date": DAY, "clock_local": f"{h:02d}:00", "timestamp": f"{DAY}T{h:02d}:00:00-07:00",
            "status": "AVAILABLE", "planned_price_low": 9.98, "planned_price_mid": 10., "planned_price_high": 10.02,
            "sample_count": 2, "reference_price": 10., "ratio_median": 1., "reference_session": "2026-09-08",
            "reference_observed_at": "2026-09-09T00:00:00Z",
            "held_shares": 17, "samples": [{"private_extension": "excluded"}]}
            for h in range(4, 18)}}
    write(trade / "planning-price-path.json", prices)
    config = {"action_date": DAY, "preparation_scope": "STOCK_ONLY", "symbols": ["ABCL"],
        "target_contract_version": "independent-stock-targets-v1", "target_price_source_contract": CANONICAL_STOCK_PRICE_SOURCE}
    gm = {"run_timestamp": FROZEN, "configuration": config, "output_files": {
        "forecasts.parquet": {}, "model.joblib": {"size": 999, "checksum_sha256": "a" * 64}}}
    gr = {"schema_version": "immutable-overnight-gameplan-receipt-v1", "run_path": game.relative_to(tmp_path).as_posix(),
        "run_timestamp": FROZEN, "action_date": DAY, "published_at": FROZEN,
        "execution_authority": "ADVISORY_PAPER_ONLY", "broker_orders_enabled": False, "orders_placed": 0}
    tc = {"schema_version": "cash-aware-gameplan-trade-planning-v4", "action_date": DAY,
        "source_gameplan_run": game.relative_to(tmp_path).as_posix(), "source_receipt_sha256": "",
        "execution_authority": "REVIEW_ONLY_REVALIDATE_AT_ENTRY", "orders_placed": 0, "broker_orders_enabled": False}
    tm = {"run_timestamp": FROZEN, "configuration": tc, "output_files": {"planning-price-path.json": {},
        "account-snapshot.json": {"size": 999, "checksum_sha256": "a" * 64}}}
    tr = {**tc, "status": "COMPLETE", "run_path": trade.relative_to(tmp_path).as_posix(),
        "forecast_rows": 24, "completed_at": FROZEN}
    case = SimpleNamespace(root=tmp_path, game=game, trade=trade, out=out, frame=frame,
        gm=gm, gr=gr, tm=tm, tr=tr, prices=prices)
    rebind(case)
    return case


def export(case, **kwargs):
    return handoff.export_owner_package(case.root, gameplan_run=case.game, trade_plan_run=case.trade,
        owner_id="scout", output_root=case.out, created_at=CREATED, **kwargs)


def test_export_retains_frozen_forecasts_and_excludes_account_model_and_raw_samples(saved, monkeypatch):
    original = handoff._read
    reads = []
    def checked(path, *args, **kwargs):
        reads.append(Path(path).name)
        assert Path(path).name not in {"account-snapshot.json", "report.json", "model.joblib"}
        return original(path, *args, **kwargs)
    monkeypatch.setattr(handoff, "_read", checked)
    before = {p: p.read_bytes() for p in (saved.game / "receipt.json", saved.game / "forecasts.parquet", saved.trade / "receipt.json")}
    path = export(saved)
    package = json.loads(path.read_text())
    assert package["source_revision"] is None and package["source_revision_status"] == "UNRECORDED"
    assert package["frozen_symbols"] == ["ABCL"] and len(package["forecasts"]) == 24
    assert {row["id"] for row in package["forecasts"]} == set(saved.frame.id)
    assert all(row["raw_probability"] is None for row in package["forecasts"])
    assert all(row["calibrated_probability"] == .6 for row in package["forecasts"])
    assert "private_account_balance" not in path.read_text() and "available_cash" not in path.read_text()
    assert "held_shares" not in path.read_text() and "private_extension" not in path.read_text()
    assert package["source_hashes"]["forecasts_sha256"] == digest(saved.game / "forecasts.parquet")
    assert {p: p.read_bytes() for p in before} == before
    assert not (saved.root / "ml/nightly-gameplan-latest").exists()
    with pytest.raises(FileExistsError):
        export(saved)


@pytest.mark.parametrize("name", ["manifest.json", "receipt.json", "forecasts.parquet"])
def test_changed_gameplan_bytes_fail(saved, name):
    path = saved.game / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises((ValueError, RuntimeError)):
        export(saved)
    assert list(saved.out.iterdir()) == []


@pytest.mark.parametrize("field,value", [("source_receipt_sha256", "b" * 64), ("action_date", "2026-09-10"),
    ("forecast_rows", 23), ("status", "FAILED"), ("broker_orders_enabled", True)])
def test_trade_receipt_mismatch_fails(saved, field, value):
    saved.tr[field] = value
    write(saved.trade / "receipt.json", saved.tr)
    with pytest.raises(ValueError, match="receipt"):
        export(saved)


@pytest.mark.parametrize("change", ["universe", "required_nan", "direction", "future", "partial_grid"])
def test_native_forecast_semantic_errors_fail_even_with_rebound_hashes(saved, change):
    if change == "universe": saved.frame.loc[0, "symbol"] = "DBX"
    if change == "required_nan": saved.frame.loc[0, "model_family"] = None
    if change == "direction": saved.frame.loc[0, "direction"] = "BEARISH"
    if change == "future": saved.frame.loc[0, "frozen_at"] = "2026-09-10T10:00:00Z"
    if change == "partial_grid": saved.frame = saved.frame.iloc[:-1]
    saved.frame.to_parquet(saved.game / "forecasts.parquet", index=False)
    rebind(saved)
    with pytest.raises(ValueError):
        export(saved)


def test_valid_no_history_abstention_keeps_probability(saved):
    saved.frame["symbol_fitted_target_rows"] = 1
    saved.frame["symbol_route_fitted_target_rows"] = 1
    saved.frame.loc[0, ["direction", "model_status", "symbol_route_fitted_target_rows"]] = ["NO_EDGE", "RESEARCH_NO_TARGET_HISTORY", 0]
    saved.frame.to_parquet(saved.game / "forecasts.parquet", index=False)
    rebind(saved)
    package = json.loads(export(saved).read_text())
    row = package["forecasts"][0]
    assert row["direction"] == "NO_EDGE" and row["calibrated_probability"] == .6


def test_raw_direction_metadata_is_bound_without_loading_fitted_models(saved):
    fields = {"probability_target_contract": "raw-price-direction-v1", "gameplan_variant": "YG"}
    saved.gm["configuration"].update(fields)
    saved.gr.update(fields)
    for field, value in fields.items(): saved.frame[field] = value
    saved.frame.to_parquet(saved.game / "forecasts.parquet", index=False)
    rebind(saved)
    assert json.loads(export(saved).read_text())["forecasts"][0]["gameplan_variant"] == "YG"


def test_supplied_revision_requires_exact_receipt_bound_provenance(saved):
    with pytest.raises(ValueError, match="provenance"):
        export(saved, source_revision="b" * 40)
    path = saved.root / "source-provenance.json"
    write(path, {"schema_version": "gameplan-source-provenance-v1", "source_revision": "b" * 40,
        "receipt_sha256": digest(saved.game / "receipt.json")})
    result = export(saved, source_revision="b" * 40, source_provenance=path, source_provenance_sha256=digest(path))
    package = json.loads(result.read_text())
    assert package["source_revision_status"] == "RECORDED"
    assert package["source_reference"]["kind"] == "CALLER_PINNED_SOURCE_REFERENCE"


def test_provenance_cannot_be_reused_for_another_receipt(saved):
    path = saved.root / "source-provenance.json"
    write(path, {"schema_version": "gameplan-source-provenance-v1", "source_revision": "b" * 40, "receipt_sha256": "c" * 64})
    with pytest.raises(ValueError, match="exact Gameplan receipt"):
        export(saved, source_revision="b" * 40, source_provenance=path, source_provenance_sha256=digest(path))


@pytest.mark.parametrize("name", ["../../outside.json", "nested/../outside.json", "nested//file.json",
    "/outside.json", "C:/outside.json", "nested\\file.json", "./file.json", "nested/./file.json", "nested/"])
def test_manifest_escape_is_rejected_without_following_it(saved, name):
    saved.tm["output_files"][name] = {"size": 0, "checksum_sha256": "a" * 64}
    rebind(saved)
    with pytest.raises(ValueError, match="unsafe output path"):
        export(saved)


def test_safe_nested_native_inventory_is_accepted_without_opening_models(saved, monkeypatch):
    for name in ("models/1h/model.joblib", "source-metadata/selection.json"):
        saved.gm["output_files"][name] = {"size": 999, "checksum_sha256": "a" * 64}
    rebind(saved)
    original = handoff._read
    def selected_only(path, *args, **kwargs):
        assert Path(path).name in {"manifest.json", "receipt.json", "forecasts.parquet", "planning-price-path.json"}
        return original(path, *args, **kwargs)
    monkeypatch.setattr(handoff, "_read", selected_only)
    assert len(json.loads(export(saved).read_text())["forecasts"]) == 24


@pytest.mark.parametrize("field", ["run_timestamp", "symbols"])
def test_duplicate_json_keys_are_rejected_at_any_metadata_depth(saved, field):
    path = saved.game / "manifest.json"
    payload = path.read_text(encoding="utf-8")
    path.write_text(payload.replace(f'"{field}":', f'"{field}": null, "{field}":', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON metadata key"):
        export(saved)


def test_selected_run_escape_is_rejected(saved):
    with pytest.raises(ValueError, match="immutable run directory"):
        handoff.export_owner_package(saved.root, gameplan_run=saved.out, trade_plan_run=saved.trade,
            owner_id="scout", output_root=saved.out, created_at=CREATED)


def test_symlink_input_is_rejected(saved):
    selected = saved.trade / "planning-price-path.json"
    real = saved.trade / "original-prices.json"
    selected.rename(real)
    try:
        selected.symlink_to(real)
    except OSError:
        real.rename(selected)
        pytest.skip("Host does not allow creating a synthetic symlink")
    with pytest.raises(ValueError, match="links or reparse"):
        export(saved)


def test_reparse_input_is_rejected_on_hosts_without_symlink_privileges(saved, monkeypatch):
    selected = saved.trade / "planning-price-path.json"
    original = Path.lstat
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == selected else info
    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError, match="links or reparse"):
        export(saved)


def test_source_mutation_during_validation_is_not_published(saved, monkeypatch):
    original = handoff.build_owner_package
    def mutate(**kwargs):
        package = original(**kwargs)
        with (saved.game / "receipt.json").open("ab") as stream:
            stream.write(b" ")
        return package
    monkeypatch.setattr(handoff, "build_owner_package", mutate)
    with pytest.raises(ValueError, match="changed during validation"):
        export(saved)
    assert list(saved.out.iterdir()) == []


def test_price_observation_cannot_postdate_its_completed_publication(saved):
    saved.prices["observed_at"] = "2026-09-09T10:20:00Z"
    write(saved.trade / "planning-price-path.json", saved.prices)
    rebind(saved)
    with pytest.raises(ValueError, match="Planning prices"):
        export(saved)


def test_native_price_file_uses_separate_bound_on_initial_and_final_reads(saved, monkeypatch):
    # Native historical samples can be much larger than the sanitized handoff.
    # Small patched limits exercise both reads without a large test artifact.
    saved.prices["historical_sample_fixture"] = "x" * 30000
    write(saved.trade / "planning-price-path.json", saved.prices)
    rebind(saved)
    monkeypatch.setattr(handoff, "_JSON_LIMIT", 16000)
    monkeypatch.setattr(handoff, "MAX_NATIVE_PRICE_BYTES", 65536)
    original = handoff._read
    price_limits = []
    def bounded(path, *args, **kwargs):
        if Path(path).name == "planning-price-path.json":
            price_limits.append(kwargs.get("maximum"))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(handoff, "_read", bounded)
    path = export(saved)
    assert price_limits == [65536, 65536]
    assert path.stat().st_size < (saved.trade / "planning-price-path.json").stat().st_size
    assert "historical_sample_fixture" not in path.read_text()
    assert json.loads(path.read_text())["source_hashes"]["price_path_sha256"] == digest(saved.trade / "planning-price-path.json")


def test_native_price_file_still_rejects_its_separate_size_limit(saved, monkeypatch):
    saved.prices["historical_sample_fixture"] = "x" * 30000
    write(saved.trade / "planning-price-path.json", saved.prices)
    rebind(saved)
    monkeypatch.setattr(handoff, "_JSON_LIMIT", 16000)
    monkeypatch.setattr(handoff, "MAX_NATIVE_PRICE_BYTES", 32000)
    assert (saved.trade / "planning-price-path.json").stat().st_size > 32000
    with pytest.raises(ValueError, match="size bound"):
        export(saved)
    assert list(saved.out.iterdir()) == []


def test_metadata_does_not_inherit_larger_native_price_limit(saved, monkeypatch):
    saved.gm["oversized_metadata_fixture"] = "x" * 17000
    rebind(saved)
    monkeypatch.setattr(handoff, "_JSON_LIMIT", 16000)
    monkeypatch.setattr(handoff, "MAX_NATIVE_PRICE_BYTES", 65536)
    with pytest.raises(ValueError, match="size bound"):
        export(saved)


def test_completed_reference_disclosures_survive_without_raw_reference_rows(saved):
    saved.prices["contract_version"] = SPARSE_PLANNING_PRICE_PATH_CONTRACT
    saved.prices["reference_completion"] = {"contract_version": "fixture-completion", "synthetic_bar_count": 3,
        "synthetic_bars": [{"private_extension": "excluded"}]}
    for point in saved.prices["points"].values():
        point.update(reference_is_synthetic=True, reference_fill_count=3, reference_gap_minutes=180)
    write(saved.trade / "planning-price-path.json", saved.prices)
    rebind(saved)
    package = json.loads(export(saved).read_text())
    assert package["price_path"]["reference_completion"]["synthetic_bar_count"] == 3
    assert all(point["reference_is_synthetic"] for point in package["price_path"]["points"].values())
    assert "synthetic_bars" not in package["price_path"]["reference_completion"]
