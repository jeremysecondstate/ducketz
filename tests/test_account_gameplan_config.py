import json
import pytest
from ml.account_gameplan.config import (CONFIG, VERSION, load_account_config, assert_coordinator,
                                       digest, verify_cutover)
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


def write_config(root, machine="pc-original", status="PREPARING"):
    value = {"schema_version": VERSION, "machine_id": machine, "coordinator_id": "pc-original",
             "participants": {"pc-original": ["AAPL"], "pc-new": ["MU"]}, "account_fingerprint": "a" * 64}
    value["activation"] = {"status": status, "binding_sha256": digest(value)}
    path = root / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


def test_unconfigured_is_legacy_and_preparing_does_not_authorize_execution(tmp_path):
    assert load_account_config(tmp_path) is None
    write_config(tmp_path)
    config = load_account_config(tmp_path)
    assert config.symbols == ("AAPL", "MU")
    with pytest.raises(ValueError, match="NOT_ACTIVE"):
        assert_coordinator(config, GAMEPLAN_SIZING_POLICY)


def test_scout_rejects_all_execution_even_when_active(tmp_path):
    write_config(tmp_path, "pc-new", "ACTIVE")
    with pytest.raises(ValueError, match="NO_ORDER_AUTHORITY"):
        assert_coordinator(load_account_config(tmp_path), GAMEPLAN_SIZING_POLICY)
    from ml.stock_trader.independent_session import run_independent_stock_session
    with pytest.raises(ValueError, match="NO_ORDER_AUTHORITY"):
        run_independent_stock_session(tmp_path, execute=True, wait_for_open=True,
            sizing_policy=GAMEPLAN_SIZING_POLICY, runner=lambda *a, **k: pytest.fail("must not run"))


def test_any_config_blocks_legacy_one_shot_before_broker(tmp_path):
    write_config(tmp_path)
    from ml.stock_trader.runtime import run_stock_trader_once
    with pytest.raises(ValueError, match="SOLE_INDEPENDENT"):
        run_stock_trader_once(tmp_path, execute=True)


def test_equivalent_unsorted_universes_have_canonical_runtime_membership(tmp_path):
    value = write_config(tmp_path)
    value["participants"]["pc-original"] = ["TSLA", "AAPL"]
    value["activation"]["binding_sha256"] = digest({k: v for k, v in value.items() if k != "activation"})
    (tmp_path / CONFIG).write_text(json.dumps(value))
    assert load_account_config(tmp_path).participants["pc-original"] == ("AAPL", "TSLA")


def test_active_account_ui_refuses_legacy_cash_projection_without_shared_pointer(tmp_path):
    from app.ui.gameplan_data import load_gameplan, GameplanError
    from gameplan_fixture import write_plan
    write_plan(tmp_path)
    write_config(tmp_path, status="ACTIVE")
    with pytest.raises(GameplanError, match="shared account Gameplan"):
        load_gameplan(tmp_path)


@pytest.mark.parametrize("mutation", ["hash", "coordinator", "overlap", "unknown", "active_without_receipt"])
def test_invalid_bindings_fail_closed(tmp_path, mutation):
    value = write_config(tmp_path)
    if mutation == "hash": value["account_fingerprint"] = "b" * 64
    elif mutation == "coordinator": value["coordinator_id"] = "pc-new"
    elif mutation == "overlap": value["participants"]["pc-new"] = ["AAPL"]
    elif mutation == "unknown": value["fallback_execute"] = True
    else: value["activation"]["status"] = "ACTIVE"
    (tmp_path / CONFIG).write_text(json.dumps(value))
    with pytest.raises((ValueError, TypeError, KeyError)):
        config = load_account_config(tmp_path)
        verify_cutover(tmp_path, config)
