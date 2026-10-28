"""Complete local data adapters with real stores and prerecorded science."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import ast
import json
import socket
import threading

import pytest

# Public common main lacks the separately composed local consumers. Report that
# limit before importing them; portable data_renewal tests always run there.
ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "ml/intraday/runtime.py").read_bytes())
config_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResearchConfig")
if (not any(isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            and n.target.id == "population_contract" for n in config_node.body)
        or not (ROOT / "ml/intraday/candle_collection.py").is_file()):
    pytest.skip("Complete Atlas data integration adapters are absent on this public base", allow_module_level=True)

from ml.intraday import data_renewal as subject, runtime
from ml.intraday.prediction import PACIFIC, STEP, digest, instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.split_handoff import ReadySnapshots
from ml.intraday.store import ShadowStore
from tests.test_intraday_runtime import REF, dataset, history
from tests.test_intraday_data_renewal import Renewal, Planned


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("Offline integration network call"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("Offline integration network call"))


def bindings(tmp_path):
    watch = tmp_path / "watchlist.txt"; watch.write_text("AAPL\nIONQ\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(actor="Atlas", machine="pc-original", symbols=["AAPL", "IONQ"], symbol_profile_path=str(watch))))
    return profile, watch


def test_initializer_freezes_original_local_science_and_no_trader_controls(tmp_path):
    profile, watch = bindings(tmp_path)
    original = {"session_id": "data-atlas-1a:fixed-identity"}
    renewal = Renewal(original, role="initialize")
    renewal.envelope = dict(repository=str(tmp_path), datastore=str(tmp_path), profile=str(profile), watchlist=str(watch),
        session=original, options=dict(symbol_workers=2, publication_offset_seconds=30, lookback_days=None),
        daily_data_wake="03:55", data_weekdays=[0, 1, 2, 3, 4])
    result = subject._initialize(renewal, lambda: REF + timedelta(minutes=16))
    frozen = result["session"]
    assert frozen["session_id"] == original["session_id"] and frozen["started_at"] == (REF + timedelta(minutes=16)).isoformat()
    assert frozen["symbols"] == ["AAPL", "IONQ"] and frozen["config"]["symbol_workers"] == 2
    assert frozen["config"]["models"] == list(ResearchConfig().models)
    assert frozen["daily_data_wake"] == "03:55" and frozen["data_weekdays"] == [0, 1, 2, 3, 4]
    assert frozen["trading_authority"] is False and result["roles"] == ["prep-0", "prep-1", "candles"]
    assert not (tmp_path / "controls").exists() and not (tmp_path / "intraday/manual-session.json").exists()


def prepare_environment(tmp_path, monkeypatch, *, builder=None):
    from app.services import databento_market_data
    now = [REF + timedelta(minutes=16)]
    config = ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=2)
    base = tmp_path / "intraday/system-1a"; base.mkdir(parents=True)
    original = dict(actor="Atlas", session_id="data-atlas-1a:fixed-identity", symbols=["AAPL", "IONQ"],
        started_at=(now[0] - timedelta(minutes=1)).isoformat(), close_at=(REF + timedelta(hours=4)).isoformat(),
        config=json.loads(json.dumps(asdict(config))))
    store = ShadowStore(base / "collection.sqlite3")
    subject._collection_schema(store); subject._admit_collection_day(store, original, original["close_at"], now[0])
    def build(frame, *, cutoff, reference_at, **kwargs):
        if builder: builder(frame, reference_at)
        value = dataset(frame, cutoff=cutoff, reference_at=reference_at, **kwargs)
        value.inference["information_available_at"] = (reference_at + STEP).isoformat()
        return value
    monkeypatch.setattr(runtime, "ShadowRunner", lambda **kwargs: ShadowRunner(**kwargs, dataset_builder=build,
        trainer=lambda *a, **k: pytest.fail("Data preparation attempted fitting")))
    class Incoming:
        def __init__(self, **kwargs): self.reference = kwargs["reference_at"]
        def bind_cycle(self, *args): pass
        def get(self, symbol):
            frame = history(symbol); frame["opened_at"] = self.reference; frame["available_at"] = self.reference + STEP
            return frame
    monkeypatch.setattr(runtime, "LiveHistories", Incoming)
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: SimpleNamespace(dataset="EQUS.MINI"))
    cutoffs = []
    monkeypatch.setattr(subject, "_history", lambda root, store, session, config: cutoffs.append(session["started_at"]) or {})
    args = dict(root=tmp_path, base=base, session=original, config=config, store=store,
                close=instant(original["close_at"]), clock=lambda: now[0], pause=lambda _: None)
    return now, original, config, args, cutoffs


def test_real_symbol_passes_publish_exact_immutable_inputs_and_full_scope(tmp_path, monkeypatch):
    now, original, config, args, cutoffs = prepare_environment(tmp_path, monkeypatch)
    renewals = [Renewal(original, role="prep-0"), Renewal(original, role="prep-1")]
    for renewal in renewals:
        with pytest.raises(Planned): subject._prepare(renewal, **args)
    handoff = ReadySnapshots(args["base"] / "ready", actor="Atlas", symbols=original["symbols"], clock=args["clock"])
    ready = handoff.ready(now[0], configuration_hash=digest(asdict(config)))
    assert {r["symbol"] for r in ready} == {"AAPL", "IONQ"}
    for row in ready:
        assert row["reference_at"] == REF.isoformat() and row["target_close"] == (REF + 3 * STEP).isoformat()
        lane = ShadowStore(args["base"] / ("preparation-" + row["symbol"] + ".sqlite3"))
        assert lane.forecasts() == []
        with lane.connect() as db:
            cycle = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (row["cycle_id"],)).fetchone()[0])
        assert cycle["symbols"] == original["symbols"] and cycle["configuration_hash"] == digest(asdict(config))
    common = ShadowStore(args["base"] / "preparation.sqlite3")
    with common.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM symbol_inputs").fetchone()[0] == 0
    assert len(cutoffs) == 2


def test_fast_real_symbol_can_publish_next_quarter_while_other_previous_pass_is_busy(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def build(frame, reference):
        if frame.iloc[0]["symbol"] == "AAPL":
            entered.set(); assert release.wait(10)
    now, original, config, args, cutoffs = prepare_environment(tmp_path, monkeypatch, builder=build)
    slow, fast = Renewal(original, role="prep-0"), Renewal(original, role="prep-1")
    errors = []
    def slow_pass():
        try: subject._prepare(slow, **args)
        except Planned: pass
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=slow_pass)
    try:
        thread.start(); assert entered.wait(10)
        with pytest.raises(Planned): subject._prepare(fast, **args)
        now[0] += STEP
        fast.state = fast.checkpoints[-1][0]
        with pytest.raises(Planned): subject._prepare(fast, **args)
        ready = ReadySnapshots(args["base"] / "ready", actor="Atlas", symbols=original["symbols"], clock=args["clock"]).ready(now[0], configuration_hash=digest(asdict(config)))
        assert thread.is_alive() and {r["symbol"] for r in ready} == {"IONQ"}
        assert ready[0]["reference_at"] == (REF + STEP).isoformat()
        assert slow.checkpoints == []
    finally:
        release.set(); thread.join(10)
    assert not errors and not thread.is_alive()


def test_same_reference_retry_retains_history_cutoff_in_successor(tmp_path, monkeypatch):
    now, original, config, args, cutoffs = prepare_environment(tmp_path, monkeypatch)
    renewal = Renewal(original, role="prep-0")
    from ml.intraday.split_handoff import ReadySnapshots
    monkeypatch.setattr(ReadySnapshots, "ready", lambda *a, **k: [])
    with pytest.raises(Planned): subject._prepare(renewal, **args)
    renewal.state = renewal.checkpoints[-1][0]
    now[0] += timedelta(seconds=5)
    with pytest.raises(Planned): subject._prepare(renewal, **args)
    assert cutoffs == ["2026-10-08T17:16:00+00:00"] * 2
    now[0] += STEP
    renewal.state = renewal.checkpoints[-1][0]
    with pytest.raises(Planned): subject._prepare(renewal, **args)
    assert cutoffs[-1] == "2026-10-08T17:31:05+00:00"
