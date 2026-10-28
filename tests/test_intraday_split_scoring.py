"""Provider-free post-close scoring, causal source bytes and manual stops."""
from datetime import timedelta
from types import SimpleNamespace
import json

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=(
    "ml/intraday/final_scoring.py", "ml/intraday/split_scoring.py", "ml/intraday/store.py",
    "tests/test_intraday_final_scoring.py"),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                 ("prepared_source", "forecast_preparer", "datastore"))))

from ml.intraday.prediction import STEP, digest, encoded
from ml.intraday.runtime import ResearchConfig
from ml.intraday.store import ShadowStore
from ml.intraday.split_scoring import scoring_pass, renewal_scoring_pass
from tests.test_intraday_final_scoring import CLOSE, SID, save_forecast, final_candle


def setup(tmp_path, symbols=("AAPL",)):
    source = ShadowStore(tmp_path / "intraday/system-1a/collection.sqlite3")
    target = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    session = {"actor": "Atlas", "symbols": list(symbols), "session_id": SID,
               "started_at": (CLOSE - timedelta(hours=9)).isoformat(), "close_at": CLOSE.isoformat()}
    forecasts = {symbol: save_forecast(target, cycle="own-" + symbol, symbol=symbol) for symbol in symbols}
    return source, target, session, forecasts


def run(root, target, session, at, **options):
    return scoring_pass(root=root, store=target, session=session, config=ResearchConfig(),
                        clock=lambda: at, **options)


def test_postclose_actual_final_observation_scores_original_and_reuses_without_writes(tmp_path, monkeypatch):
    source, target, session, forecasts = setup(tmp_path)
    source.record_candle(final_candle())
    first = run(tmp_path, target, session, CLOSE + timedelta(seconds=5))
    assert first["status"] == "COMPLETED" and first["outcomes"]["scored"] == 1
    assert first["newly_mirrored"] == 1
    revisions = target.revisions(forecasts["AAPL"]["forecast_id"])
    assert revisions[-1]["status"] == "scored"
    assert revisions[-1]["target_not_down"] is True
    monkeypatch.setattr(target, "record_candle", lambda *a: pytest.fail("Unchanged original rewritten"))
    second = run(tmp_path, target, session, CLOSE + timedelta(seconds=10))
    assert second["status"] == "COMPLETED" and second["newly_mirrored"] == 0
    assert target.revisions(forecasts["AAPL"]["forecast_id"]) == revisions
    assert target.forecast(forecasts["AAPL"]["cycle_id"], "AAPL") == forecasts["AAPL"]


def test_actual_availability_and_manual_stop_hold_postclose_boundary(tmp_path):
    source, target, session, forecasts = setup(tmp_path)
    source.record_candle(final_candle(available=CLOSE + timedelta(seconds=20)))
    early = run(tmp_path, target, session, CLOSE + timedelta(seconds=10))
    assert early["status"] == "WAITING_FOR_DATA" and early["mirrored"] == 0
    assert target.revisions(forecasts["AAPL"]["forecast_id"]) == []
    stopped = run(tmp_path, target, session, CLOSE + timedelta(seconds=25), stopped=lambda: True)
    assert stopped["status"] == "STOPPED"
    assert target.observed_candles() == []
    assert run(tmp_path, target, session, CLOSE + timedelta(seconds=25))["status"] == "COMPLETED"


def test_scoring_scopes_source_original_day_and_preserves_other_session(tmp_path):
    source, target, session, forecasts = setup(tmp_path)
    other = save_forecast(target, cycle="other-session", session="old-manual-session")
    old = final_candle()
    for field in ("opened_at", "available_at"):
        from ml.intraday.prediction import instant
        old[field] = (instant(old[field]) - timedelta(days=1)).isoformat()
    old_id = source.record_candle(old)
    with source.connect() as db:
        # The old day is outside this reader's admitted interval. It is not
        # reread as a current target or silently repaired by a scoring pass.
        db.execute("UPDATE candles SET payload='broken historical unrelated bytes' WHERE id=?", (old_id,))
    source.record_candle(final_candle())
    result = run(tmp_path, target, session, CLOSE + timedelta(seconds=5))
    assert result["status"] == "COMPLETED" and result["outcomes"]["scored"] == 1
    assert len(target.observed_candles()) == 1
    assert target.revisions(other["forecast_id"]) == []


def test_source_corruption_does_not_complete_symbol_or_block_other_valid_score(tmp_path):
    source, target, session, forecasts = setup(tmp_path, symbols=("AAPL", "AMZN"))
    bad_id = source.record_candle(final_candle())
    source.record_candle(final_candle(symbol="AMZN"))
    with source.connect() as db:
        raw = db.execute("SELECT payload FROM candles WHERE id=?", (bad_id,)).fetchone()[0]
        changed = json.loads(raw)
        changed["close"] = 100.25
        db.execute("UPDATE candles SET payload=? WHERE id=?", (encoded(changed), bad_id))
    result = run(tmp_path, target, session, CLOSE + timedelta(seconds=5))
    assert result["status"] == "WAITING_FOR_DATA"
    assert result["final_symbols_ready"] == ["AMZN"]
    assert result["rejected_candles"]
    assert target.revisions(forecasts["AAPL"]["forecast_id"]) == []
    assert target.revisions(forecasts["AMZN"]["forecast_id"])[-1]["status"] == "scored"


def test_changed_mirrored_row_is_detected_without_overwriting_evidence(tmp_path):
    source, target, session, forecasts = setup(tmp_path)
    source.record_candle(final_candle())
    run(tmp_path, target, session, CLOSE + timedelta(seconds=5))
    with target.connect() as db:
        identity, raw = db.execute("SELECT id,payload FROM candles").fetchone()
        changed = json.loads(raw)
        changed["close"] = 100.25
        db.execute("UPDATE candles SET payload=? WHERE id=?", (encoded(changed), identity))
    result = run(tmp_path, target, session, CLOSE + timedelta(seconds=6))
    assert result["status"] == "WAITING_FOR_DATA" and result["rejected_candles"]
    with target.connect() as db:
        assert json.loads(db.execute("SELECT payload FROM candles WHERE id=?", (identity,)).fetchone()[0])["close"] == 100.25


def test_completed_source_empty_is_unavailable_never_a_fabricated_outcome(tmp_path):
    source, target, session, forecasts = setup(tmp_path)
    with source.connect() as db:
        db.execute("CREATE TABLE intraday_candle_collection(session_id TEXT,symbol TEXT,reference_at TEXT,status TEXT)")
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'source_empty')",
            ("system-1a:atlas:2026-10-14:collection", "AAPL", (CLOSE - STEP).isoformat()))
    result = run(tmp_path, target, session, CLOSE + timedelta(seconds=5))
    assert result["status"] == "COMPLETED" and result["outcomes"]["unavailable"] == 1
    assert target.observed_candles() == []
    assert target.revisions(forecasts["AAPL"]["forecast_id"]) == []


def test_renewal_retains_waiting_scoring_after_close_and_completes_without_provider(tmp_path):
    source, target, session, forecasts = setup(tmp_path)
    checkpoints = []
    class Checkpoint(Exception):
        pass
    def checkpoint(state, due):
        checkpoints.append((state, due))
        raise Checkpoint
    renewal = SimpleNamespace(session=session, stopped=lambda: False, checkpoint=checkpoint)
    with pytest.raises(Checkpoint):
        renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal, config=ResearchConfig(),
                             clock=lambda: CLOSE + timedelta(seconds=5))
    assert checkpoints[-1][0]["last_scoring"]["status"] == "WAITING_FOR_DATA"
    source.record_candle(final_candle())
    assert renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal, config=ResearchConfig(),
        clock=lambda: CLOSE + timedelta(seconds=5))["status"] == "COMPLETED"
