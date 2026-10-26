"""Canonical Atlas execution imports, manual independence and frozen projections."""
import ast
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_atlas_manual_executor_is_present():
    assert importlib.util.find_spec("ml.intraday.account_execution") is not None
    from ml.intraday.account_execution import IntradayAccountExecutor
    assert IntradayAccountExecutor.__module__ == "ml.intraday.account_execution"


def test_atlas_manual_start_has_no_gameplan_compatibility_dependency():
    source = (ROOT / "ml/intraday/live_session.py").read_text(encoding="utf8")
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "run_manual")
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "runtime_compatibility" for n in ast.walk(function))
    assert not any(isinstance(n, ast.Name) and n.id == "compatibility" for n in ast.walk(function))
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "IntradayAccountExecutor"]
    assert len(calls) == 1
    arguments = {item.arg: item.value for item in calls[0].keywords}
    assert "compatibility" not in arguments
    assert ast.unparse(arguments["actor"]) == "pc"
    assert ast.unparse(arguments["diagnostic_session_id"]) == "session_id"


def test_constructor_is_broker_free_and_uses_own_store(tmp_path):
    from ml.intraday.account_execution import IntradayAccountExecutor
    from ml.intraday.store import ShadowStore
    class ForbiddenBroker:
        def __getattribute__(self, name):
            raise AssertionError("Construction must not consult the broker")
    store = ShadowStore(tmp_path / "own.sqlite3")
    executor = IntradayAccountExecutor(datastore=tmp_path, store=store,
        broker=ForbiddenBroker(), local_symbols=("AAPL",), session_started_at="2026-10-08T18:00:00Z",
        clock=lambda: "2026-10-08T18:00:01Z", manual_authority=lambda: False)
    assert executor.actor == "Atlas" and executor.stream == "atlas-15m"
    assert executor.config is executor.ledger is executor.authority is None
    assert executor.store.path == store.path
    assert "compatibility" not in inspect.signature(IntradayAccountExecutor).parameters


def test_native_and_intention_adapters_preserve_canonical_base_classes():
    from ml.intraday.native_ledger import IntradayLedger
    from ml.intraday.execution_intentions import IntentionStore
    from ml.intraday.intentions import IntentionStore as OriginalIntentionStore
    from ml.stock_trader.horizon_ledger import HorizonLedger
    assert IntradayLedger.__bases__ == (HorizonLedger,)
    assert IntentionStore.__bases__ == (OriginalIntentionStore,)
    for name in ("_original_action", "save_sizing", "processed_at_or_after", "history"):
        assert getattr(IntentionStore, name) is getattr(OriginalIntentionStore, name)


def test_operating_paths_are_only_this_traders_own_namespace():
    from ml.intraday import local_execution
    assert str(local_execution.NATIVE_PATH).replace("\\", "/") == "intraday/execution/holdings.sqlite3"
    assert str(local_execution.AUTHORITY_PATH).replace("\\", "/") == "intraday/execution/authority.sqlite3"
    assert str(local_execution.LOCK_PATH).replace("\\", "/") == "locks/intraday-execution.lock"


def test_fake_manual_atlas_caller_constructs_own_executor_without_gameplan_or_workers(tmp_path, monkeypatch):
    import builtins
    import importlib.util
    import dotenv
    from ml.intraday import account_execution, data, live_model_recipe, live_session, minute_feed
    from app.services import schwab
    captured, threads, calls = [], [], []
    now = "2026-10-14T22:31:00Z"
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(live_session, "manual_wake_at", lambda t: live_session.instant(t))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(data, "load_local_history", lambda *a, **k: object())
    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", lambda config, **k: (config, {}))
    monkeypatch.setattr(live_model_recipe, "native_interval_symbols_for_runtime", lambda runtime: ())
    monkeypatch.setattr(live_session, "prepare_minute_provider", lambda *a, **k: object())
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *a, **k: None)
    monkeypatch.setattr(live_session, "ShadowRunner", lambda **k: object())
    monkeypatch.setattr(schwab, "SchwabSession", lambda: SimpleNamespace(offline_fixture=True))
    class FakeFeed:
        history_source = scoring_history_source = None
        def __init__(self, **kwargs):
            pass
    class FakeExecutor:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            assert kwargs["broker"].offline_fixture
            assert "compatibility" not in kwargs
        def step(self, forecast):
            pytest.fail("No fixture forecast may create a submission")
        def recover(self):
            calls.append("own_recovery")
    class NoThread:
        def __init__(self, *, target, name, **kwargs):
            threads.append(name)
        def start(self):
            pass
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pass
    def finish(seconds):
        raise KeyboardInterrupt()
    original_import = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = importlib.util.resolve_name("." * level + name, globals["__package__"]) if level else name
        assert resolved != "ml.intraday.runtime_compatibility"
        assert resolved not in {"ml.intraday.scout_execution", "ml.intraday.scout_local_execution"}
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(account_execution, "IntradayAccountExecutor", FakeExecutor)
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", FakeFeed)
    monkeypatch.setattr(live_session.threading, "Thread", NoThread)
    monkeypatch.setattr(builtins, "__import__", guarded)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile.json", watchlist=tmp_path / "watchlist.txt",
        config=live_session.ResearchConfig(), clock=lambda: now, sleep=finish)
    assert result["status"] == "STOPPED" and len(captured) == 1
    assert captured[0]["actor"] == "Atlas" and captured[0]["diagnostic_session_id"] == result["session_id"]
    assert captured[0]["local_symbols"] == ("AAPL",) and calls == ["own_recovery"]
    assert threads == ["intraday-manual-forecast", "intraday-manual-scoring", "intraday-manual-minute-data"]
    assert captured[0]["manual_authority"]() is False


from test_intraday_main_account_execution import env, advance


def test_legacy_unscoped_event_replay_keeps_exact_identity_and_metadata(env):
    from ml.intraday.intentions import IntentionStore as OriginalIntentionStore
    from ml.intraday.prediction import digest
    forecast = env.forecast()
    arguments = dict(event_key="original-event", recorded_at=env.now, disposition="own-observation",
                     evidence={"reason": "offline-original"}, kind="execution_disposition")
    original = OriginalIntentionStore(env.store).record(forecast, **arguments)
    replay = env.executor.intents.record(forecast, **arguments)
    assert replay == original
    assert original["event_identity"] == digest([forecast["forecast_id"], "original-event"])
    assert original["stock_action"] == "BUY"
    assert original["direction_policy_version"] == forecast["direction_policy_version"]


def test_sized_event_scope_uses_first_intention_and_preserves_original_side(env):
    from ml.intraday.prediction import digest
    forecast = env.forecast()
    env.executor.step(forecast)
    intention = env.executor.intents.get(forecast["forecast_id"])
    advance(env)
    arguments = dict(event_key="new-execution", recorded_at=env.now, disposition="own-observation",
                     evidence={"reason": "offline-scoped"}, kind="execution_disposition", scope_to_intention=True)
    event = env.executor.intents.record(forecast, **arguments)
    assert event["event_identity"] == digest([forecast["forecast_id"], "new-execution", {"intention_id": digest(intention)}])
    assert env.executor.intents.record(forecast, **arguments) == event
    assert event["side"] == intention["side"] == event["stock_action"] == "BUY"


def test_grouped_recovery_preserves_frozen_direction_and_exact_single_order(env):
    forecast = env.forecast()
    env.executor.step(forecast)
    original = env.store.forecasts()[0]
    identity = env.executor.ledger.snapshot().reservations[0].reservation_id
    advance(env)
    env.executor.recover()
    history = env.executor.intents.history(forecast["forecast_id"])
    assert env.store.forecasts()[0] == original
    assert len(env.broker.submissions) == 1
    assert env.executor.ledger.snapshot().reservations[0].reservation_id == identity
    for event in history:
        assert event["side"] == "BUY" and event["stock_action"] == forecast["stock_action"]
        assert event["direction_policy_version"] == forecast["direction_policy_version"]


def test_missing_original_intention_is_not_fabricated(env):
    from ml.intraday.prediction import ContractError
    forecast = env.forecast()
    with pytest.raises(ContractError, match="original immutable intention"):
        env.executor.intents.record(forecast, event_key="requires-sizing", recorded_at=env.now,
            disposition="own-observation", evidence={"reason": "offline-only"}, kind="execution_disposition",
            require_original_intention=True)


@pytest.mark.parametrize("recorded_policy", [True, False])
def test_recovery_bridge_keeps_historical_buy_and_absent_policy_unrecorded(env, recorded_policy):
    import json
    from ml.intraday.direction_metadata import FIELDS
    from ml.intraday.prediction import digest, encoded
    current = env.forecast(probability=.50005)
    original = {key: value for key, value in current.items() if key not in {"forecast_id", *FIELDS}}
    if recorded_policy:
        original.update(stock_action="BUY", direction_policy_version="stock-direction-50-v2",
                        direction_up_threshold=.5, direction_down_threshold=.5)
    original["forecast_id"] = digest(original)
    # Reconstruct a historical immutable fixture before the executor reads it;
    # this is no production mutation or current-policy relabeling operation.
    with env.store.connect() as db:
        key, raw = db.execute("SELECT id,payload FROM intents WHERE forecast_id=? ORDER BY rowid LIMIT 1",
                             (current["forecast_id"],)).fetchone()
        intent = {k: v for k, v in json.loads(raw).items() if k not in FIELDS}
        intent.update(forecast_id=original["forecast_id"], side="BUY")
        if recorded_policy:
            intent.update({k: original[k] for k in FIELDS})
        db.execute("UPDATE forecasts SET id=?,payload=? WHERE id=?",
                   (original["forecast_id"], encoded(original), current["forecast_id"]))
        db.execute("UPDATE intents SET id=?,forecast_id=?,payload=? WHERE id=?",
                   (digest(intent), original["forecast_id"], encoded(intent), key))
        row = db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE id=?",
                         (original["forecast_id"],)).fetchone()
        header, _ = env.store._forecast_projection(row)
        result = env.executor.intents._projection_direction(db, header)
        side = env.executor.intents._original_action(db, header)
    assert side == "BUY"
    assert result == ({k: original[k] for k in FIELDS} if recorded_policy else {})
    assert env.store.forecast_by_id(original["forecast_id"]) == original
