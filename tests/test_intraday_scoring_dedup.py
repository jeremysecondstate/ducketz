"""Repeated scoring avoids writes while retaining immutable evidence checks."""
import json

import pytest

from ml.intraday import data, live_session
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


NOW = "2026-10-08T17:50:00Z"


def run_polls(store, count, *, clock=lambda: NOW, after_poll=None, stopped=None):
    polls = []
    def pause(seconds):
        assert seconds == 3
        polls.append(seconds)
        if after_poll:
            after_poll(len(polls))
    live_session.scoring_loop(store, histories={}, provider=None, journal=None,
        session_id="dedup-test", pc="Scout", clock=clock,
        stopped=stopped or (lambda: len(polls) >= count), pause=pause,
        history_source=lambda _: pytest.fail("Observed targets must not fetch history"))
    return polls


def observed_store(tmp_path, **changes):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    saved = store.save_forecast(forecast(mode="live", pc="Scout", **changes))
    store.record_candle(candle())
    return store, saved


def count_methods(monkeypatch, store, *names):
    calls = {name: [] for name in names}
    for name in names:
        original = getattr(store, name)
        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name].append((args, kwargs))
            return _original(*args, **kwargs)
        monkeypatch.setattr(store, name, counted)
    return calls


def test_second_poll_validates_every_input_without_repeating_observe_or_candle_write(tmp_path, monkeypatch):
    store, saved = observed_store(tmp_path)
    calls = count_methods(monkeypatch, store, "observe", "record_candle", "forecasts", "observed_candles")
    assert run_polls(store, 2) == [3, 3]
    assert len(calls["observe"]) == len(calls["record_candle"]) == 1
    assert len(calls["forecasts"]) == len(calls["observed_candles"]) == 2
    revisions = store.revisions(saved["forecast_id"])
    assert len(revisions) == 1 and revisions[0]["status"] == "scored"


def test_late_corrected_target_is_scored_then_deduplicated(tmp_path, monkeypatch):
    store, saved = observed_store(tmp_path)
    original_record = store.record_candle
    calls = count_methods(monkeypatch, store, "observe", "record_candle")
    def correction(poll):
        if poll == 1:
            original_record(candle(close="99", available_at="2026-10-08T17:48:00Z"))
    run_polls(store, 3, after_poll=correction)
    revisions = store.revisions(saved["forecast_id"])
    assert [r["target_not_down"] for r in revisions] == [True, False]
    assert revisions[0]["candle_id"] != revisions[1]["candle_id"]
    assert len(calls["observe"]) == len(calls["record_candle"]) == 2


@pytest.mark.parametrize("prior", ["pending", "unscorable"])
def test_new_exact_target_corrects_non_scored_prior(tmp_path, monkeypatch, prior):
    store, saved = observed_store(tmp_path)
    if prior == "pending":
        store.observe(saved, as_of="2026-10-08T17:44:00Z")
    else:
        store.observe(saved, as_of=NOW, unavailable_reason="No original observation")
    calls = count_methods(monkeypatch, store, "observe")
    run_polls(store, 2)
    assert len(calls["observe"]) == 1
    assert [r["status"] for r in store.revisions(saved["forecast_id"])] == [prior, "scored"]


def test_derived_reference_keeps_synthetic_status_and_initial_audit(tmp_path, monkeypatch):
    store, saved = observed_store(tmp_path,
        reference_provenance={"forward_fill_contract": "saved-derived-reference"})
    calls = count_methods(monkeypatch, store, "observe", "invalidate_unverified_outcome")
    run_polls(store, 3)
    assert len(calls["observe"]) == 1
    assert len(calls["invalidate_unverified_outcome"]) == 1
    revisions = store.revisions(saved["forecast_id"])
    assert len(revisions) == 1 and revisions[0]["status"] == "synthetic"
    assert live_session._outcome_already_observed(saved, candle(), revisions, as_of=NOW)


def test_legacy_synthetic_outcome_is_still_invalidated_and_original_retained(tmp_path, monkeypatch):
    store, saved = observed_store(tmp_path)
    legacy = candle(high="100", low="100", volume=0, synthetic=True,
        no_trade=True, evidence_json='{"legacy_confirmations":2}')
    # Historical admission supplies only the immutable fixture, never live proof.
    with monkeypatch.context() as old_writer:
        old_writer.setattr(data, "synthetic_continuity_verified", lambda _: True)
        old = store.observe(saved, as_of=NOW, candle=legacy)
    # The genuine candle corrects the invalid legacy assessment on this poll.
    calls = count_methods(monkeypatch, store, "observe", "invalidate_unverified_outcome")
    run_polls(store, 2)
    revisions = store.revisions(saved["forecast_id"])
    assert revisions[0] == old
    assert [r["status"] for r in revisions] == ["synthetic", "unscorable", "scored"]
    assert len(calls["observe"]) == len(calls["invalidate_unverified_outcome"]) == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='unverified_synthetic_outcome_requires_audit'").fetchone()[0] == 1


def test_identical_outcome_still_raises_for_backdated_observation(tmp_path, monkeypatch):
    store, saved = observed_store(tmp_path)
    store.observe(saved, as_of=NOW, candle=candle())
    calls = count_methods(monkeypatch, store, "observe")
    with pytest.raises(ContractError, match="backdated"):
        run_polls(store, 1, clock=lambda: "2026-10-08T17:49:00Z")
    assert len(calls["observe"]) == 1


def test_unavailable_future_candle_is_not_observed_or_used_by_shortcut(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "future.sqlite3")
    saved = store.save_forecast(forecast(mode="live", pc="Scout"))
    store.record_candle(candle(available_at="2026-10-08T17:48:00Z"))
    calls = count_methods(monkeypatch, store, "observe")
    run_polls(store, 1, clock=lambda: "2026-10-08T17:46:00Z")
    assert calls["observe"] == [] and store.revisions(saved["forecast_id"]) == []


def test_clock_reversal_after_eligibility_retains_unavailable_target_error(tmp_path, monkeypatch):
    store, saved = observed_store(tmp_path)
    # A structurally valid old revision must not bypass observe's receipt check
    # if the wall clock moves back between candidate selection and observation.
    revision = store.observe(saved, as_of=NOW, candle=candle())
    revision["observed_at"] = "2026-10-08T17:45:00Z"
    monkeypatch.setattr(store, "revisions", lambda _: [revision])
    times = iter([NOW, NOW, NOW, "2026-10-08T17:45:00Z"])
    calls = count_methods(monkeypatch, store, "observe")
    with pytest.raises(ContractError, match="available target"):
        run_polls(store, 1, clock=lambda: next(times))
    assert len(calls["observe"]) == 1


@pytest.mark.parametrize("table", ["forecasts", "candles", "outcomes"])
def test_later_poll_fails_closed_on_changed_immutable_payload(tmp_path, monkeypatch, table):
    store, _ = observed_store(tmp_path)
    calls = count_methods(monkeypatch, store, "observe")
    def corrupt(poll):
        if poll == 1:
            with store.connect() as db:
                key, raw = db.execute("SELECT id,payload FROM " + table + " LIMIT 1").fetchone()
                value = json.loads(raw)
                field, replacement = {"forecasts": ("probability_not_down", .73),
                    "candles": ("close", "99"), "outcomes": ("target_not_down", False)}[table]
                value[field] = replacement
                db.execute("UPDATE " + table + " SET payload=? WHERE id=?", (encoded(value), key))
    with pytest.raises(ContractError, match="hash"):
        run_polls(store, 2, after_poll=corrupt)
    assert len(calls["observe"]) == 1


@pytest.mark.parametrize("field,value", [
    ("status", "pending"), ("target_not_down", 1),
    ("target_close_price", "100.0"), ("reference_to_target_return", "1"),
    ("return_basis", "another-basis"), ("candle_id", "0" * 64),
    ("unavailable_reason", "no target"), ("availability_basis", "assumed"),
    ("revision_id", "0" * 64), ("unexpected", "extra"),
    ("forecast_id", "different"), ("observed_at", "invalid"),
])
def test_malformed_or_different_revision_never_shortcuts(tmp_path, field, value):
    store, saved = observed_store(tmp_path)
    revision = store.observe(saved, as_of=NOW, candle=candle())
    malformed = {**revision, field: value}
    assert not live_session._outcome_already_observed(saved, candle(), [malformed], as_of=NOW)
    if field not in {"revision_id", "observed_at"}:
        evidence = {k: v for k, v in malformed.items() if k not in {"revision_id", "observed_at"}}
        malformed["revision_id"] = digest(evidence)
        assert not live_session._outcome_already_observed(saved, candle(), [malformed], as_of=NOW)


def test_manual_stop_before_forecast_never_enters_observe(tmp_path, monkeypatch):
    store, _ = observed_store(tmp_path)
    stopped = []
    original = store.forecasts
    def forecasts(**kwargs):
        values = original(**kwargs)
        stopped.append(True)
        return values
    monkeypatch.setattr(store, "forecasts", forecasts)
    calls = count_methods(monkeypatch, store, "observe")
    run_polls(store, 1, stopped=lambda: bool(stopped))
    assert calls["observe"] == []
