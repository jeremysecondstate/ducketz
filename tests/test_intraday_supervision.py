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
    assert result["model_assessment_scope"] == "supervised_candidates_and_supervised_incumbent"
    assert result["manual_recipe_qualification"] == "separate_manual_experiment_not_qualified_by_this_selection"
    assert not (tmp_path / "intraday").exists()


def helpers(monkeypatch):
    monkeypatch.setattr(s, "local_scope", lambda *args: ("Scout", ("ABCL", "DBX")))
    def audit(history, **kwargs):
        return {"freshness_ok": history != "stale", "completed_session": {
            "session_id": "2026-10-13", "closed_at": "2026-10-14T00:00:00Z"}}
    return dict(repository="unused", as_of="2026-10-14T08:00Z", execute=True,
        refresh=lambda *args, **kwargs: {"status": "complete", "source_dataset": "EQUS.MINI",
            "source_schema": "ohlcv-1m", "results": {symbol: {"error_files": 0} for symbol in args[2]},
            "requests": [{"symbol": symbol, "status": "fetched", "source_schema": "ohlcv-1m",
                "error_type": None, "provider_available_end": "2026-10-14T00:00:00Z"} for symbol in args[2]]},
        loader=lambda root, symbol, **kwargs: symbol, auditor=audit,
        builder=lambda *args, **kwargs: SimpleNamespace(feature_names=("a",), manifest={},
            training_reference_at=("2026-10-13T17:00:00Z",)))


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


@pytest.mark.parametrize("legacy", [False, True])
def test_pending_install_reuses_reviewed_dataset_scope_without_retuning(tmp_path, monkeypatch, legacy):
    from dataclasses import asdict
    import json
    from ml.intraday.model_policy import load_active_policy
    from ml.intraday.models import ModelSpec
    from ml.intraday.prediction import digest
    kwargs = helpers(monkeypatch)
    manifest = {"synthetic_policy": "exclude", "lookback_days": 45,
                "private_review_detail": "retained only in local decision"}
    kwargs["builder"] = lambda *a, **kw: SimpleNamespace(feature_names=("a",), manifest=manifest,
        training_reference_at=("2026-10-13T17:00:00Z",))
    proposal = dict(status="promoted", holdout_session="2026-10-13", selected_specs=[
        asdict(ModelSpec("qualified-logistic", family="logistic", probability_calibration="sigmoid"))])
    original_install = s.install_selection
    monkeypatch.setattr(s, "install_selection", lambda *a, **kw: (_ for _ in ()).throw(OSError("fixture interrupted install")))
    failed = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: proposal, **kwargs)
    assert failed["status"] == "incomplete"
    decisions = list((tmp_path / "intraday/supervision/decisions").rglob("*.json"))
    assert len(decisions) == 2
    if legacy:
        for path in decisions:
            value = json.loads(path.read_text())
            value.pop("sha256")
            value.pop("dataset_manifest")
            value["sha256"] = digest(value)
            path.write_text(json.dumps(value))
    frozen = {path: path.read_bytes() for path in decisions}
    monkeypatch.setattr(s, "install_selection", original_install)
    kwargs["builder"] = lambda *a, **kw: pytest.fail("rebuilt frozen session decision")
    complete = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: pytest.fail("retuned frozen holdout"), **kwargs)
    assert complete["status"] == "complete"
    for symbol in ("ABCL", "DBX"):
        assert complete["symbols"][symbol]["decision_reused"] is True
        policy = load_active_policy(tmp_path / "intraday/supervision", pc="Scout", symbol=symbol)
        assert policy["dataset_scope"] == {"synthetic_policy": "exclude", "lookback_days": None if legacy else 45}
        raw_policy = json.loads((tmp_path / f"intraday/supervision/active/Scout-{symbol}.json").read_text())
        assert "private_review_detail" not in raw_policy
    assert {path: path.read_bytes() for path in decisions} == frozen


def test_stale_active_inputs_do_not_block_completed_session_assessment(tmp_path, monkeypatch):
    kwargs = helpers(monkeypatch)
    kwargs["loader"] = lambda root, symbol, **kw: "stale" if symbol == "ABCL" else symbol
    calls = []
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: calls.append(1) or
        {"status": "retained", "holdout_session": "2026-10-13"}, **kwargs)
    assert result["symbols"]["ABCL"]["status"] == "complete"
    assert result["symbols"]["ABCL"]["current_inputs_fresh"] is False
    assert result["symbols"]["ABCL"]["data"]["freshness_ok"] is False
    assert result["symbols"]["DBX"]["status"] == "complete"
    assert len(calls) == 2


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
    assert result["status"] == "complete"
    assert result["symbols"]["ABCL"]["decision_reused"] is True
    assert result["symbols"]["ABCL"]["current_inputs_fresh"] is False


@pytest.mark.parametrize("failed", [False, True])
def test_unpublished_or_failed_refresh_cannot_freeze(tmp_path, monkeypatch, failed):
    kwargs = helpers(monkeypatch)
    refresh = kwargs["refresh"]
    def incomplete(*args, **kw):
        value = refresh(*args, **kw)
        if failed:
            value["status"] = "failed"
        else:
            for request in value["requests"]:
                request["provider_available_end"] = "2026-10-13T23:59:00Z"
        return value
    kwargs["refresh"] = incomplete
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: pytest.fail("fitted unpublished session"), **kwargs)
    assert result["status"] == "incomplete"
    assert all(item["status"] == "waiting_for_fresh_data" for item in result["symbols"].values())
    assert not list((tmp_path / "intraday/supervision/decisions").rglob("*.json"))


def test_missing_intended_holdout_freezes_insufficiency_without_older_assessment(tmp_path, monkeypatch):
    kwargs = helpers(monkeypatch)
    kwargs["builder"] = lambda *a, **kw: SimpleNamespace(feature_names=("a",), manifest={},
        training_reference_at=("2026-10-12T17:00:00Z",))
    for _ in range(2):
        result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: pytest.fail("assessed older session"), **kwargs)
        assert result["status"] == "complete"
    item = result["symbols"]["ABCL"]
    assert item["selection"]["status"] == "insufficient_data"
    assert item["selection"]["holdout_session"] == "2026-10-13"
    assert item["installation"]["status"] == "retained"
    assert item["decision_reused"] is True


@pytest.mark.parametrize("statistical", [False, True])
def test_only_typed_insufficiency_is_a_terminal_decision(tmp_path, monkeypatch, statistical):
    from ml.intraday.data import InsufficientTrainingData
    from ml.intraday.prediction import ContractError
    kwargs = helpers(monkeypatch)
    def unavailable(*a, **kw):
        raise (InsufficientTrainingData("No matured finite training labels for this cycle/window")
               if statistical else ContractError("Canonical raw identity changed"))
    kwargs["builder"] = unavailable
    result = s.supervise(datastore=tmp_path, evaluator=lambda *a, **kw: pytest.fail("fitted invalid dataset"), **kwargs)
    assert result["status"] == ("complete" if statistical else "incomplete")
    item = result["symbols"]["ABCL"]
    if statistical:
        assert item["selection"]["holdout_session"] == "2026-10-13"
        assert item["selection"]["status"] == "insufficient_data"
    else:
        assert item["error_type"] == "ContractError"
        assert not list((tmp_path / "intraday/supervision/decisions").rglob("*.json"))


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
    assert result["observed_not_down_fraction"] == 0
    assert result["majority_baseline_accuracy"] == 1


def test_review_distinguishes_synthetic_missing_and_unscorable_outcomes(tmp_path):
    from ml.intraday.store import ShadowStore
    from ml.intraday.prediction import TARGET_VERSION
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    forecasts = []
    for index in range(5):
        forecasts.append(store.save_forecast(dict(pc="Scout", symbol="ABCL", cycle_id=f"cycle-{index}",
            reference_at="2026-10-13T17:00:00+00:00", reference_open="100",
            target_open="2026-10-13T17:30:00+00:00", target_close="2026-10-13T17:45:00+00:00",
            issued_at="2026-10-13T17:16:02+00:00", information_available_at="2026-10-13T17:15:03+00:00",
            data_through="2026-10-13T17:15:00+00:00", target_version=TARGET_VERSION,
            mode="live", availability_basis="observed", probability_not_down=.9,
            configured_count=1, successful_count=1, ensemble_status="full",
            models=[dict(name="a", status="success", probability_not_down=.9)])))
    candle = dict(symbol="ABCL", opened_at="2026-10-13T17:30:00+00:00",
        available_at="2026-10-13T17:45:04+00:00", open="100", high="101", low="99",
        close="100", volume=100, source_id="source", availability_kind="actual", synthetic=False)
    store.observe(forecasts[0], as_of="2026-10-13T17:46:00Z", candle=candle)
    store.observe(forecasts[1], as_of="2026-10-13T17:46:00Z", candle={**candle, "synthetic": True,
        "evidence_json": '{"kind":"fixture_source_empty_minute"}'})
    store.observe(forecasts[2], as_of="2026-10-13T17:46:00Z")
    store.observe(forecasts[3], as_of="2026-10-13T17:46:00Z", unavailable_reason="fixture invalid target")
    # The fifth forecast has no saved outcome. A later observed correction cannot
    # leak into the review cutoff or turn the synthetic result into a scored one.
    store.observe(forecasts[1], as_of="2026-10-14T09:00:00Z", candle=candle)
    result = s.review_session(tmp_path, pc="Scout", symbol="ABCL", session_id="2026-10-13", as_of="2026-10-14T08:00Z")
    assert result["forecasts"] == 5
    assert result["scored"] == result["synthetic_outcomes"] == result["other_unscored_outcomes"] == 1
    assert result["missing_outcomes"] == 2
    assert result["unscored_outcomes"] == 4
    assert result["accuracy"] == 1
    assert result["brier"] == pytest.approx(.01)
