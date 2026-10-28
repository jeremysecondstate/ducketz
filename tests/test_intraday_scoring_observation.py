"""Offline observation dispatch plus capability-scoped exact scoring integration."""
import ast
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
RECOVERY_CONTRACT = "intraday-multi-provider-recovery-v1"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Scoring observations must not contact services")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def isolated_helper(monkeypatch, completed, continuity):
    """Load the exact candidate source without importing any local adapter."""
    name = "_scoring_observation_fixture_" + uuid4().hex
    package = ModuleType(name)
    package.__path__ = []
    data = ModuleType(name + ".data")
    data.completed_15m_observation = completed
    data.synthetic_continuity_verified = continuity
    coverage = ModuleType(name + ".coverage")
    coverage.RECOVERY_POPULATION_CONTRACT = RECOVERY_CONTRACT
    for module in (package, data, coverage):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    spec = importlib.util.spec_from_file_location(name + ".scoring_observation",
        ROOT / "ml/intraday/scoring_observation.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec.loader.exec_module(module)
    return module.completed_scoring_observation


@pytest.mark.parametrize("population", [None, "ordinary-population", RECOVERY_CONTRACT])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("continuous", [False, True])
def test_portable_observation_dispatch_requires_all_applicable_proofs(monkeypatch, population, complete, continuous):
    events, value = [], {"population_contract": population, "retained_original": "unchanged"}
    before = dict(value)
    def completed(candle):
        assert candle is value
        events.append("completed")
        return complete
    def continuity(candle):
        assert candle is value
        events.append("continuity")
        return continuous
    check = isolated_helper(monkeypatch, completed, continuity)
    assert check(value) is (complete and (population == RECOVERY_CONTRACT or continuous))
    assert events == (["completed", "continuity"] if complete and population != RECOVERY_CONTRACT else ["completed"])
    assert value == before


def test_portable_incomplete_input_cannot_reach_continuity_or_field_reads(monkeypatch):
    class Incomplete:
        def get(self, *args):
            pytest.fail("Rejected observation must not be read again")
    check = isolated_helper(monkeypatch, lambda value: False,
        lambda value: pytest.fail("Rejected observation reached continuity"))
    assert check(Incomplete()) is False


@pytest.mark.parametrize("stage", ["completed", "continuity"])
def test_portable_validation_errors_are_not_hidden(monkeypatch, stage):
    class EvidenceError(Exception):
        pass
    def fail(value):
        raise EvidenceError(stage)
    check = isolated_helper(monkeypatch, fail if stage == "completed" else lambda value: True,
                            fail if stage == "continuity" else lambda value: True)
    with pytest.raises(EvidenceError, match=stage):
        check({"population_contract": "ordinary-population"})


def real_observation_contract_available():
    """A portable common checkout may lack Atlas's existing frozen adapter."""
    required = {
        "ml/intraday/data.py": {"completed_15m_observation", "synthetic_continuity_verified"},
        "ml/intraday/coverage.py": {"RECOVERY_POPULATION_CONTRACT"},
        "ml/intraday/recovered_population.py": {"verified_recovery_candle", "recovery_candle", "wrap_primary"},
    }
    for name, expected in required.items():
        path = ROOT / name
        if not path.is_file():
            return False
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError):
            return False
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        if not expected <= names:
            return False
    return all((ROOT / name).is_file() for name in (
        "ml/intraday/store.py", "ml/intraday/final_scoring.py", "ml/intraday/split_scoring.py",
        "ml/market_gap_evidence.py", "tests/test_intraday_recovered_population.py", "tests/test_intraday_store.py"))


@pytest.fixture(scope="class")
def real_runtime():
    from ml.intraday import recovered_population, scoring_observation
    from ml.intraday.data import completed_15m_observation, synthetic_continuity_verified
    from ml.intraday.prediction import ContractError, STEP, digest, instant
    from ml.intraday.store import ShadowStore
    from tests.test_intraday_recovered_population import SESSION, assessment, bar, primary
    from tests.test_intraday_store import candle as legacy_candle, forecast
    globals().update({name: value for name, value in locals().copy().items() if not name.startswith("_")})


def recovered(kind):
    if kind == "primary":
        return recovered_population.wrap_primary(primary())
    return recovered_population.recovery_candle(
        assessment(schwab=[bar()] if kind == "observed" else ()), SESSION)


def previous_predicate(value):
    return completed_15m_observation(value) and synthetic_continuity_verified(value)


def save_own_forecast(store, target):
    reference = instant(target["opened_at"]) - 2 * STEP
    session_id, cycle = "manual-scoring-proof-fixture", "own-original"
    store.begin_cycle(cycle, dict(pc="Atlas", mode="live", experiment=session_id,
        symbols=[target["symbol"]], reference_at=reference.isoformat(),
        configuration_hash="fixture", started_at=reference.isoformat()))
    value = store.save_forecast(forecast(pc="Atlas", mode="live", symbol=target["symbol"],
        cycle_id=cycle, reference_at=reference.isoformat(), data_through=(reference + STEP).isoformat(),
        information_available_at=(reference + STEP + timedelta(seconds=10)).isoformat(),
        issued_at=(reference + STEP + timedelta(seconds=20)).isoformat(),
        target_open=instant(target["opened_at"]).isoformat(),
        target_close=(instant(target["opened_at"]) + STEP).isoformat(),
        source_id=target["source_id"], source_dataset=target["source_dataset"],
        population_contract=target["population_contract"]))
    return session_id, value


@pytest.mark.skipif(not real_observation_contract_available(),
    reason="Portable checkout lacks the existing frozen observation adapter; full local integration is separate")
@pytest.mark.usefixtures("real_runtime")
class TestRealObservationIntegration:
    @pytest.mark.parametrize("value", [None, 0, "invalid", [], {}, {"interval_seconds": 900}])
    def test_invalid_legacy_shapes_keep_previous_rejection(self, value):
        assert scoring_observation.completed_scoring_observation(value) is False
        assert scoring_observation.completed_scoring_observation(value) == previous_predicate(value)


    @pytest.mark.parametrize("kind", ["primary", "observed", "carry"])
    def test_genuine_recovery_proof_is_validated_once_and_never_changed(self, kind, monkeypatch):
        value = recovered(kind)
        original = deepcopy(value)
        assert previous_predicate(value) is True
        calls, verify = [], recovered_population.verified_recovery_candle
        def checked(candle):
            calls.append(candle)
            return verify(candle)
        monkeypatch.setattr(recovered_population, "verified_recovery_candle", checked)
        assert scoring_observation.completed_scoring_observation(value) is True
        assert calls == [value]
        assert value == original


    @pytest.mark.parametrize("kind", ["primary", "observed", "carry"])
    @pytest.mark.parametrize("field, replacement", [
        ("evidence_json", "{}"), ("evidence_json", "malformed"),
        ("source_id", "different"), ("source_dataset", "different"),
        ("close", 1234), ("available_at", "2026-10-12T14:14:00Z"),
        ("synthetic", 1), ("session_id", "different"),
    ])
    def test_changed_recovery_evidence_keeps_original_rejection(self, kind, field, replacement, monkeypatch):
        value = recovered(kind)
        value[field] = replacement
        assert previous_predicate(value) is False
        calls, verify = [], recovered_population.verified_recovery_candle
        def checked(candle):
            calls.append(candle)
            return verify(candle)
        monkeypatch.setattr(recovered_population, "verified_recovery_candle", checked)
        assert scoring_observation.completed_scoring_observation(value) is False
        assert calls == [value]


    @pytest.mark.parametrize("field", ["source_id", "source_dataset", "evidence_json", "opened_at", "available_at"])
    def test_missing_recovery_proof_fields_remain_unavailable(self, field):
        value = recovered("observed")
        del value[field]
        assert previous_predicate(value) is False
        assert scoring_observation.completed_scoring_observation(value) is False


    def test_nonrecovery_completed_candle_still_requires_separate_continuity(self, ):
        observed = legacy_candle()
        assert scoring_observation.completed_scoring_observation(observed) is True
        native_minute = dict(observed, interval_seconds=60)
        assert scoring_observation.completed_scoring_observation(native_minute) is False
        unverified = dict(observed, synthetic=True, no_trade=True, high="100", low="100", volume=0,
                          evidence_json='{"legacy_confirmations":2}')
        assert completed_15m_observation(unverified) is True
        assert synthetic_continuity_verified(unverified) is False
        assert scoring_observation.completed_scoring_observation(unverified) is False
        for value in (observed, native_minute, unverified, primary()):
            assert scoring_observation.completed_scoring_observation(value) == previous_predicate(value)


    @pytest.mark.parametrize("kind", ["primary", "observed", "carry"])
    def test_recovery_finalization_retains_saved_labels_probabilities_and_exact_outcome(self, tmp_path, kind):
        from ml.intraday.final_scoring import score_saved_session_outcomes
        store = ShadowStore(tmp_path / "own.sqlite3")
        target = recovered(kind)
        session_id, original = save_own_forecast(store, target)
        store.record_candle(target)
        now = instant(target["available_at"]) + timedelta(seconds=1)
        options = dict(session_id=session_id, actor="Atlas", symbols=(target["symbol"],), as_of=now)
        result = score_saved_session_outcomes(store, **options)
        status = "synthetic" if target["synthetic"] else "scored"
        assert result == dict(session_id=session_id, scored=int(status == "scored"),
                             synthetic=int(status == "synthetic"), unavailable=0)
        revisions = store.revisions(original["forecast_id"])
        assert len(revisions) == 1
        assert revisions[0]["status"] == status
        assert revisions[0]["target_not_down"] == (target["close"] >= 100)
        assert revisions[0]["candle_id"] == digest(target)
        assert score_saved_session_outcomes(store, **options) == result
        assert store.revisions(original["forecast_id"]) == revisions
        assert store.forecast(original["cycle_id"], target["symbol"]) == original


    @pytest.mark.parametrize("kind", ["observed", "carry"])
    def test_split_mirror_and_finalization_share_predicate_without_rewriting_originals(self, tmp_path, kind, monkeypatch):
        from ml.intraday.split_scoring import scoring_pass
        target = recovered(kind)
        source = ShadowStore(tmp_path / "intraday/system-1a/collection.sqlite3")
        store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
        session_id, original = save_own_forecast(store, target)
        source.record_candle(target)
        now = instant(target["available_at"]) + timedelta(seconds=1)
        session = dict(actor="Atlas", symbols=[target["symbol"]], session_id=session_id,
                       close_at=(instant(target["opened_at"]) + STEP).isoformat())
        calls, completed = [], scoring_observation.completed_scoring_observation
        def checked(value):
            calls.append(value)
            return completed(value)
        monkeypatch.setattr(scoring_observation, "completed_scoring_observation", checked)
        options = dict(root=tmp_path, store=store, session=session,
            config=SimpleNamespace(population_contract=target["population_contract"], source_dataset="EQUS.MINI"),
            clock=lambda: now)
        first = scoring_pass(**options)
        status = "synthetic" if target["synthetic"] else "scored"
        assert first["status"] == "COMPLETED" and first["outcomes"][status] == 1
        assert first["newly_mirrored"] == 1 and len(calls) == 2
        before = store.revisions(original["forecast_id"])
        calls.clear()
        second = scoring_pass(**options)
        assert second["newly_mirrored"] == 0 and second["outcomes"][status] == 1
        assert len(calls) == 2
        assert store.revisions(original["forecast_id"]) == before
        assert store.forecast(original["cycle_id"], target["symbol"]) == original


    def test_optimized_finalization_still_rejects_corrupt_unrelated_history(self, tmp_path):
        from ml.intraday.final_scoring import score_saved_session_outcomes
        store = ShadowStore(tmp_path / "own.sqlite3")
        target = recovered("observed")
        session_id, original = save_own_forecast(store, target)
        store.record_candle(target)
        key = store.record_candle(legacy_candle(symbol=target["symbol"]))
        with store.connect() as db:
            db.execute("UPDATE candles SET payload=json_set(payload,'$.close',99) WHERE id=?", (key,))
        with pytest.raises(ContractError, match="candle evidence hash"):
            score_saved_session_outcomes(store, session_id=session_id, actor="Atlas",
                symbols=(target["symbol"],), as_of=instant(target["available_at"]) + timedelta(seconds=1))
        assert store.revisions(original["forecast_id"]) == []


