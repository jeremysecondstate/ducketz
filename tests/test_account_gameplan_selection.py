import hashlib
import json
from pathlib import Path

import pytest

from ml.account_gameplan.config import CONFIG, VERSION, load_account_config
from ml.account_gameplan.planner import publish_account_plan, account_forecast_id
from ml.stock_trader.gameplan_execution import account_execution_plan, execution_frame, load_execution_signals
from test_account_gameplan_config import write_config
from test_account_gameplan_sources import DAY, native_case, export
from test_gameplan_cash_ledger import snapshot


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.fixture
def selected(tmp_path, monkeypatch):
    cases = [native_case(tmp_path, monkeypatch, producer=p, symbol=s)
             for p, s in (("pc-original", "AAPL"), ("pc-new", "MU"))]
    sources = [export(case) for case in cases]
    root = cases[0].root
    value = write_config(root, status="ACTIVE")
    receipt_path = root / "state/account-gameplan/cutovers/reviewed.json"
    save(receipt_path, {"schema_version": VERSION, "status": "VERIFIED",
        "binding_sha256": value["activation"]["binding_sha256"], "machine_id": "pc-original",
        "coordinator_id": "pc-original", "peer_execution_fenced": True, "peer_fence_receipt_sha256": "f" * 64,
        "migration_manifest_sha256": "b" * 64, "fresh_union_reconciliation_sha256": "c" * 64,
        "installed_source_commit": "d" * 40, "orders_placed": 0})
    value["activation"].update(receipt_path=receipt_path.relative_to(root).as_posix(),
                               receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest())
    save(root / CONFIG, value)
    state = snapshot(("AAPL", "MU"), cash=1000, equity=20000)
    state.update(observed_at=DAY + "T06:00:00Z", account_fingerprint="a" * 64)
    plan = publish_account_plan(root / "ml/account-gameplan-runs/combined", sources, state,
        observed_at=state["observed_at"], clock=lambda: state["observed_at"])
    pointer = {"schema_version": VERSION, "status": "SELECTED", "config_sha256": load_account_config(root).fingerprint,
               "action_date": DAY, "producer_id": "pc-original", "current": {
                   "run_path": plan.path.relative_to(root).as_posix(), "manifest_sha256": plan.manifest_sha256}}
    save(root / f"ml/account-gameplan-by-date/{DAY}/run.json", pointer)
    return root, plan, cases


def test_execution_uses_both_sources_original_horizons_and_no_ui_pointer(selected):
    root, plan, _ = selected
    frame, path = execution_frame(root, action_date=DAY)
    assert path == plan.path and len(frame) == 38
    assert set(frame.symbol) == {"AAPL", "MU"}
    signals, _ = load_execution_signals(root, as_of=DAY + "T11:01:00Z")
    assert len(signals) == 8 and {s.symbol for s in signals.values()} == {"AAPL", "MU"}
    expected = {account_forecast_id(row.producer_id, "frozen", row.original_forecast_id)
                for row in frame.itertuples()}
    assert {signal.prediction_id for signal in signals.values()}.issubset(expected)
    assert not (root / "ml/account-gameplan-latest/run.json").exists()


@pytest.mark.parametrize("failure", ["native_receipt", "selection", "receipt", "config", "date", "legacy"])
def test_configured_execution_never_falls_back_to_local_plan(selected, failure):
    root, plan, cases = selected
    if failure == "native_receipt": (cases[0].source / "receipt.json").write_text("{}")
    elif failure == "selection": (root / f"ml/account-gameplan-by-date/{DAY}/run.json").unlink()
    elif failure == "receipt": (root / "state/account-gameplan/cutovers/reviewed.json").write_text("{}")
    elif failure == "config":
        raw = json.loads((root / CONFIG).read_text())
        raw["activation"]["status"] = "PREPARING"
        save(root / CONFIG, raw)
    elif failure == "legacy":
        from ml.stock_trader.gameplan_execution import _assert_execution_deployment, GameplanDeploymentUnavailable
        with pytest.raises(GameplanDeploymentUnavailable):
            _assert_execution_deployment(root, cases[0].source, action_date=DAY)
        return
    with pytest.raises((ValueError, OSError)):
        account_execution_plan(root, action_date="2026-10-06" if failure == "date" else DAY)
