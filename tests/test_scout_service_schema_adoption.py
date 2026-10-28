"""Saved-outcome dependency adoption preserves predecessor scientific rows."""
from ml.intraday.final_scoring import score_saved_session_outcomes
from ml.intraday.store import ShadowStore, _EXECUTION_FORECAST_SCHEMA
from tests.test_intraday_final_scoring_indexed import own, candle, NOW, SESSION


def test_predecessor_store_upgrade_preserves_originals_and_unknown_send(tmp_path):
    path = tmp_path / "predecessor.sqlite3"
    store = ShadowStore(path)
    saved = own(store, actor="Scout", symbol="FIXTURE")
    store.record_candle(candle(symbol="FIXTURE"))
    with store.connect() as db:
        before = db.execute("SELECT * FROM forecasts").fetchall()
        # Remove only the new optional revision schema, yielding the original
        # scientific schema. Retained source rows remain the predecessor bytes.
        for name, sql in _EXECUTION_FORECAST_SCHEMA.items():
            if sql.lstrip().startswith("CREATE TRIGGER"):
                db.execute('DROP TRIGGER "' + name + '"')
        for name, sql in _EXECUTION_FORECAST_SCHEMA.items():
            if sql.lstrip().startswith("CREATE TABLE"):
                db.execute('DROP TABLE "' + name + '"')
        db.execute("CREATE TABLE fixture_order_fence(id TEXT PRIMARY KEY,state TEXT)")
        db.execute("INSERT INTO fixture_order_fence VALUES('original-send','UNKNOWN')")
    adopted = ShadowStore(path)
    with adopted.connect() as db:
        assert db.execute("SELECT * FROM forecasts").fetchall() == before
        assert db.execute("SELECT * FROM fixture_order_fence").fetchall() == [("original-send", "UNKNOWN")]
        generation = adopted._execution_forecast_generation(db)
    assert adopted.forecast(saved["cycle_id"], "FIXTURE") == saved
    result = score_saved_session_outcomes(adopted, session_id=SESSION, actor="Scout",
                                          symbols=("FIXTURE",), as_of=NOW)
    assert result["scored"] == 1
    reopened = ShadowStore(path)
    with reopened.connect() as db:
        assert db.execute("SELECT * FROM forecasts").fetchall() == before
        assert reopened._execution_forecast_generation(db) == generation
        assert db.execute("SELECT * FROM fixture_order_fence").fetchall() == [("original-send", "UNKNOWN")]
