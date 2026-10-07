"""Offline account-view fixtures; source promotion checks live in source tests."""
import json

import pytest

from app.ui.gameplan_data import GameplanError, load_gameplan, plan_sessions
from ml.account_gameplan.planner import VERSION, encoded, project_account_plan, render_account_plan, sha, account_forecast_id
from ml.artifacts import file_checksum
from test_account_gameplan_sources import DAY, native_case, export, save
from test_gameplan_cash_ledger import snapshot
from gameplan_fixture import write_plan


def pin(root, run, producer="pc-original", *, digest=None):
    pointer = root / "ml/account-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    save(pointer, {"status": "SELECTED", "producer_id": producer, "current": {"run_path": run.relative_to(root).as_posix(),
        "manifest_sha256": digest or file_checksum(run / "manifest.json")}})


def bind(run):
    report = json.loads((run / "report.json").read_text())
    manifest = {"schema_version": VERSION, "action_date": report["action_date"], "sources": report["sources"],
        "output_files": {path.name: sha(path.read_bytes()) for path in run.iterdir()
                         if path.name not in {"manifest.json", "receipt.json"}}}
    save(run / "manifest.json", manifest)
    save(run / "receipt.json", {"schema_version": VERSION, "status": "COMPLETE", "action_date": report["action_date"],
        "manifest_sha256": file_checksum(run / "manifest.json"), "completed_at": report["observed_at"],
        "orders_placed": 0, "broker_orders_enabled": False})


@pytest.fixture
def account(tmp_path, monkeypatch):
    sources = [export(native_case(tmp_path, monkeypatch, producer=producer, symbol=symbol))
               for producer, symbol in (("pc-original", "AAPL"), ("pc-new", "MU"))]
    state = snapshot(("AAPL", "MU"), cash=1000, equity=10000)
    state.update(observed_at="2026-10-05T06:00:00Z", account_fingerprint="a" * 64)
    rows, report, prices = project_account_plan(sources, state, observed_at=state["observed_at"])
    run = tmp_path / "ml/account-gameplan-runs/reviewed"
    run.mkdir(parents=True)
    rows.to_parquet(run / "trade-plan.parquet", index=False)
    for name, value in (("report.json", report), ("direction-ledger.json", report["direction_based_projection"]),
                        ("account-snapshot.json", state), ("planning-price-path.json", prices)):
        (run / name).write_bytes(encoded(value))
    for producer in (None, "pc-original", "pc-new"):
        name = "Gameplan.md" if producer is None else f"Gameplan-{producer}.md"
        (run / name).write_text(render_account_plan(rows, report, producer), encoding="utf-8")
    bind(run)
    pin(tmp_path, run)
    return tmp_path, run


def test_two_views_filter_forecasts_and_actions_but_share_account_cash(account):
    root, run = account
    first = load_gameplan(root)
    pin(root, run, "pc-new")
    second = load_gameplan(root)
    assert first.symbols == ("AAPL",) and second.symbols == ("MU",)
    assert len(first.forecasts) == len(second.forecasts) == 24
    assert first.actions and second.actions
    assert {row.symbol for row in first.actions} == {"AAPL"}
    assert {row.symbol for row in second.actions} == {"MU"}
    assert first.account_ledger == second.account_ledger
    assert first.account_hourly == second.account_hourly
    assert {event["symbol"] for event in first.account_ledger["events"]} == {"AAPL", "MU"}
    a = next(row for row in first.forecasts if row.eligible and row.start.hour == 4)
    b = next(row for row in second.forecasts if row.eligible and row.start.hour == 4)
    assert (a.cash_after_low, a.cash_after_base, a.cash_after_high) == (b.cash_after_low, b.cash_after_base, b.cash_after_high)
    assert a.producer_id == "pc-original" and a.original_forecast_id.startswith(DAY + ":AAPL:")
    assert a.forecast_id == account_forecast_id(a.producer_id, "frozen", a.original_forecast_id)
    assert first.report_path.name == "Gameplan-pc-original.md"
    assert "conditional" in first.planning_note and len(first.source_provenance) == 2
    assert plan_sessions(root) == (DAY,)
    assert load_gameplan(root, DAY).account_hourly == first.account_hourly


def test_legacy_remains_unchanged_without_account_pointer(tmp_path):
    run = write_plan(tmp_path)
    plan = load_gameplan(tmp_path)
    assert plan.run_directory == run and plan.producer_id is None and plan.account_ledger is None


@pytest.mark.parametrize("failure", ["hash", "file", "escape", "producer", "incomplete", "foreign_identity", "private_cash", "view"])
def test_invalid_account_plan_never_falls_back_to_available_legacy(account, failure):
    root, run = account
    write_plan(root)
    if failure == "hash": pin(root, run, digest="0" * 64)
    elif failure == "file": (run / "Gameplan.md").write_text("changed")
    elif failure == "escape":
        pointer = root / "ml/account-gameplan-latest/run.json"
        document = json.loads(pointer.read_text())
        document["current"]["run_path"] = "../outside"
        save(pointer, document)
    elif failure == "producer": pin(root, run, "unregistered")
    elif failure == "view":
        (run / "Gameplan-pc-original.md").unlink()
        bind(run)
        pin(root, run)
    else:
        import pandas as pd
        rows = pd.read_parquet(run / "trade-plan.parquet")
        if failure == "incomplete": rows = rows.iloc[1:]
        elif failure == "private_cash": rows.loc[0, "projected_cash_after_base"] = 999999
        else: rows.loc[0, "source_receipt_sha256"] = "0" * 64
        rows.to_parquet(run / "trade-plan.parquet", index=False)
        bind(run)
        pin(root, run)
    with pytest.raises(GameplanError):
        load_gameplan(root)


def test_account_unavailable_projection_preserves_both_sources_without_fake_cash(account):
    from ml.account_gameplan.sources import read_source_bundle
    from ml.account_gameplan.planner import publish_account_plan
    from test_account_gameplan_planner import reseal_fixture
    root, run = account
    sources = [read_source_bundle(root / producer / "portable",
        expected_manifest_sha256=file_checksum(root / producer / "portable/manifest.json"),
        expected_producer=producer, expected_symbols=[symbol])
        for producer, symbol in (("pc-original", "AAPL"), ("pc-new", "MU"))]
    point = next(iter(sources[1].price_path["points"].values()))
    point.update(status="UNAVAILABLE_REFERENCE_PRICE", reference_price=None,
                 planned_price_low=None, planned_price_mid=None, planned_price_high=None)
    sources[1] = reseal_fixture(sources[1], prices=sources[1].price_path)
    state = json.loads((run / "account-snapshot.json").read_text())
    run = publish_account_plan(run.parent / "unavailable", sources, state,
        observed_at=state["observed_at"], clock=lambda: state["observed_at"]).path
    pin(root, run)
    first = load_gameplan(root)
    pin(root, run, "pc-new")
    second = load_gameplan(root)
    assert not first.projection_available and not second.projection_available
    assert first.actions == second.actions == first.account_hourly == second.account_hourly == ()
    assert all(row.quantity is None and row.cash_after_base is None for row in (*first.forecasts, *second.forecasts))
    assert "MU" in first.projection_note and first.account_ledger == second.account_ledger


def test_explicit_account_history_does_not_select_legacy_session(account):
    root, _ = account
    write_plan(root, session="2026-09-14")
    with pytest.raises(GameplanError, match="No completed account Gameplan"):
        load_gameplan(root, "2026-09-14")
