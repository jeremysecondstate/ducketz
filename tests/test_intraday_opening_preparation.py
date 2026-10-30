"""Opening preparation/adoption with real immutable stores and fake model fits."""
from dataclasses import asdict, replace
from datetime import timedelta
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

# The public package carries owned deltas, not another PC's complete composed
# runtime. Installed-generation validation supplies these actual adapters.
_ROOT = Path(__file__).resolve().parents[1]
_REQUIRED = ("runtime.py", "prediction.py", "store.py", "account_execution.py", "split_handoff.py", "split_model.py")
if any(not (_ROOT / "ml/intraday" / name).is_file() for name in _REQUIRED):
    pytest.skip("Opening integration requires the reviewed complete local 15-minute installation", allow_module_level=True)

from ml.intraday import opening_preparation as opening
from ml.intraday.account_execution import forecast_disposition
from ml.intraday.prediction import STEP, ContractError, digest, instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.split_handoff import ReadySnapshots
from ml.intraday.split_model import model_pass
from ml.intraday.store import ShadowStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Opening preparation tests must not use providers or brokers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def environment(tmp_path):
    env = SimpleNamespace(root=tmp_path, now=instant("2026-10-29T10:46:00Z"), fits=[], outputs=[])
    env.reference = instant("2026-10-29T10:30:00Z")
    env.target = env.reference + 2 * STEP
    env.clock = lambda: env.now
    env.symbols = ("AAPL", "IONQ")
    env.config = ResearchConfig(models=("fixture",), symbol_workers=2)
    env.data_store = ShadowStore(tmp_path / "intraday/system-1a/preparation.sqlite3")
    env.handoff = ReadySnapshots(tmp_path / "intraday/system-1a/ready", actor="Atlas", symbols=env.symbols, clock=env.clock)
    def dataset(frame, *, cutoff, reference_at, **kwargs):
        return SimpleNamespace(X=np.asarray([[0], [1], [0], [1]]), y=np.asarray([0, 1, 0, 1]),
            inference_X=np.asarray([[1]]), feature_names=("x",),
            label_available_at=pd.date_range(reference_at-timedelta(hours=2), periods=4, freq="15min"),
            reference_at=pd.date_range(reference_at-timedelta(hours=3), periods=4, freq="15min"),
            target_close_at=pd.date_range(reference_at-timedelta(hours=2, minutes=15), periods=4, freq="15min"),
            inference={"symbol": frame.iloc[0].symbol, "reference_at": reference_at.isoformat(),
                "reference_open": 100, "information_available_at": (reference_at+STEP).isoformat(),
                "availability_kind": "actual"}, manifest={"training_rows": 4})
    def trainer(*args, config, **kwargs):
        env.fits.append(kwargs)
        return {"outputs": [{"name": "fixture", "status": "success", "probability_not_down": .5001,
            "version": "offline", "training_seconds": 0, "inference_seconds": 0}],
            "configured_count": 1, "successful_count": 1, "complete": True,
            "probability_not_down": .5001, "training_seconds": 0, "inference_seconds": 0}
    env.trainer = trainer
    data = ShadowRunner(store=env.data_store, pc="Atlas", symbols=env.symbols, config=env.config,
        clock=env.clock, dataset_builder=dataset, trainer=lambda *a, **k: pytest.fail("1A fitted a model"))
    histories = {symbol: pd.DataFrame([{"symbol": symbol, "opened_at": env.reference,
        "available_at": env.reference + STEP, "availability_kind": "actual", "interval_seconds": 900,
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100, "synthetic": False}])
        for symbol in env.symbols}
    prepared = data.prepare_cycle(histories, reference_at=env.reference, experiment="system-1a-offline",
        on_prepared=lambda cid, symbol, cycle, frozen: env.handoff.publish(cid, symbol, cycle, frozen))
    assert prepared["status"] == "prepared"
    env.factory = lambda **kwargs: ShadowRunner(**kwargs, trainer=env.trainer,
        dataset_builder=lambda *a, **k: pytest.fail("Preparation rebuilt frozen inputs"))
    env.prepare = lambda **kwargs: opening.run_preparation(datastore=tmp_path, actor="Atlas", symbols=env.symbols,
        config=env.config, clock=env.clock, runner_factory=kwargs.pop("runner_factory", env.factory), **kwargs)
    env.store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    env.queue = SimpleNamespace(enqueue=env.outputs.append)
    env.session = {"actor": "Atlas", "symbols": list(env.symbols), "session_id": "manual-opening",
        "started_at": env.target.isoformat(), "close_at": (env.target+timedelta(hours=13)).isoformat()}
    env.adopt = lambda **kwargs: opening.adopt_opening(store=env.store, queue=env.queue,
        session=kwargs.pop("session", env.session), config=kwargs.pop("config", env.config), state=kwargs.pop("state", {}),
        clock=env.clock, stopped=kwargs.pop("stopped", lambda: False), assigned_symbols=env.symbols, **kwargs)
    return env


def test_preparation_fits_once_without_trader_or_execution_queue(tmp_path):
    env = environment(tmp_path)
    report = env.prepare()
    assert report["status"] == "PREPARED" and report["ready_before_open"]
    assert report["orders_placed"] == report["broker_calls"] == 0 and not report["trader_started"]
    assert len(env.fits) == 2 and env.outputs == [] and env.store.forecasts() == []
    source = ShadowStore(tmp_path / "intraday" / opening.CACHE_NAME)
    with source.connect() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='intraday_live_queue'").fetchone() is None
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "intraday/manual-session.json").exists()
    env.prepare()
    assert len(env.fits) == 2, "Repeated preparation refitted the same immutable cycle"


def test_opening_admission_preserves_all_original_science_and_original_issue_clock(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    originals = ShadowStore(tmp_path / "intraday" / opening.CACHE_NAME).forecasts()
    assert env.adopt() is None and not env.outputs
    env.now = env.target
    result = env.adopt()
    assert result["symbols"] == list(env.symbols) and len(env.fits) == 2
    for current in env.outputs:
        source = next(row for row in originals if row["symbol"] == current["symbol"])
        assert current["issued_at"] == source["issued_at"] < env.session["started_at"]
        assert current["opening_handoff"]["source_forecast_id"] == source["forecast_id"]
        assert {k: v for k, v in current.items() if k not in {"forecast_id", "cycle_id", "opening_handoff"}} == {
            k: v for k, v in source.items() if k not in {"forecast_id", "cycle_id"}}
        assert opening.opening_handoff_valid(current, session_started_at=env.session["started_at"])
        assert forecast_disposition(current, now=env.now, session_started_at=env.session["started_at"],
                                    local_symbols=env.symbols) == "eligible_for_account_checks"
    assert env.adopt(state=result["state"]) is None
    assert forecast_disposition(env.outputs[0], now=env.target-timedelta(seconds=1),
        session_started_at=env.session["started_at"], local_symbols=env.symbols) == "invalid_opening_handoff"


def test_prepared_opening_is_adopted_when_current_ready_pointer_has_advanced(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    env.now = env.target + timedelta(seconds=40)
    # Current scheduling would select03:45. Opening evidence still owns03:30.
    result = model_pass(store=env.store, queue=env.queue, session=env.session, config=env.config,
        handoff=env.handoff, clock=env.clock, runner_factory=lambda **kw: pytest.fail("Opening was refitted"))
    assert result["opening_prepared"] and len(env.outputs) == 2
    assert all(instant(f["reference_at"]) == env.reference for f in env.outputs)


def test_consecutive_model_passes_do_not_refit_adopted_opening_reference(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    env.now = env.target
    calls = dict(store=env.store, queue=env.queue, session=env.session, config=env.config,
        handoff=env.handoff, clock=env.clock, runner_factory=lambda **kw: pytest.fail("Adopted opening refitted"))
    first = model_pass(**calls)
    second = model_pass(**calls, state=first["state"])
    assert second["status"] == "WAITING_FOR_READY" and len(env.outputs) == 2
    # The adopted03:30 marker does not suppress a later reference. An advanced
    # pointer must reach its ordinary immutable-input loading path.
    env.now += timedelta(seconds=31)
    advanced = {"symbol": "AAPL", "reference_at": (env.reference+STEP).isoformat(),
                "record_sha256": "a"*64}
    env.handoff.ready = lambda **kw: [advanced]
    env.handoff.rejected = []
    loaded = []
    def load(record):
        loaded.append(record)
        raise ContractError("offline advanced-record sentinel")
    env.handoff.load = load
    model_pass(**calls, state=second["state"])
    assert loaded == [advanced]


def test_manual_stop_and_new_session_cannot_replay_prepared_predictions(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    env.now = env.target
    assert env.adopt(stopped=lambda: True) is None
    assert not env.outputs
    env.adopt()
    before = list(env.outputs)
    env.now += timedelta(seconds=1)
    another = {**env.session, "session_id": "manual-start-again", "started_at": env.now.isoformat()}
    assert env.adopt(session=another) is None and env.outputs == before


def test_expiry_wrong_config_and_ordinary_prior_issue_remain_unusable(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    source = ShadowStore(tmp_path / "intraday" / opening.CACHE_NAME).forecasts()[0]
    env.now = env.target
    assert env.adopt(config=replace(env.config, synthetic_policy="exclude")) is None
    assert forecast_disposition(source, now=env.now, session_started_at=env.session["started_at"],
                                local_symbols=env.symbols) == "forecast_precedes_manual_start"
    env.now += STEP
    assert env.adopt() is None and not env.outputs


def test_queue_failure_retries_exact_admission_without_refitting(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    env.now = env.target
    env.queue.enqueue = lambda forecast: (_ for _ in ()).throw(OSError("offline publication failure"))
    assert env.adopt() is None
    saved = env.store.forecasts()
    assert len(saved) == 2
    env.now += timedelta(seconds=1)
    env.queue.enqueue = env.outputs.append
    assert env.adopt()["symbols"] == list(env.symbols)
    assert env.outputs == saved and len(env.fits) == 2


def test_late_completed_fit_is_preserved_and_adopted_before_original_expiry(tmp_path):
    env = environment(tmp_path)
    original = env.trainer
    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        env.now = env.target + timedelta(minutes=2)
        return value
    env.trainer = slow
    result = env.prepare()
    assert result["status"] == "PREPARED" and not result["ready_before_open"]
    assert len(env.fits) == 2
    assert env.adopt()["symbols"] == list(env.symbols)
    assert all(instant(f["issued_at"]) == env.now for f in env.outputs)


def test_changed_admission_or_science_is_not_a_prior_issue_exception(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    env.now = env.target
    env.adopt()
    original = env.outputs[0]
    for changed in ({**original, "probability_not_down": .9},
                    {**original, "issued_at": env.target.isoformat()},
                    {**original, "opening_handoff": {**original["opening_handoff"], "session_id": "wrong"}}):
        changed.pop("forecast_id")
        changed["forecast_id"] = digest(changed)
        assert not opening.opening_handoff_valid(changed, session_started_at=env.session["started_at"])
        assert forecast_disposition(changed, now=env.now, session_started_at=env.session["started_at"],
                                    local_symbols=env.symbols) == "invalid_opening_handoff"


def test_corrupt_symbol_evidence_does_not_hide_other_prepared_symbol(tmp_path):
    env = environment(tmp_path)
    env.prepare()
    source = ShadowStore(tmp_path / "intraday" / opening.CACHE_NAME)
    with source.connect() as db:
        db.execute("UPDATE forecasts SET payload='{}' WHERE symbol='AAPL'")
    env.now = env.target
    assert env.adopt()["symbols"] == ["IONQ"]
    assert len(env.outputs) == 1 and env.outputs[0]["symbol"] == "IONQ"
    with env.store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='opening_forecast_rejected'").fetchone()[0] == 1


def test_failed_preparation_store_does_not_become_a_current_model_prerequisite(tmp_path):
    env = environment(tmp_path)
    (tmp_path / "intraday" / opening.CACHE_NAME).write_bytes(b"not a SQLite store")
    env.now = env.target
    assert env.adopt() is None and env.outputs == []
    with env.store.connect() as db:
        row = db.execute("SELECT payload FROM events WHERE kind='opening_preparation_unavailable'").fetchone()
    assert json.loads(row[0])["ordinary_model_work_continues"]


@pytest.mark.parametrize("date,utc_hour", [("2026-10-29", 11), ("2026-11-03", 12)])
def test_opening_window_follows_pacific_dst(date, utc_hour):
    reference, ready, target, close = opening.opening_window(date+"T09:00:00Z")
    assert target.hour == utc_hour and target-reference == 2*STEP
    assert ready == reference+STEP+timedelta(seconds=30) and close == target+STEP


def test_weekend_and_late_invocations_create_no_cache_or_trader_state(tmp_path):
    for moment, status in [("2026-10-31T10:30:00Z", "NO_OPENING_MARKET_SESSION"),
                           ("2026-10-29T11:00:00Z", "OPENING_ADMISSION_CLOSED")]:
        result = opening.run_preparation(datastore=tmp_path, actor="Atlas", symbols=("AAPL",),
            config=ResearchConfig(), clock=lambda: instant(moment))
        assert result["status"] == status
    assert not list(tmp_path.iterdir())
