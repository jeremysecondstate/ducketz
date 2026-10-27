"""Offline lane boundaries: isolated stores, fake transports, no provider calls."""
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import json
import ast
from pathlib import Path
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import pandas as pd

_adapter = Path(__file__).resolve().parents[1] / "ml/intraday/live_session.py"
_definitions = ast.parse(_adapter.read_bytes()).body if _adapter.is_file() else []
if not {"initialize_renewal_session", "run_renewal_lane"} <= {
        node.name for node in _definitions if isinstance(node, ast.FunctionDef)}:
    pytest.skip("Installed 15-minute renewal adapter required; apply the reviewed compatibility patch",
                allow_module_level=True)

from ml.intraday import live_session
from ml.intraday.prediction import instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Minimal fake broker around the actual independent execution ledgers."""
    from test_independent_stock_runtime import Broker, ACCOUNT, BROKER_ID, SYMBOLS
    from test_intraday_starter_sizing import fixture as sizing_fixture
    from ml.intraday import account_execution as execution, cancellation
    from ml.intraday.local_execution import LocalExecutionBinding
    from ml.intraday.prediction import digest
    from ml.stock_trader.contracts import PortfolioState, QuoteState
    from ml.stock_trader.horizon_broker import OrderHistoryError
    from ml.stock_trader.horizon_ledger import OrderEvidence, FillEvidence
    original, _, _, _ = sizing_fixture()
    state = SimpleNamespace(root=tmp_path, now=pd.Timestamp(original["issued_at"]), cash=100.,
        held=dict.fromkeys(SYMBOLS, 0.), broker=Broker(), orders={})
    state.config = LocalExecutionBinding("Atlas", "pc-original", tuple(SYMBOLS[:4]), ACCOUNT, "e" * 64)
    state.store = ShadowStore(tmp_path / "intraday.sqlite3")
    state.session_started_at = state.now - pd.Timedelta(seconds=30)
    state.clock = lambda: state.now.isoformat()
    monkeypatch.setattr(execution, "load_local_binding", lambda _: state.config)

    def observe(broker, ledger, **kwargs):
        state.now += pd.Timedelta(milliseconds=1)
        rows = []
        for reservation in ledger.pending_reservations():
            if not reservation.broker_order_id:
                raise OrderHistoryError("UNRESOLVED_SUBMISSION_WITHOUT_EXACT_BROKER_ORDER_ID")
            row = state.orders[reservation.broker_order_id]
            filled = row.get("filled", 0)
            fills = (() if not filled else (FillEvidence("fill-" + reservation.broker_order_id,
                filled, reservation.limit_price, row["fill_at"]),))
            rows.append(OrderEvidence(digest([reservation.reservation_id, row, state.now.isoformat()]),
                reservation.reservation_id, ACCOUNT, state.now.isoformat(), reservation.broker_order_id,
                row["status"], reservation.quantity, filled, reservation.quantity - filled, fills, row["status"]))
        return tuple(rows)
    monkeypatch.setattr(execution, "capture_order_evidence", observe)
    monkeypatch.setattr(cancellation, "capture_order_evidence", observe)

    def capture(broker, **kwargs):
        state.now += pd.Timedelta(milliseconds=1)
        working, pending_buys = [], {}
        for order_id, row in state.orders.items():
            left = row["quantity"] - row.get("filled", 0)
            working.append(dict(status="CURRENT", asset_type="EQUITY", instruction=row["side"],
                order_id=order_id, symbol=row["symbol"], remaining_quantity=left,
                filled_quantity=row.get("filled", 0), limit_price=row["price"],
                reserved_cash=str(Decimal(row["price"]) * left)))
            pending_buys[row["symbol"]] = pending_buys.get(row["symbol"], 0) + left
        observed = state.now.isoformat()
        return PortfolioState(observed, 15_000., state.cash, sum(state.held.values()) * 50, 0., dict(state.held),
            {s: state.held[s] * 50 for s in SYMBOLS}, pending_buys, {}, len(working),
            {s: QuoteState(s, 50., 50., 50., 50., 1000., observed) for s in SYMBOLS},
            digest([observed, state.cash, state.held, working]), BROKER_ID, tuple(working))
    monkeypatch.setattr(execution, "capture_portfolio_state", capture)
    submit = state.broker.submit_prepared_order

    def fake_post(payload, context, *, before_post):
        location = submit(payload, context, before_post=before_post)
        broker_id = str(location).rsplit("/", 1)[-1]
        leg = payload["orderLegCollection"][0]
        state.orders[broker_id] = {"symbol": leg["instrument"]["symbol"], "side": leg["instruction"],
            "quantity": leg["quantity"], "price": payload["price"], "status": "WORKING"}
        return location
    state.broker.submit_prepared_order = fake_post

    def frozen_forecast():
        raw, snapshot, _, _ = sizing_fixture()
        cycle_id = digest([raw["symbol"], raw["reference_at"], "renewal-fixture"])
        state.store.begin_cycle(cycle_id, {"pc": "Atlas", "symbols": [raw["symbol"]],
            "reference_at": raw["reference_at"], "mode": "live", "configuration_hash": "fixture-only",
            "started_at": raw["issued_at"]})
        frozen_hash, _ = state.store.freeze_inputs(cycle_id, raw["symbol"], snapshot)
        raw.update(pc="Atlas", cycle_id=cycle_id, mode="live", availability_basis="observed",
            information_available_at=snapshot["candles"][-1]["available_at"], snapshot_hash=frozen_hash,
            configured_count=10, successful_count=10, ensemble_status="full",
            models=[{"name": "fixture-model-" + str(i), "status": "success",
                     "probability_not_down": .72, "version": "fixture-only-no-training"} for i in range(10)])
        return state.store.save_forecast(raw)
    state.forecast = frozen_forecast
    return state


def native(env):
    from ml.intraday.account_execution import NATIVE_PATH
    from ml.stock_trader.horizon_ledger import HorizonLedger
    from test_independent_stock_runtime import ACCOUNT
    return HorizonLedger(env.root / NATIVE_PATH, ACCOUNT)


def account(env):
    from ml.account_gameplan.authority import AccountAuthority, AuthorityBinding
    from ml.intraday.account_execution import AUTHORITY_PATH
    from test_independent_stock_runtime import ACCOUNT
    return AccountAuthority(env.root / AUTHORITY_PATH,
                            AuthorityBinding(ACCOUNT, env.config.coordinator_id, env.config.participants))


def advance(env, seconds):
    env.now += pd.Timedelta(seconds=seconds)


class Renewed(BaseException):
    pass


class Renewal:
    def __init__(self, session=None, state=None):
        self.session = session or dict(session_id="manual-atlas-15m:fixture", actor="Atlas",
            symbols=["AAPL", "AMZN"], started_at="2026-10-08T17:16:00+00:00",
            close_at="2026-10-09T00:00:00+00:00", config=asdict(live_session.ResearchConfig()),
            binding_fingerprint="fixture-binding")
        self.state = state or {}
        self.stop = False
        self.saved = None
        self.waits = 0

    def stopped(self):
        return self.stop

    def wait_due(self, **kwargs):
        self.waits += 1

    def checkpoint(self, state, next_poll_at):
        assert not self.stop
        self.saved = (state, next_poll_at)
        raise Renewed()


def test_complete_execution_sweep_renews_with_one_session_and_recovery_deadline(tmp_path):
    now = instant("2026-10-08T17:17:00Z")
    renewal = Renewal()
    original_session = json.dumps(renewal.session, sort_keys=True)
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    queue = live_session.ExecutionQueue(store, renewal.session["session_id"])
    for symbol in renewal.session["symbols"]:
        queue.enqueue(store.save_forecast(forecast(mode="live", symbol=symbol)))
    events = []
    executor = SimpleNamespace(step=lambda f: events.append(f["symbol"]) or dict(
        disposition="submitted", evidence={}, kind="execution_disposition"),
        recover=lambda: events.append("recover"))
    with pytest.raises(Renewed):
        live_session._renewal_execution_pass(store=store, queue=queue, executor=executor,
            renewal=renewal, clock=lambda: now)
    assert events == ["AAPL", "AMZN", "recover"]
    assert renewal.saved == ({"next_recovery": (now + timedelta(seconds=3)).isoformat()},
                             (now + timedelta(seconds=1)).isoformat())
    successor = Renewal(session=renewal.session, state=renewal.saved[0])
    with pytest.raises(Renewed):
        live_session._renewal_execution_pass(store=ShadowStore(store.path),
            queue=live_session.ExecutionQueue(ShadowStore(store.path), renewal.session["session_id"]),
            executor=executor, renewal=successor, clock=lambda: now + timedelta(seconds=1))
    assert events == ["AAPL", "AMZN", "recover"]
    assert json.dumps(successor.session, sort_keys=True) == original_session
    assert successor.saved[0] == renewal.saved[0]


@pytest.mark.parametrize("disposition", ["submission_recovery_required", "previous_intention_reconciliation"])
def test_unknown_outcomes_keep_durable_queue_and_do_not_resubmit_on_renewal(tmp_path, disposition):
    now = instant("2026-10-08T17:17:00Z")
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    value = store.save_forecast(forecast(mode="live"))
    queue = live_session.ExecutionQueue(store, renewal.session["session_id"])
    queue.enqueue(value)
    posts = []
    executor = SimpleNamespace(step=lambda f: posts.append(f["forecast_id"]) or dict(
        disposition=disposition, kind="execution_disposition",
        evidence={"reason_code": "EXACT_BROKER_ORDER_ID_UNAVAILABLE"}), recover=lambda: None)
    with pytest.raises(Renewed):
        live_session._renewal_execution_pass(store=store, queue=queue, executor=executor,
            renewal=renewal, clock=lambda: now)
    successor = Renewal(session=renewal.session, state=renewal.saved[0])
    with pytest.raises(Renewed):
        live_session._renewal_execution_pass(store=store,
            queue=live_session.ExecutionQueue(ShadowStore(store.path), renewal.session["session_id"]),
            executor=executor, renewal=successor, clock=lambda: now + timedelta(seconds=1))
    assert posts == [value["forecast_id"]]
    assert store.forecast(value["cycle_id"], value["symbol"]) == value
    with store.connect() as db:
        status = db.execute("SELECT status FROM intraday_live_queue").fetchone()[0]
    assert status == ("submission_recovery_required" if disposition == "submission_recovery_required" else "pending")


def test_stop_within_sweep_prevents_later_symbol_recovery_and_renewal(tmp_path):
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    queue = live_session.ExecutionQueue(store, renewal.session["session_id"])
    for symbol in renewal.session["symbols"]:
        queue.enqueue(store.save_forecast(forecast(mode="live", symbol=symbol)))
    calls = []
    def step(value):
        calls.append(value["symbol"])
        renewal.stop = True
        return dict(disposition="submitted", kind="execution_disposition", evidence={})
    result = live_session._renewal_execution_pass(store=store, queue=queue,
        executor=SimpleNamespace(step=step, recover=lambda: pytest.fail("recovered after stop")),
        renewal=renewal, clock=lambda: "2026-10-08T17:17:00Z")
    assert result == {"status": "STOPPED", "role": "execution"}
    assert calls == ["AAPL"] and renewal.saved is None


def test_execution_completion_never_renews_or_reaches_executor(tmp_path):
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    result = live_session._renewal_execution_pass(store=store,
        queue=live_session.ExecutionQueue(store, renewal.session["session_id"]), executor=object(),
        renewal=renewal, clock=lambda: renewal.session["close_at"])
    assert result == {"status": "COMPLETED", "role": "execution"} and renewal.saved is None


def test_forecasting_cursor_and_fairness_survive_a_fresh_worker_without_recompute(tmp_path, monkeypatch):
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    calls = []
    now = instant("2026-10-08T17:17:00Z")
    config = live_session.ResearchConfig()
    def cycle(incoming, **kwargs):
        calls.append(kwargs)
        store.begin_cycle("cycle", dict(mode="live", pc="Atlas", experiment=renewal.session["session_id"],
            reference_at=kwargs["reference_at"].isoformat(), symbols=renewal.session["symbols"],
            started_at=now.isoformat(), status="completed"))
        return {"cycle_id": "cycle"}
    runner = SimpleNamespace(store=store, config=config, run_cycle=cycle)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: object())
    queue = live_session.ExecutionQueue(store, renewal.session["session_id"])
    with pytest.raises(Renewed):
        live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(),
            queue=queue, stopped=renewal.stopped, clock=lambda: now, renewal=renewal)
    assert len(calls) == 1 and calls[0]["retry_symbols"] is None
    assert renewal.saved[0] == {"last_reference": "2026-10-08T17:00:00+00:00", "recovery_due": True}
    successor = Renewal(session=renewal.session, state=renewal.saved[0])
    with pytest.raises(Renewed):
        live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(),
            queue=queue, stopped=successor.stopped, clock=lambda: now, renewal=successor)
    assert len(calls) == 1


def test_scoring_full_pass_renews_without_final_session_scoring(tmp_path, monkeypatch):
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    monkeypatch.setattr(live_session, "score_saved_session_outcomes", lambda *a, **k: pytest.fail("closeout on renewal"))
    with pytest.raises(Renewed):
        live_session.scoring_loop(store, histories={}, provider=object(), journal=object(),
            session_id=renewal.session["session_id"], stopped=renewal.stopped,
            clock=lambda: "2026-10-08T17:17:00Z", renewal=renewal)
    assert renewal.saved == ({"audited": []}, "2026-10-08T17:17:03+00:00")


@pytest.mark.parametrize("complete", [False, True])
def test_candle_full_pass_closes_out_only_once_at_natural_end(tmp_path, monkeypatch, complete):
    from ml.intraday import candle_runtime
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    calls = []
    class Collector:
        def __init__(self, *args, **kwargs):
            assert kwargs["started_at"] == instant(renewal.session["started_at"])
            assert kwargs["symbols"] == ["AAPL", "AMZN"]
        def _storage(self, operation, action):
            return action()
        def pass_once(self):
            calls.append("all-symbol-collection-pass")
            return {"final_complete": complete}
    monkeypatch.setattr(candle_runtime, "CandleCollector", Collector)
    monkeypatch.setattr(live_session, "score_saved_session_outcomes", lambda *a, **k: calls.append("closeout") or {})
    arguments = dict(store=store, histories={}, provider=object(), journal=object(), renewal=renewal,
        config=live_session.ResearchConfig(), clock=lambda: "2026-10-09T00:00:00Z", pause=lambda s: None)
    if complete:
        result = candle_runtime.renewal_candle_pass(**arguments)
        assert result["final_collection_complete"] is True and renewal.saved is None
        assert calls == ["all-symbol-collection-pass", "closeout"]
    else:
        with pytest.raises(Renewed):
            candle_runtime.renewal_candle_pass(**arguments)
        assert calls == ["all-symbol-collection-pass"]


def test_market_input_quarter_iteration_preserves_retry_subpasses_and_pending_reference(tmp_path, monkeypatch):
    from ml.intraday import market_inputs, market_input_runtime
    renewal = Renewal()
    now = [instant("2026-10-08T17:30:28Z")]
    events = []
    class Collector:
        def __init__(self, *a, **k):
            pass
        def start(self):
            events.append("start")
            return {}
        def collect(self, reference):
            events.append(("collect", reference.isoformat()))
            raise RuntimeError("fixture failed source collection")
        def recover_pending(self):
            events.append("recover")
        def close(self):
            events.append("close")
    monkeypatch.setattr(market_inputs, "MarketInputCollector", Collector)
    with pytest.raises(Renewed):
        market_input_runtime.renewal_market_input_pass(root=tmp_path, symbols=renewal.session["symbols"],
            store=ShadowStore(tmp_path / "shadow.sqlite3"), renewal=renewal,
            clock=lambda: now[0], config=live_session.ResearchConfig(),
            sleep=lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)))
    assert events[:3] == ["start", ("collect", "2026-10-08T17:00:00+00:00"), "recover"]
    assert events.count("start") == 1
    assert renewal.saved[0]["pending_references"] == ["2026-10-08T17:00:00+00:00"]
    assert renewal.saved[1] == "2026-10-08T17:30:30+00:00"


def test_session_initialization_freezes_scope_and_config_without_broker(tmp_path, monkeypatch):
    from ml.intraday import local_execution
    from app.services import schwab
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL", "AMZN")))
    monkeypatch.setattr(local_execution, "load_local_binding", lambda *a: SimpleNamespace(
        actor="Atlas", symbols=("AAPL", "AMZN"), fingerprint="fixture-binding"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: pytest.fail("broker opened during initialization"))
    session = live_session.initialize_renewal_session(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile", watchlist=tmp_path / "watchlist",
        config=live_session.ResearchConfig(symbol_workers=2), clock=lambda: "2026-10-08T17:17:00Z")
    assert session["close_at"] == "2026-10-09T00:00:00+00:00"
    assert session["symbols"] == ["AAPL", "AMZN"]
    assert session["config"]["symbol_workers"] == 2


@pytest.mark.parametrize("unknown", [False, True])
def test_fresh_executors_reconcile_original_orders_and_partial_fills_without_reposting(env, unknown):
    from ml.intraday.account_execution import IntradayAccountExecutor
    from ml.intraday.candle_collection import market_close_at

    renewal = Renewal()
    renewal.session.update(started_at=env.session_started_at.isoformat(),
        close_at=market_close_at(env.session_started_at).isoformat(), symbols=list(env.config.symbols))
    value = env.forecast()
    original = json.dumps(value, sort_keys=True)
    env.store.update_cycle(value["cycle_id"], {"experiment": renewal.session["session_id"]})
    queue = live_session.ExecutionQueue(env.store, renewal.session["session_id"])
    queue.enqueue(value)
    env.broker.unknown_submission = unknown
    generations = []
    for generation in range(3):
        executor = IntradayAccountExecutor(datastore=env.root, store=ShadowStore(env.store.path),
            broker=env.broker, local_symbols=env.config.symbols, session_started_at=env.session_started_at,
            clock=env.clock, manual_authority=lambda: not renewal.stopped(), stopped=renewal.stopped)
        generations.append(executor)
        with pytest.raises(Renewed):
            live_session._renewal_execution_pass(store=env.store, queue=queue, executor=executor,
                renewal=renewal, clock=env.clock)
        reservation = native(env).snapshot().reservations[0]
        if generation == 0:
            identity = reservation.reservation_id
            broker_identity = reservation.broker_order_id
            if not unknown:
                env.orders[broker_identity].update(status="PARTIAL", filled=1, fill_at=env.clock())
                env.held["AAPL"], env.cash = 1., 50.
        elif generation == 1 and not unknown:
            assert reservation.filled_quantity == 1 and reservation.status == "PARTIAL"
        assert reservation.reservation_id == identity
        assert reservation.broker_order_id == broker_identity
        renewal = Renewal(session=renewal.session, state=renewal.saved[0])
        advance(env, seconds=4)
    assert len({id(executor) for executor in generations}) == 3
    assert len(env.broker.submissions) == 1 and env.broker.cancellations == []
    assert json.dumps(env.store.forecast(value["cycle_id"], value["symbol"]), sort_keys=True) == original
    assert native(env).snapshot().reservations[0].status == ("UNKNOWN" if unknown else "PARTIAL")
    own = account(env).read_state()["reservations"][0]
    assert own["status"] == ("UNKNOWN" if unknown else "PARTIAL")


def test_optional_market_input_failure_does_not_stop_core_or_loop_restarts(tmp_path, monkeypatch):
    import dotenv
    from ml.intraday import local_execution, market_input_runtime
    renewal = Renewal()
    renewal.session["config"]["market_inputs_enabled"] = True
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL", "AMZN")))
    monkeypatch.setattr(local_execution, "load_local_binding", lambda *a: SimpleNamespace(
        actor="Atlas", symbols=("AAPL", "AMZN"), fingerprint="fixture-binding"))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    def fail(**kwargs):
        raise RuntimeError("fixture optional stream setup failure")
    monkeypatch.setattr(market_input_runtime, "renewal_market_input_pass", fail)
    result = live_session.run_renewal_lane("market_inputs", repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile", watchlist=tmp_path / "watchlist", renewal=renewal,
        clock=lambda: "2026-10-08T17:17:00Z")
    assert result["status"] == "COMPLETED" and result["optional_input_status"] == "FAILED"
    assert result["core_forecasts_continue"] is True and result["automatic_lane_restart"] is False
    assert not renewal.stopped() and renewal.saved is None


def test_changed_account_binding_cannot_enter_a_continuation(tmp_path, monkeypatch):
    from ml.intraday import local_execution
    from app.services import schwab
    renewal = Renewal()
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL", "AMZN")))
    monkeypatch.setattr(local_execution, "load_local_binding", lambda *a: SimpleNamespace(
        actor="Atlas", symbols=("AAPL", "AMZN"), fingerprint="different-account-binding"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: pytest.fail("broker opened after account drift"))
    with pytest.raises(live_session.ContractError, match="membership changed"):
        live_session.run_renewal_lane("execution", repository=tmp_path, datastore=tmp_path,
            profile=tmp_path / "profile", watchlist=tmp_path / "watchlist", renewal=renewal,
            clock=lambda: "2026-10-08T17:17:00Z")


def test_renewal_history_loading_is_lazy_and_keeps_original_session_cutoff(tmp_path, monkeypatch):
    from dataclasses import replace
    from ml.intraday import data
    from ml.intraday.coverage import RECOVERY_POPULATION_CONTRACT
    renewal = Renewal()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    calls = []
    def load(root, symbol, **kwargs):
        calls.append((symbol, kwargs))
        return SimpleNamespace(manifest={"population_contract": RECOVERY_POPULATION_CONTRACT})
    monkeypatch.setattr(data, "load_local_history", load)
    config = replace(live_session.ResearchConfig(), population_contract=RECOVERY_POPULATION_CONTRACT)
    histories = live_session._renewal_histories(tmp_path, store, renewal.session, config)
    assert calls == []
    first = histories.get("AAPL")
    assert histories["AAPL"] is first and "AAPL" in histories
    assert [symbol for symbol, _ in calls] == ["AAPL"]
    assert calls[0][1]["recovery_cutoff"] == instant(renewal.session["started_at"])
    assert histories.get("UNCONFIGURED") is None
    assert [symbol for symbol, _ in calls] == ["AAPL"]
    assert histories.get("AMZN") is not None
    assert [symbol for symbol, _ in calls] == ["AAPL", "AMZN"]


def _subprocess_turn(root, role, now):
    script = Path(__file__).with_name("intraday_renewal_subprocess.py")
    result = subprocess.run([sys.executable, "-B", str(script), str(root), role, now],
        cwd=script.parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                  "DUCKETZ_TRADER_RENEWAL_REQUEST": ""},
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((root / "turn-result.json").read_text())


def test_actual_scientific_adapter_uses_fresh_processes_without_retraining_saved_cycles(tmp_path):
    from ml.intraday.local_execution import validate_binding
    root = tmp_path
    (root / "OFFLINE-RENEWAL-FIXTURE").touch()
    raw_binding = dict(schema_version="independent-local-15m-binding-v1", actor="Atlas", machine="pc-original",
        symbols=["AAPL", "IONQ"], account_fingerprint="e" * 64, manual_start_only=True,
        legacy_history_policy="PRESERVE_ORIGINALS_NEW_BROKER_CAPACITY_EPOCH")
    binding_path = root / "intraday/execution/config.json"
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(json.dumps(raw_binding))
    session = dict(session_id="manual-atlas-15m:subprocess-science", actor="Atlas", symbols=raw_binding["symbols"],
        started_at="2026-10-08T17:16:00+00:00", close_at="2026-10-09T00:00:00+00:00",
        binding_fingerprint=validate_binding(raw_binding).fingerprint,
        config=asdict(live_session.ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=2)))
    (root / "session.json").write_text(json.dumps(session))
    turns = []
    original_forecasts = None
    for now in ("2026-10-08T17:17:00Z", "2026-10-08T17:17:01Z",
                "2026-10-08T17:32:00Z", "2026-10-08T17:32:01Z"):
        turns.append(_subprocess_turn(root, "forecast", now))
        store = ShadowStore(root / "intraday/shadow.sqlite3")
        if original_forecasts is None:
            original_forecasts = store.forecasts()
        else:
            assert all(store.forecast(f["cycle_id"], f["symbol"]) == f for f in original_forecasts)
    assert len({turn["pid"] for turn in turns}) == 4
    assert [turn["training_calls"] for turn in turns] == [2, 2, 4, 4]
    assert [turn["counts"] for turn in turns] == [
        {"cycles": 1, "forecasts": 2}, {"cycles": 1, "forecasts": 2},
        {"cycles": 2, "forecasts": 4}, {"cycles": 2, "forecasts": 4}]
    assert all(turn["session_id"] == session["session_id"] and turn["started_at"] == session["started_at"]
               and turn["close_at"] == session["close_at"] for turn in turns)
    assert all(turn["result"]["status"] == "PLANNED_RENEWAL" for turn in turns)
    (root / "manual-stop").touch()
    stopped = _subprocess_turn(root, "forecast", "2026-10-08T17:47:00Z")
    assert stopped["result"]["status"] == "STOPPED" and stopped["training_calls"] == 4


@pytest.mark.parametrize("unknown", [False, True])
def test_actual_execution_adapter_preserves_orders_across_fresh_processes(tmp_path, unknown):
    from test_independent_stock_runtime import SYMBOLS, ACCOUNT
    from ml.stock_trader.horizon_ledger import HorizonLedger
    from ml.intraday.local_execution import NATIVE_PATH

    root = tmp_path
    (root / "OFFLINE-RENEWAL-FIXTURE").touch()
    session = dict(session_id="manual-atlas-15m:subprocess-orders", actor="Atlas", symbols=list(SYMBOLS[:4]),
        started_at="2026-10-08T18:00:00+00:00", close_at="2026-10-09T00:00:00+00:00",
        binding_fingerprint="e" * 64, fixture_unknown=unknown, config=asdict(live_session.ResearchConfig()))
    (root / "session.json").write_text(json.dumps(session))
    turns = []
    for index, now in enumerate(("2026-10-08T18:00:04Z", "2026-10-08T18:00:08Z", "2026-10-08T18:00:12Z")):
        turns.append(_subprocess_turn(root, "execution", now))
        saved = json.loads((root / "fixture-broker.json").read_text())
        reservation = HorizonLedger(root / NATIVE_PATH, ACCOUNT).snapshot().reservations[0]
        if index == 0:
            identity = (reservation.reservation_id, reservation.broker_order_id)
            if not unknown:
                saved["orders"][reservation.broker_order_id].update(status="PARTIAL", filled=1, fill_at=saved["now"])
                saved["held"]["AAPL"], saved["cash"] = 1., 50.
                (root / "fixture-broker.json").write_text(json.dumps(saved))
        assert (reservation.reservation_id, reservation.broker_order_id) == identity
        assert len(saved["submissions"]) == 1
    assert len({turn["pid"] for turn in turns}) == 3
    assert all(turn["result"]["status"] == "PLANNED_RENEWAL" for turn in turns)
    assert reservation.status == ("UNKNOWN" if unknown else "PARTIAL")
    assert reservation.filled_quantity == (0 if unknown else 1)
