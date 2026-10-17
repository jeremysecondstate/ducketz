"""Previously saved unsupported targets are invalidated without rewriting history."""
from copy import deepcopy

from ml.intraday.prediction import digest, encoded
from ml.intraday.store import ShadowStore


def legacy(store):
    candle = dict(symbol="TEST", opened_at="2026-10-08T17:30:00+00:00",
        available_at="2026-10-08T17:45:04+00:00", open=100, high=100, low=100, close=100,
        volume=0, interval_seconds=900, source_id="fixture", availability_kind="actual",
        synthetic=True, no_trade=True, evidence_json='{"covered":true,"empty_responses":2}')
    key = store.record_candle(candle)
    evidence = {"forecast_id": "existing-original-forecast", "status": "synthetic",
                "target_not_down": True, "target_close_price": "100", "candle_id": key}
    value = {**evidence, "revision_id": digest(evidence), "observed_at": "2026-10-08T17:46:00+00:00"}
    with store.connect() as db:
        db.execute("INSERT INTO outcomes VALUES(?,?,?,?)",
            (value["revision_id"], value["forecast_id"], value["observed_at"], encoded(value)))
    return value, candle


def test_invalidation_retains_original_and_is_idempotent(tmp_path):
    store = ShadowStore(tmp_path / "test.sqlite3")
    old, candle = legacy(store)
    before = deepcopy(old)
    fixed = store.invalidate_unverified_outcome(old["forecast_id"], as_of="2026-10-09T00:00:00Z")
    assert fixed["status"] == "unscorable"
    assert fixed["target_not_down"] is None and fixed["target_close_price"] is None
    assert fixed["invalidates_revision_id"] == old["revision_id"]
    assert fixed["rejected_candle_id"] == old["candle_id"]
    assert store.revisions(old["forecast_id"])[0] == before
    assert store.observed_candles() == [candle]
    assert store.invalidate_unverified_outcome(old["forecast_id"], as_of="2026-10-10T00:00:00Z") is None
    assert len(store.revisions(old["forecast_id"])) == 2


def test_audit_cannot_backdate_revision_or_change_observed_target(tmp_path):
    store = ShadowStore(tmp_path / "test.sqlite3")
    old, _ = legacy(store)
    assert store.invalidate_unverified_outcome(old["forecast_id"], as_of="2026-10-08T17:45:00Z") is None
    assert len(store.revisions(old["forecast_id"])) == 1
    assert store.invalidate_unverified_outcome("unknown", as_of="2026-10-10T00:00:00Z") is None
