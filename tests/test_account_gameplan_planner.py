from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pandas as pd
import pytest

from ml.account_gameplan.planner import (combine_inputs, project_account_plan, publish_account_plan,
                                         read_account_plan, render_account_plan, account_forecast_id)
from ml.account_gameplan.sources import export_source_bundle
from ml.artifacts import file_checksum, write_manifest
from test_account_gameplan_sources import DAY, export, native_case, save
from test_gameplan_cash_ledger import snapshot


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    sources = [export(native_case(tmp_path, monkeypatch, producer=p, symbol=s))
               for p, s in (("pc-original", "AAPL"), ("pc-new", "MU"))]
    state = snapshot(("AAPL", "MU"), cash=1000, equity=20000)
    state.update(observed_at="2026-10-05T06:00:00Z", account_fingerprint="a" * 64)
    return sources, state


def reseal_fixture(bundle, *, forecasts=None, prices=None, config_revision=None, receipt_revision=None):
    """Regenerate explicitly synthetic native files, then export a new pinned bundle."""
    root = bundle.path.parent
    game, trade = (root / bundle.metadata[key] for key in ("source_gameplan_run", "trade_plan_run"))
    frame = forecasts if forecasts is not None else bundle.forecasts
    symbols = sorted(set(frame.symbol))
    frame.to_parquet(game / "forecasts.parquet", index=False)
    frame.to_parquet(trade / "trade-plan.parquet", index=False)
    config = json.loads((game / "gameplan.json").read_text())
    config["symbols"] = symbols
    if config_revision is not None:
        config["source_revision"] = config_revision
    save(game / "gameplan.json", config)
    write_manifest(game, run_timestamp="2026-10-05T04:30:00Z", input_files=[],
        output_files=["gameplan.json", "forecasts.parquet"], configuration=config, datastore_root=root)
    receipt = json.loads((game / "receipt.json").read_text())
    if receipt_revision is not None:
        receipt["source_revision"] = receipt_revision
    receipt["manifest_checksum_sha256"] = file_checksum(game / "manifest.json")
    save(game / "receipt.json", receipt)
    if prices is not None:
        save(trade / "planning-price-path.json", prices)
    snapshot = json.loads((trade / "account-snapshot.json").read_text())
    snapshot["held_shares"] = {symbol: 0 for symbol in symbols}
    save(trade / "account-snapshot.json", snapshot)
    manifest = json.loads((trade / "manifest.json").read_text())
    trade_config = manifest["configuration"]
    trade_config["source_receipt_sha256"] = file_checksum(game / "receipt.json")
    report = json.loads((trade / "report.json").read_text())
    report.update(trade_config)
    report["snapshot"] = snapshot
    save(trade / "report.json", report)
    write_manifest(trade, run_timestamp="2026-10-05T05:05:00Z", input_files=[game / "receipt.json"],
        output_files=list(manifest["output_files"]), configuration=trade_config, datastore_root=root)
    receipt = json.loads((trade / "receipt.json").read_text())
    receipt.update(trade_config, manifest_sha256=file_checksum(trade / "manifest.json"))
    save(trade / "receipt.json", receipt)
    destination = root / (bundle.path.name + "-reseeded")
    return export_source_bundle(root, gameplan_run=game, trade_plan_run=trade, destination=destination,
        producer_id=bundle.metadata["producer_id"], expected_symbols=symbols)


def test_union_has_one_cash_buffer_and_global_priority(inputs):
    sources, state = inputs
    sources[0].forecasts["calibrated_probability"] = .60
    sources[1].forecasts["calibrated_probability"] = .80
    sources[1] = reseal_fixture(sources[1])
    rows, report, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
    ledger = report["direction_based_projection"]
    assert len(rows) == 48 and len(ledger["hourly"]) == 14
    assert ledger["summary"]["starting_cash"] == 1000
    assert ledger["summary"]["cash_buffer"] == 50
    assert ledger["summary"]["ending_cash_low"] >= 50
    assert ledger["events"][0]["symbol"] == "MU"
    assert sum(-event["cash_change_low"] for event in ledger["events"] if event["action"] == "BUY") <= 950
    first_hour = rows.loc[rows.execution_eligible & pd.to_datetime(rows.target_window_start).eq(pd.Timestamp(DAY + "T11:00Z"))]
    assert first_hour.groupby("symbol").projected_cash_after_low.first().nunique() == 1
    assert set(rows.producer_id) == {"pc-original", "pc-new"}
    text = render_account_plan(rows, report)
    assert "Ordered account events" in text and "No-fill cash baseline: 1,000.00" in text
    assert "Ending shares:" in text


def test_eleven_symbols_each_share_one_account_budget(inputs):
    templates, _ = inputs
    expanded, symbols = [], []
    for owner, template in enumerate(templates):
        names = [f"T{owner}{index}" for index in range(11)]
        symbols.extend(names)
        frames, points = [], {}
        for name in names:
            frame = template.forecasts.copy(deep=True)
            frame["symbol"] = name
            frame["id"] = [f"{name}:{route}" for route in frame.route]
            frames.append(frame)
            for key, point in template.price_path["points"].items():
                points[name + "|" + key.split("|", 1)[1]] = {**point, "symbol": name}
        expanded.append(reseal_fixture(template, forecasts=pd.concat(frames, ignore_index=True),
            prices={**template.price_path, "points": points}))
    state = snapshot(tuple(symbols), cash=1000, equity=20000)
    state.update(observed_at="2026-10-05T06:00:00Z", account_fingerprint="a" * 64)
    rows, report, prices = project_account_plan(expanded, state, observed_at=state["observed_at"])
    ledger = report["direction_based_projection"]
    assert len(rows) == 528 and rows.groupby("symbol").size().eq(24).all()
    assert len(prices["points"]) == 308
    assert ledger["summary"]["starting_cash"] == 1000 and ledger["summary"]["cash_buffer"] == 50
    assert ledger["summary"]["ending_cash_low"] >= 50
    assert len(set(rows.id)) == 528
    assert rows.groupby("producer_id").size().eq(264).all()


@pytest.mark.parametrize("bad", ["missing", "overlap", "day", "policy", "coverage", "price", "ids"])
def test_refuses_incomplete_or_incompatible_producers(inputs, bad):
    sources, state = inputs
    if bad == "missing": sources.pop()
    elif bad == "overlap": sources[1].metadata["symbols"] = ["AAPL"]
    elif bad == "day": sources[1].metadata["action_date"] = "2026-10-06"
    elif bad == "policy": sources[1].metadata["direction_up_threshold"] = .54
    elif bad == "coverage": sources[1] = replace(sources[1], forecasts=sources[1].forecasts.iloc[1:])
    elif bad == "price": sources[1].price_path["points"].pop(next(iter(sources[1].price_path["points"])))
    else: sources[1].forecasts.loc[1, "id"] = sources[1].forecasts.loc[0, "id"]
    with pytest.raises(ValueError):
        project_account_plan(sources, state, observed_at=state["observed_at"])


@pytest.mark.parametrize("stamp", ["2026-10-05T05:58:59Z", "2026-10-05T06:00:01Z"])
def test_stale_or_future_snapshot_rejected(inputs, stamp):
    sources, state = inputs
    state["observed_at"] = stamp
    with pytest.raises(ValueError, match="stale or future"):
        project_account_plan(sources, state, observed_at="2026-10-05T06:00:00Z")


def test_unavailable_price_never_manufactures_cash_projection(inputs):
    sources, state = inputs
    point = next(iter(sources[0].price_path["points"].values()))
    point.update(status="UNAVAILABLE_REFERENCE_PRICE", planned_price_low=None,
                 planned_price_mid=None, planned_price_high=None, reference_price=None)
    sources[0] = reseal_fixture(sources[0], prices=sources[0].price_path)
    rows, report, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
    assert report["direction_projection_status"] == "UNAVAILABLE_PRICE_REFERENCES"
    assert report["direction_based_projection"]["events"] == []
    assert rows.projected_cash_after_base.isna().all()


def test_publication_and_views_bind_original_ids_and_frozen_bytes(inputs, tmp_path):
    sources, state = inputs
    plan = publish_account_plan(tmp_path / "combined", sources, state,
                                observed_at=state["observed_at"], clock=lambda: pd.Timestamp(state["observed_at"]))
    first, cash1 = plan.view("pc-original")
    second, cash2 = plan.view("pc-new")
    assert cash1 == cash2 and len(first) == len(second) == 24
    assert plan.report["account_fingerprint"] == "a" * 64
    assert plan.report["probability_target_contract"] == "raw-price-direction-v1"
    assert all(row.id == account_forecast_id(row.producer_id, "frozen", row.original_forecast_id)
               for row in plan.rows.itertuples())
    with pytest.raises(FileExistsError):
        publish_account_plan(plan.path, sources, state, observed_at=state["observed_at"])
    (plan.path / "Gameplan.md").write_text("changed")
    with pytest.raises(ValueError, match="checksum"):
        read_account_plan(plan.path, expected_manifest_sha256=plan.manifest_sha256)


def test_sources_reopened_and_original_deadline_held(inputs, tmp_path):
    sources, state = inputs
    with pytest.raises(ValueError, match="deadline"):
        publish_account_plan(tmp_path / "late", sources, state, observed_at=state["observed_at"],
                             clock=lambda: pd.Timestamp(DAY + "T11:00Z"))
    assert not (tmp_path / "late/receipt.json").exists()
    (sources[0].path / "planning-price-path.json").write_text("{}")
    with pytest.raises(ValueError):
        publish_account_plan(tmp_path / "tampered", sources, state, observed_at=state["observed_at"])


def test_completion_cannot_use_old_snapshot_with_user_supplied_observation_time(inputs, tmp_path):
    sources, state = inputs
    with pytest.raises(ValueError, match="became stale"):
        publish_account_plan(tmp_path / "stale", sources, state, observed_at=state["observed_at"],
                             clock=lambda: pd.Timestamp(state["observed_at"]) + pd.Timedelta(seconds=61))
    assert not (tmp_path / "stale/receipt.json").exists()


def test_future_producer_receipt_rejected_even_before_opening(inputs):
    sources, state = inputs
    sources[1].metadata["trade_plan_completed_at"] = "2026-10-05T06:01:00Z"
    with pytest.raises(ValueError, match="source evidence is future"):
        project_account_plan(sources, state, observed_at=state["observed_at"])


@pytest.mark.parametrize("field,value", [("holding_policy", "fixed_target_expiry"),
                                        ("direction_up_threshold", .54)])
def test_two_matching_legacy_policies_cannot_be_silently_reinterpreted(inputs, field, value):
    sources, state = inputs
    for source in sources:
        source.metadata[field] = value
    with pytest.raises(ValueError, match="selected current manual"):
        project_account_plan(sources, state, observed_at=state["observed_at"])


def test_boundary_crossing_during_final_writes_invalidates_receipt(inputs, tmp_path):
    sources, state = inputs
    clock = iter([state["observed_at"], "2026-10-05T11:00:00Z"])
    with pytest.raises(ValueError, match="crossed during final writes"):
        publish_account_plan(tmp_path / "boundary", sources, state, observed_at=state["observed_at"],
                             clock=lambda: next(clock))
    receipt = json.loads((tmp_path / "boundary/receipt.json").read_text())
    assert receipt["status"] == "FAILED"
    with pytest.raises(ValueError, match="receipt"):
        read_account_plan(tmp_path / "boundary", expected_manifest_sha256=receipt["manifest_sha256"])


@pytest.mark.parametrize("name", ["report.json", "trade-plan.parquet"])
def test_reader_parses_only_the_bytes_it_verified(inputs, tmp_path, monkeypatch, name):
    sources, state = inputs
    plan = publish_account_plan(tmp_path / "verified", sources, state,
                                observed_at=state["observed_at"], clock=lambda: state["observed_at"])
    original_read = Path.read_bytes
    reads = []

    def swap_after_read(path):
        raw = original_read(path)
        if path == plan.path / name:
            reads.append(path)
            path.write_bytes(b"replaced after verification read")
        return raw

    monkeypatch.setattr(Path, "read_bytes", swap_after_read)
    loaded = read_account_plan(plan.path, expected_manifest_sha256=plan.manifest_sha256)
    assert len(reads) == 1
    assert loaded.rows.equals(plan.rows) and loaded.report == plan.report
