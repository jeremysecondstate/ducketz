from types import SimpleNamespace

import pytest

from ml.intraday import supervision as s


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-14T08:00Z", True), ("2026-10-14T10:30Z", True),
    ("2026-10-14T10:31Z", False), ("2026-10-14T07:59Z", False),
    ("2026-10-17T08:00Z", False), ("2026-11-02T09:00Z", True),
])
def test_pacific_weekday_window_and_dst(timestamp, expected):
    assert s.scheduled_window(timestamp) is expected


def test_default_is_offline_and_does_not_create_state(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "local_scope", lambda *args: ("Scout", ("ABCL",)))
    result = s.supervise(repository=tmp_path, datastore=tmp_path, as_of="2026-10-14T08:00Z")
    assert result["status"] == "dry_run"
    assert result["provider_calls"] == result["training_runs"] == 0
    assert not (tmp_path / "intraday").exists()


def helpers(monkeypatch):
    monkeypatch.setattr(s, "local_scope", lambda *args: ("Scout", ("ABCL", "DBX")))
    def audit(history, **kwargs):
        return {"freshness_ok": history != "stale", "completed_session": {"session_id": "2026-10-13"}}
    return dict(repository="unused", as_of="2026-10-14T08:00Z", execute=True,
        refresh=lambda *args, **kwargs: {"status": "complete"},
        loader=lambda root, symbol, **kwargs: symbol, auditor=audit,
        builder=lambda *args, **kwargs: SimpleNamespace(feature_names=("a",), manifest={}))


def test_repeat_wakes_refresh_but_never_retest_same_holdout(tmp_path, monkeypatch):
    calls = []
    kwargs = helpers(monkeypatch)
    def evaluate(dataset, **kwargs):
        calls.append(1)
        return {"status": "retained", "holdout_session": "2026-10-13", "selected_specs": []}
    for _ in range(2):
        result = s.supervise(datastore=tmp_path, evaluator=evaluate, **kwargs)
        assert result["status"] == "complete"
    assert len(calls) == 2  # one evaluation per symbol, not per wake
    assert result["symbols"]["ABCL"]["decision_reused"] is True


def test_stale_symbol_cannot_train_but_other_symbol_progresses(tmp_path, monkeypatch):
    kwargs = helpers(monkeypatch)
    kwargs["loader"] = lambda root, symbol, **kw: "stale" if symbol == "ABCL" else symbol
    calls = []
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: calls.append(1) or
        {"status": "retained", "holdout_session": "2026-10-13"}, **kwargs)
    assert result["symbols"]["ABCL"]["status"] == "waiting_for_fresh_data"
    assert result["symbols"]["DBX"]["status"] == "complete"
    assert len(calls) == 1


def test_technical_failure_resumes_and_does_not_freeze_rejection(tmp_path, monkeypatch):
    kwargs = helpers(monkeypatch)
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw:
        {"status": "failed", "holdout_session": "2026-10-13"}, **kwargs)
    assert result["status"] == "incomplete"
    assert not list((tmp_path / "intraday/supervision/decisions").rglob("*.json"))
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw:
        {"status": "retained", "holdout_session": "2026-10-13"}, **kwargs)
    assert result["status"] == "complete"


def test_latest_incomplete_session_cannot_become_holdout(tmp_path, monkeypatch):
    kwargs = helpers(monkeypatch)
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw:
        {"status": "promoted", "holdout_session": "2026-10-14"}, **kwargs)
    assert result["status"] == "incomplete"
    assert not list((tmp_path / "intraday/supervision/decisions").rglob("*.json"))


def test_saved_decision_does_not_hide_later_stale_data(tmp_path, monkeypatch):
    kwargs = helpers(monkeypatch)
    evaluator = lambda *a, **kw: {"status": "retained", "holdout_session": "2026-10-13"}
    assert s.supervise(datastore=tmp_path, evaluator=evaluator, **kwargs)["status"] == "complete"
    kwargs["loader"] = lambda *a, **kw: "stale"
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: pytest.fail("retuned holdout"), **kwargs)
    assert result["status"] == "incomplete"
    assert result["symbols"]["ABCL"]["decision_reused"] is True
    assert result["symbols"]["ABCL"]["status"] == "waiting_for_fresh_data"


def test_review_reads_latest_outcome_and_execution_without_counting_revisions(tmp_path):
    from ml.intraday.store import ShadowStore
    from ml.intraday.prediction import TARGET_VERSION, digest
    import json
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    value = dict(pc="Scout", symbol="ABCL", cycle_id="cycle", reference_at="2026-10-13T17:00:00+00:00",
        reference_open="100", target_open="2026-10-13T17:30:00+00:00", target_close="2026-10-13T17:45:00+00:00",
        issued_at="2026-10-13T17:16:02+00:00", information_available_at="2026-10-13T17:15:03+00:00",
        data_through="2026-10-13T17:15:00+00:00", target_version=TARGET_VERSION,
        mode="live", availability_basis="observed", probability_not_down=.995, configured_count=1, successful_count=1,
        ensemble_status="full", models=[dict(name="a", status="success", probability_not_down=.995)])
    f = store.save_forecast(value)
    c = dict(symbol="ABCL", opened_at="2026-10-13T17:30:00+00:00", available_at="2026-10-13T17:45:04+00:00",
        open="100", high="101", low="99", close="100", volume=100, source_id="source", availability_kind="actual", synthetic=False)
    store.observe(f, as_of="2026-10-13T17:46:00Z", candle=c)
    store.observe(f, as_of="2026-10-13T17:49:00Z", candle={**c, "close": "99", "available_at": "2026-10-13T17:48:00Z"})
    with store.connect() as db:
        for minute, filled in [(50, 1), (51, 2)]:
            projection = dict(submitted_quantity=2, filled_quantity=filled, recorded_at=f"2026-10-13T17:{minute}:00+00:00")
            db.execute("INSERT INTO intents VALUES(?,?,?,?)", (digest(projection), f["forecast_id"],
                projection["recorded_at"], json.dumps(projection)))
    result = s.review_session(tmp_path, pc="Scout", symbol="ABCL", session_id="2026-10-13", as_of="2026-10-14T08:00Z")
    assert result["forecasts"] == result["scored"] == 1
    assert result["wrong_extreme_probability_count"] == 1
    assert result["filled_quantity"] == result["submitted_quantity"] == 2
    assert result["realized_pnl"] is None
    assert result["accuracy"] == 0
