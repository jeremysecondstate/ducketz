"""Every Gameplan order entrypoint selects the current directional policy."""
from types import SimpleNamespace

import pytest

from ml import gameplan_stock_trader as cli
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY, LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY


def test_bare_gameplan_cli_defaults_to_current_policy_and_all_horizons(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "resolve_datastore_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr(cli, "run_gameplan_stock_trader_once", lambda *args, **kwargs:
        calls.append(kwargs) or SimpleNamespace(status="NO_ORDERS_SUBMITTED", to_dict=lambda: {}))
    assert cli.main(["--datastore-target", "pc"]) == 0
    assert calls[0]["target_horizon"] == "all"
    assert calls[0]["sizing_policy"] == GAMEPLAN_SIZING_POLICY


def test_direct_gameplan_api_defaults_to_current_all_horizon_runtime(tmp_path, monkeypatch):
    from ml.stock_trader import independent_runtime
    calls = []
    monkeypatch.setattr(independent_runtime, "run_independent_stock_trader_once", lambda *args, **kwargs:
        calls.append(kwargs) or "current-runtime")
    assert cli.run_gameplan_stock_trader_once(tmp_path) == "current-runtime"
    assert calls[0]["sizing_policy"] == GAMEPLAN_SIZING_POLICY


def test_managed_session_defaults_to_current_policy(tmp_path, monkeypatch):
    from ml.stock_trader import independent_session
    calls = []
    monkeypatch.setattr(cli, "resolve_datastore_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr(independent_session, "run_independent_stock_session", lambda *args, **kwargs:
        calls.append(kwargs) or {"status": "SESSION_FINISHED"})
    assert cli.main(["--datastore-target", "pc", "--run-session", "--execute"]) == 0
    assert calls[0]["sizing_policy"] == GAMEPLAN_SIZING_POLICY


@pytest.mark.parametrize("policy", [LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY])
@pytest.mark.parametrize("managed", [False, True])
def test_explicit_legacy_cli_policy_cannot_start_new_orders(tmp_path, monkeypatch, capsys, policy, managed):
    monkeypatch.setattr(cli, "resolve_datastore_dir", lambda **kwargs: pytest.fail("Legacy orders must stop before loading runtime"))
    args = ["--datastore-target", "pc", "--execute", "--sizing-policy", policy]
    if managed:
        args.append("--run-session")
    assert cli.main(args) == 1
    assert "legacy sizing is research-only" in capsys.readouterr().out


@pytest.mark.parametrize("policy", [LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY])
def test_explicit_legacy_api_policy_cannot_start_new_orders(tmp_path, policy):
    with pytest.raises(ValueError, match="legacy sizing is research-only"):
        cli.run_gameplan_stock_trader_once(tmp_path, execute=True, sizing_policy=policy)
