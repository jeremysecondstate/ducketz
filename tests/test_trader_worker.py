"""Offline entrypoint checks; imports are isolated and no live setup runs."""
import json
import ast
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

_adapters = Path(__file__).resolve().parents[1]
_intraday = _adapters / "ml/intraday/live_session.py"
_stock = _adapters / "ml/stock_trader/independent_session.py"
_intraday_defs = ast.parse(_intraday.read_bytes()).body if _intraday.is_file() else []
_stock_defs = ast.parse(_stock.read_bytes()).body if _stock.is_file() else []
if (not {"initialize_renewal_session", "run_renewal_lane"} <= {
        node.name for node in _intraday_defs if isinstance(node, ast.FunctionDef)}
        or not any(isinstance(node, ast.FunctionDef) and node.name == "run_independent_stock_session"
                   and any(arg.arg == "renewal" for arg in [*node.args.args, *node.args.kwonlyargs])
                   for node in _stock_defs)):
    pytest.skip("Installed trader renewal adapters required; apply the reviewed compatibility patch",
                allow_module_level=True)

from tools import trader_worker


@pytest.fixture
def first_use_account(tmp_path, monkeypatch):
    """Minimal private declaration and deliberately opaque original history."""
    from ml.account_gameplan.config import CONFIG, VERSION, digest, load_account_config
    from ml.trader_renewal import atomic_json
    from tools import gameplan_first_use, native_ownership_cutover
    repo, root = tmp_path / "repo", tmp_path / "datastore"
    account = {"schema_version": VERSION, "machine_id": "pc-original", "coordinator_id": "pc-original",
        "participants": {"pc-original": ["COST"], "pc-new": ["MSFT"]}, "account_fingerprint": "a" * 64}
    account["activation"] = {"status": "PREPARING", "binding_sha256": digest(account)}
    atomic_json(root / CONFIG, account)
    bound = load_account_config(root)
    marker = repo / "scratch/nightly-workflow/first-use.json"
    atomic_json(marker, {"schema_version": "gameplan-first-use-declaration-v1",
        "operation_id": "original-first-use-operation", "account_binding_sha256": bound.fingerprint,
        "account_fingerprint": bound.account_fingerprint, "executor": "Atlas",
        "basis": "LOCAL_HUMAN_ATLAS_SOLE_EXECUTOR", "peer_history_expected": False, "manual_start_only": True})
    ledger = root / native_ownership_cutover.LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger_group = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(ledger) + suffix)
        path.write_bytes(("native-original-history" + suffix).encode())
        ledger_group[path] = path.read_bytes()
    monkeypatch.setattr(gameplan_first_use, "_repository", lambda: repo)
    monkeypatch.setattr(native_ownership_cutover, "_capture",
                        lambda *a, **kw: pytest.fail("first-use fixture must not capture a broker"))
    return {"repo": repo, "root": root, "marker": marker,
            "original": (root / CONFIG).read_bytes(), "ledger_group": ledger_group}


@pytest.fixture(autouse=True)
def isolate_worker_environment(monkeypatch):
    from tools import trader_session_host
    from app.services import schwab_token_store
    monkeypatch.setattr(schwab_token_store, "TOKEN_CACHE_PATH", schwab_token_store.TOKEN_CACHE_PATH)
    monkeypatch.setattr(trader_session_host, "bind_kernel_lifetime", lambda: None)
    for key in ("DUCKETS_PRODUCTION_WATCHLIST", "DUCKETZ_TRADER_RENEWAL_REQUEST"):
        monkeypatch.setenv(key, os.environ.get(key, ""))


def test_fresh_bootstrap_excludes_editable_source_and_never_executes_pth(tmp_path):
    generation = tmp_path / "generations" / ("a" * 64)
    source, site = generation / "source", tmp_path / "sealed-site"
    (source / "tools").mkdir(parents=True)
    site.mkdir()
    (source / "tools/__init__.py").write_text("")
    (source / "tools/trader_worker.py").write_bytes(Path(trader_worker.__file__).read_bytes())
    stdlib = sysconfig.get_path("stdlib")
    selected = {"stdlib_paths": [stdlib, str(Path(sys.base_prefix) / "DLLs")], "site_packages": str(site)}
    (source / "tools/trader_installation.py").write_text(
        "def verify(*args, **kwargs):\n    return " + repr(selected) + "\n")
    (source / "fixture_version.py").write_text("VERSION = 'sealed-generation'\n")
    poison = tmp_path / "editable"
    poison.mkdir()
    (poison / "fixture_version.py").write_text("raise RuntimeError('mutable source imported')\n")
    evidence = tmp_path / "pth-executed"
    (site / "arbitrary.pth").write_text("import pathlib; pathlib.Path(" + repr(str(evidence)) + ").touch()\n")
    driver = tmp_path / "driver.py"
    driver.write_text("import runpy,sys,json\n"
        "worker=runpy.run_path(sys.argv[1])\nworker['bootstrap'](sys.argv[2])\n"
        "import fixture_version\nprint(json.dumps({'version':fixture_version.VERSION,'path':sys.path}))\n")
    result = subprocess.run([sys.executable, "-I", "-S", str(driver),
        str(source / "tools/trader_worker.py"), str(generation)],
        env={**os.environ, "PYTHONPATH": str(poison)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {"version": "sealed-generation", "path": [str(source), *selected["stdlib_paths"], str(site)]}
    assert not evidence.exists()


def request(tmp_path, *, trader="intraday", role="initialize", stopped=False):
    return SimpleNamespace(envelope=dict(repository=str(tmp_path / "bindings"), datastore=str(tmp_path / "data"),
        trader=trader, profile=str(tmp_path / "profile.json"), watchlist=str(tmp_path / "watchlist.txt"),
        options={"publication_offset_seconds": 30., "symbol_workers": None, "lookback_days": None},
        session={"session_id": "original-session"}), request={"role": role}, stopped=lambda: stopped,
        wait_due=lambda: None, claim=lambda: None)


def test_initial_intraday_setup_uses_actual_runtime_api_once(tmp_path, monkeypatch):
    import dotenv
    from ml.intraday import runtime, resource_policy, live_session
    calls = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path, **kwargs: calls.append(("env", path)))
    monkeypatch.setattr(runtime, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(resource_policy, "manual_symbol_workers", lambda *a, **kw: 2)
    session = {"session_id": "frozen", "started_at": "original", "close_at": "unchanged"}
    def initialize(**kwargs):
        calls.append(("initialize", kwargs))
        return session
    monkeypatch.setattr(live_session, "initialize_renewal_session", initialize)
    monkeypatch.setattr(live_session, "run_renewal_lane", lambda *a, **kw: pytest.fail("lane started before session admitted"))
    result = trader_worker.run(request(tmp_path))
    assert result == {"session": session, "roles": ["execution", "forecast", "scoring", "candles", "market_inputs"]}
    assert calls[1][1]["config"].symbol_workers == 2
    assert calls[1][1]["config"].publication_offset_seconds == 30
    assert len([c for c in calls if c[0] == "initialize"]) == 1
    assert os.environ["DUCKETS_PRODUCTION_WATCHLIST"] == str(tmp_path / "watchlist.txt")
    from app.services.schwab_token_store import TOKEN_CACHE_PATH
    assert TOKEN_CACHE_PATH == tmp_path / "bindings/data/schwab_tokens.json"


@pytest.mark.parametrize("role", ["execution", "forecast", "scoring", "candles", "market_inputs"])
def test_intraday_continuation_never_initializes_or_activates_again(tmp_path, monkeypatch, role):
    import dotenv
    from ml.intraday import live_session
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(live_session, "initialize_renewal_session", lambda **kw: pytest.fail("repeated manual start"))
    renewal = request(tmp_path, role=role)
    seen = []
    def run(lane, **kwargs):
        seen.append((lane, kwargs))
        return {"status": "COMPLETED", "role": lane}
    monkeypatch.setattr(live_session, "run_renewal_lane", run)
    assert trader_worker.run(renewal) == {"status": "COMPLETED", "role": role}
    assert seen[0][0] == role and seen[0][1]["renewal"] is renewal
    assert seen[0][1]["repository"] == tmp_path / "bindings"


@pytest.mark.parametrize("trader", ["intraday", "gameplan"])
@pytest.mark.parametrize("role", ["initialize", "execution"])
def test_preexisting_stop_prevents_even_environment_or_activation_setup(tmp_path, monkeypatch, trader, role):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: pytest.fail("setup after stop"))
    assert trader_worker.run(request(tmp_path, trader=trader, role=role, stopped=True)) == {"status": "STOPPED"}


def test_gameplan_continuation_never_repeats_manual_activation(tmp_path, monkeypatch):
    import dotenv
    from ml.stock_trader import control, gameplan, independent_session
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(control, "write_activation_intent", lambda *a, **k: pytest.fail("activation on continuation"))
    monkeypatch.setattr(gameplan, "write_gameplan_stock_activation_intent", lambda *a, **k: pytest.fail("activation on continuation"))
    seen = []
    monkeypatch.setattr(independent_session, "run_independent_stock_session",
        lambda root, **kw: seen.append((root, kw)) or {"status": "SESSION_COMPLETED"})
    renewal = request(tmp_path, trader="gameplan", role="execution")
    assert trader_worker.run(renewal) == {"status": "SESSION_COMPLETED"}
    assert seen[0][1]["renewal"] is renewal


@pytest.mark.parametrize("stopped", [False, True])
def test_worker_main_keeps_unknown_interruption_distinct_from_manual_stop(tmp_path, monkeypatch, stopped):
    from ml import trader_renewal
    renewal = request(tmp_path, stopped=stopped)
    results = []
    renewal.finish = results.append
    monkeypatch.setattr(trader_worker, "bootstrap", lambda path, **kwargs: {})
    monkeypatch.setattr(trader_renewal, "Renewal", lambda path: renewal)
    monkeypatch.setattr(trader_worker, "run", lambda _: (_ for _ in ()).throw(InterruptedError("fixture")))
    arguments = ["--generation", str(tmp_path), "--request", str(tmp_path / "request.json")]
    if stopped:
        assert trader_worker.main(arguments) == 0
        assert results == [{"status": "STOPPED"}]
    else:
        with pytest.raises(InterruptedError):
            trader_worker.main(arguments)
        assert results == []


def test_worker_main_has_dedicated_planned_exit_without_terminal_closeout(tmp_path, monkeypatch):
    from ml import trader_renewal
    renewal = request(tmp_path)
    renewal.finish = lambda result: pytest.fail("terminal closeout on planned renewal")
    monkeypatch.setattr(trader_worker, "bootstrap", lambda path, **kwargs: {})
    monkeypatch.setattr(trader_renewal, "Renewal", lambda path: renewal)
    def renew(_):
        raise trader_renewal.PlannedRenewal()
    monkeypatch.setattr(trader_worker, "run", renew)
    assert trader_worker.main(["--generation", str(tmp_path), "--request", str(tmp_path / "request.json")]) == trader_renewal.PLANNED_EXIT


def test_verify_only_requires_full_verification_and_never_initializes_session(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(trader_worker, "bootstrap", lambda path, **kwargs:
        calls.append((path, kwargs)) or {"generation": "sealed-source", "runtime_id": "sealed-runtime"})
    monkeypatch.setattr(trader_worker, "run", lambda _: pytest.fail("verification started a trader"))
    assert trader_worker.main(["--generation", str(tmp_path), "--verify-only"]) == 0
    assert calls == [(tmp_path, {"full": True})]
    assert json.loads(capsys.readouterr().out) == {
        "verified": True, "generation": "sealed-source", "runtime_id": "sealed-runtime"}


def test_worker_without_request_has_no_initial_start_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(trader_worker, "bootstrap", lambda *a, **k: pytest.fail("bootstrap without request"))
    with pytest.raises(SystemExit) as exc:
        trader_worker.main(["--generation", str(tmp_path)])
    assert exc.value.code == 2


@contextmanager
def managed_initializer(root):
    from ml.trader_renewal import Renewal, atomic_json, control_signatures, process_birth, PROTOCOL
    from ml.stock_trader.control import write_activation_intent
    from ml.stock_trader.gameplan import write_gameplan_stock_activation_intent
    from tools.trader_session_host import own_session
    write_activation_intent(root, active=False)
    write_gameplan_stock_activation_intent(root, active=False)
    directory = root / "state/trader-renewal/gameplan/manual-bindings-test"
    directory.mkdir(parents=True)
    envelope = {"protocol": PROTOCOL, "trader": "gameplan", "datastore": str(root),
        "owner_pid": os.getpid(), "owner_birth": process_birth(os.getpid()), "token": "offline-initializer",
        "stop_path": str(directory / "stop.json"), "session": {"session_id": "offline-session"}}
    envelope["initial_controls"] = control_signatures(envelope)
    atomic_json(directory / "session.json", envelope)
    atomic_json(directory.parent / "active.json", dict(envelope, status="RUNNING"))
    dispatch = directory / "request.json"
    atomic_json(dispatch, {"envelope": str(directory / "session.json"), "token": envelope["token"],
        "role": "initialize", "turn": "one", "generation": 1, "response": str(directory / "response.json")})
    context = Renewal(dispatch)
    context.claim()
    with own_session(root, "gameplan"):
        yield context


@pytest.mark.parametrize("damage", ["none", "claim", "owner", "active", "role", "datastore", "stop"])
def test_initial_setup_reuses_only_its_exact_claimed_controller_lock(tmp_path, damage):
    from ml.trader_renewal import atomic_json
    with managed_initializer(tmp_path) as context:
        root = tmp_path
        if damage == "claim":
            context.request_path.with_suffix(".claim.json").unlink()
        elif damage == "owner":
            (tmp_path / "locks/independent-stock-session.lock").write_text("process=independent-stock-session\npid=0\ntoken=other\n")
        elif damage == "active":
            atomic_json(tmp_path / "state/trader-renewal/gameplan/active.json", {"status": "ENDED"})
        elif damage == "role":
            context.request["role"] = "execution"
        elif damage == "datastore":
            root = tmp_path / "different"
        elif damage == "stop":
            atomic_json(context.envelope["stop_path"], {"reason": "MANUAL_STOP"})
        if damage == "none":
            assert context.require_initial_session_lock(root)
        else:
            with pytest.raises((OSError, ValueError, RuntimeError)):
                context.require_initial_session_lock(root)


def test_first_use_uses_original_private_declaration_under_controller_lock(first_use_account, tmp_path, monkeypatch):
    from tools import gameplan_first_use, native_ownership_cutover
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    case = first_use_account
    frozen = tmp_path / "frozen-source"
    frozen.mkdir()
    monkeypatch.setattr(gameplan_first_use, "_repository", lambda: frozen)
    monkeypatch.setattr(native_ownership_cutover, "_repository", lambda: frozen)
    monkeypatch.setattr(native_ownership_cutover, "_capture", lambda *a, **kw: pytest.fail("broker during first-use fixture"))
    with managed_initializer(case["root"]) as context:
        original_lock = (case["root"] / "locks/independent-stock-session.lock").read_bytes()
        assert gameplan_first_use.inspect_first_use(case["root"]) is None
        assert gameplan_first_use.inspect_first_use(case["root"], repository=case["repo"])["ready"]
        result = native_ownership_cutover.ensure_gameplan_account_ready(case["root"],
            sizing_policy=GAMEPLAN_SIZING_POLICY, repository=case["repo"], renewal=context)
        assert result["ready"] and result["activation_changed"] and not result["broker_read_performed"]
        assert (case["root"] / "locks/independent-stock-session.lock").read_bytes() == original_lock
    assert all(path.read_bytes() == raw for path, raw in case["ledger_group"].items())
    assert not list(frozen.rglob("*"))


def test_failed_native_setup_keeps_private_diagnostics_out_of_frozen_source(first_use_account, tmp_path, monkeypatch):
    from tools import gameplan_first_use, native_ownership_cutover
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    case = first_use_account
    case["marker"].unlink()
    frozen = tmp_path / "sealed-source"
    frozen.mkdir()
    monkeypatch.setattr(gameplan_first_use, "_repository", lambda: frozen)
    monkeypatch.setattr(native_ownership_cutover, "_repository", lambda: frozen)
    with managed_initializer(case["root"]) as context:
        result = native_ownership_cutover.ensure_gameplan_account_ready(case["root"],
            sizing_policy=GAMEPLAN_SIZING_POLICY, repository=case["repo"], renewal=context,
            capture=lambda *a: pytest.fail("missing configuration must not contact broker"))
        assert not result["ready"] and result["reason"] == "NATIVE_INVENTORY_CONFIGURATION_MISSING"
    assert len(list((case["repo"] / "scratch/nightly-workflow/manual-start-failures").glob("*.json"))) == 1
    assert not list(frozen.rglob("*"))
    assert (case["root"] / "state/account-gameplan/config.json").read_bytes() == case["original"]


def test_native_setup_reuses_outer_lock_but_keeps_hourly_exclusion(tmp_path):
    from tools import native_ownership_cutover
    from datafetching.runtime_lock import exclusive_runtime_lock
    root, work = tmp_path / "data", tmp_path / "operation/activation"
    work.mkdir(parents=True)
    (root / "state/account-gameplan").mkdir(parents=True)
    exchange_lock = tmp_path / "exchange.lock"
    with managed_initializer(root) as context:
        expected = (root / "locks/independent-stock-session.lock").read_bytes()
        with native_ownership_cutover._locks(root, work, exchange_lock, renewal=context):
            assert (root / "locks/independent-stock-session.lock").read_bytes() == expected
            assert (root / "locks/stock-trader-hourly.lock").is_file()
        # The retained marker is conservative when a synthetic fixture has no
        # account; remove only this fixture's marker to exercise live exclusion.
        (root / "locks/stock-trader-hourly.lock").unlink()
        with exclusive_runtime_lock(root / "locks/stock-trader-hourly.lock", process_name="fixture-other-owner"):
            with pytest.raises(FileExistsError):
                with native_ownership_cutover._locks(root, work, exchange_lock, renewal=context):
                    pytest.fail("lost hourly exclusion")


def test_native_private_binding_paths_are_separate_from_sealed_code(tmp_path, monkeypatch):
    from tools import native_ownership_exchange as exchange
    original, frozen, root = tmp_path / "original", tmp_path / "frozen", tmp_path / "data"
    for path in (original, frozen, root):
        path.mkdir()
    workflow = original / "scratch/nightly-workflow/config.json"
    profile = original / "scratch/cross-pc/local-profile.json"
    active = original / "scratch/cross-pc/active.json"
    state = original / "scratch/native-state"
    state.mkdir(parents=True)
    shared = tmp_path / "CODEXSTORE/ducketz-nightly-exchange/v1"
    shared.mkdir(parents=True)
    owners = {"atlas": [f"A{i}" for i in range(11)], "scout": [f"B{i}" for i in range(11)]}
    cfg = {"schema_version": exchange.VERSION, "actor": "Atlas", "operation_id": "synthetic-operation",
        "cutover_action_date": "2026-10-05", "datastore_root": str(root), "local_profile": str(profile),
        "coordination_active": str(active), "exchange_root": str(shared), "state_root": str(state),
        "private_native_ledger_exchange_authorized": True}
    from ml.trader_renewal import atomic_json
    atomic_json(profile, {"actor": "Atlas", "machine": "pc-original", "checkout": str(original), "symbols": owners["atlas"]})
    atomic_json(active, {})
    atomic_json(workflow, {"actor": "Atlas", "repository": str(original), "datastore": str(root),
        "local_profile": str(profile), "coordination_active": str(active)})
    atomic_json(workflow.with_name("exchange-config.json"), {"actor": "Atlas", "exchange_root": str(shared),
        "workflow_config": str(workflow), "private_exchange_authorized": True, "owners": owners,
        "local_profile": str(profile), "coordination_active": str(active), "account_scope_sha256": "a" * 64})
    cfg_path = original / "scratch/native-config.json"
    atomic_json(cfg_path, cfg)
    monkeypatch.setattr(exchange, "_repository", lambda: frozen)
    monkeypatch.setattr(exchange, "_verify_installation", lambda path: None)
    monkeypatch.setattr(exchange, "load_account_config", lambda path: SimpleNamespace(machine_id="pc-original",
        participants={"pc-original": owners["atlas"], "pc-new": owners["scout"]}, account_fingerprint="a" * 64))
    assert exchange.load_config(cfg_path, repository=original) == cfg
    inputs = exchange._binding_inputs(cfg, repository=original)
    assert inputs["private_environment"] == original / ".env"
    assert inputs["workflow"] == workflow
    assert inputs["tools/native_ownership_exchange.py"] == frozen / "tools/native_ownership_exchange.py"
    with pytest.raises(ValueError, match="private profile"):
        exchange.load_config(cfg_path)


def test_frozen_token_store_uses_original_cache_and_same_lock_without_copying_secrets(tmp_path):
    from app.services import schwab_token_store
    frozen, original = tmp_path / "source", tmp_path / "original"
    target = frozen / "app/services/schwab_token_store.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(Path(schwab_token_store.__file__).read_bytes())
    driver = tmp_path / "token-binding-probe.py"
    driver.write_text("import importlib.util,json,pathlib,sys\n"
        "spec=importlib.util.spec_from_file_location('frozen_tokens',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        "default=str(m.TOKEN_CACHE_PATH);m.configure_local_repository(pathlib.Path(sys.argv[2]))\n"
        "print(json.dumps({'default':default,'configured':str(m.TOKEN_CACHE_PATH),'lock':str(m.TOKEN_CACHE_PATH.with_name(m.TOKEN_CACHE_PATH.name+'.lock'))}))\n")
    result = subprocess.run([sys.executable, "-I", "-B", str(driver), str(target), str(original)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert Path(value["default"]) == frozen / "data/schwab_tokens.json"
    assert Path(value["configured"]) == original / "data/schwab_tokens.json"
    assert Path(value["lock"]) == original / "data/schwab_tokens.json.lock"
    assert not original.exists() and not (frozen / "data").exists()


def test_token_binding_rejects_relative_and_active_transactions(tmp_path, monkeypatch):
    from app.services import schwab_token_store as tokens
    with pytest.raises(ValueError):
        tokens.configure_local_repository(Path("relative"))
    monkeypatch.setattr(tokens._TOKEN_CACHE_LOCK_STATE, "depth", 1, raising=False)
    with pytest.raises(RuntimeError, match="active token-cache"):
        tokens.configure_local_repository(tmp_path)


def test_token_binding_cannot_change_another_threads_transaction(tmp_path):
    import threading
    from app.services import schwab_token_store as tokens
    entered, release = threading.Event(), threading.Event()
    def owner():
        with tokens._TOKEN_CACHE_THREAD_LOCK:
            entered.set()
            release.wait(5)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="active token-cache"):
            tokens.configure_local_repository(tmp_path)
    finally:
        release.set()
        thread.join(timeout=5)
