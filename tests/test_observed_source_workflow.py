"""New source policy uses normal nightly acquisition without an operational gate."""
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from ml.overnight_runtime import run_overnight_pipeline


@pytest.mark.parametrize("source", ["observed-multi-source-stock-v1", "observed-multi-source-stock-v2"])
def test_new_source_keeps_history_stage_and_requests_real_schwab_history(tmp_path,monkeypatch,source):
    calls=[]
    def complete(command,**kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("offline fixture")
        return 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage",complete)
    (tmp_path/"datafetching").mkdir()
    (tmp_path/"datafetching/watchlist.local.txt").write_text("AAA\n")
    result=run_overnight_pipeline(tmp_path,datastore_argument=("--datastore",str(tmp_path)),
        repository_root=tmp_path,reporter=None,stock_only=True,independent_stock_horizons=True,
        stock_price_source=source,archive_history=True,
        start_at="loop_a_close_fetch",stop_after="stock_target_history",stats_first=True)
    report=json.loads((Path(result)/"stage-report.json").read_text())
    assert report["stage_order"]==["loop_a_close_fetch","stock_target_history"]
    assert "--include-schwab-price-history" in calls[0]
    assert "--extend-to-feature-history" in calls[1]

    assert ("--include-fmp-price-history" in calls[0]) == source.endswith("-v2")


def test_optional_history_uses_ny_calendar_overlap(tmp_path, monkeypatch):
    from datafetching.observed_stock_history import capture_optional_fmp_history
    calls=[]
    def capture(root,symbols,**kwargs):
        calls.append(kwargs)
        return {"status":"fixture"}
    monkeypatch.setattr("datafetching.fmp_price_history_receipts.capture_native_five_minute_history",capture)
    result=capture_optional_fmp_history(tmp_path,["AAA"],now=datetime(2026,10,17,1,tzinfo=timezone.utc))
    assert result["start_date"]=="2026-10-10"
    assert result["end_date"]=="2026-10-16"
    assert result["core_work_continues"]
    assert len(calls)==1


def test_optional_source_failure_keeps_core_work_continuing(tmp_path,monkeypatch):
    from datafetching.observed_stock_history import capture_optional_fmp_history
    def fail(*args,**kwargs):
        raise OSError("private URL never serialized")
    monkeypatch.setattr("datafetching.fmp_price_history_receipts.capture_native_five_minute_history",fail)
    result=capture_optional_fmp_history(tmp_path,["AAA"],now=datetime(2026,10,17,12,tzinfo=timezone.utc))
    assert result["status"]=="error"
    assert result["error_type"]=="OSError"
    assert "private URL" not in str(result)
    assert result["core_work_continues"]


def test_dated_policy_preserves_completed_configuration_binding():
    from ml.nightly_workflow import _configuration_binding, _verify_configuration_binding
    from ml.gameplan_price_policy import POLICY_VERSION
    config = {"repository": "C:/repo", "datastore": "C:/data", "local_profile": "C:/profile.json"}
    old = _configuration_binding(config, "2026-10-19")
    config["stock_price_source_policy"] = {"version": POLICY_VERSION,
        "effective_from_action_date": "2026-10-20", "source_contract": "observed-multi-source-stock-v2"}
    _verify_configuration_binding(config, {"action_date": "2026-10-19", "configuration_binding": old})
    new = _configuration_binding(config, "2026-10-20")
    assert new["stock_price_source"] == "observed-multi-source-stock-v2"
    with pytest.raises(ValueError, match="configuration changed"):
        _verify_configuration_binding(config, {"action_date": "2026-10-20", "configuration_binding": old})


@pytest.mark.parametrize("day,source", [("2026-10-19", "xnas-itch-archive-v1"),
    ("2026-10-20", "observed-multi-source-stock-v2")])
def test_dated_native_catchup_keeps_archive_maintenance(tmp_path, monkeypatch, day, source):
    from ml.nightly_workflow import _run_native
    from ml.gameplan_price_policy import POLICY_VERSION
    calls = []
    def run(root, **kwargs):
        calls.append(kwargs)
        return tmp_path / "attempt"
    monkeypatch.setattr("ml.overnight_runtime.run_overnight_pipeline", run)
    monkeypatch.setattr("ml.overnight_runtime.overnight_status", lambda root: {})
    monkeypatch.setattr("ml.nightly_workflow._native_outputs", lambda path: {"native_run": str(path)})
    config = {"repository": str(tmp_path), "datastore": str(tmp_path), "continue_until_complete": False,
        "stock_price_source_policy": {"version": POLICY_VERSION,
            "effective_from_action_date": "2026-10-20", "source_contract": "observed-multi-source-stock-v2"}}
    state = {"action_date": day, "source_session": "2026-10-16", "deadline_at": "2026-10-20T11:00:00Z",
        "steps": {"datastore_catchup": {}}}
    _run_native(config, state, "datastore_catchup", lambda: None)
    assert calls[0]["stock_price_source"] == source
    assert calls[0]["stop_after"] == "stock_target_history"


@pytest.mark.parametrize("source,valid", [
    ("technicals/calculations/market_regime.py", True),
    ("technicals/../outside.py", False),
    ("scratch/private.py", False),
])
def test_frozen_feedback_accepts_verified_technical_namespace(tmp_path, monkeypatch, source, valid):
    from ml import gameplan_model_feedback as feedback
    from ml.nightly_workflow import _frozen_feedback_diagnostics
    actual = {"reviewed_at": "2026-10-17T01:00:00Z", "outcomes_through": "2026-10-17T00:00:00Z"}
    monkeypatch.setattr(feedback, "_stats", lambda root, frozen_binding: (object(), actual))
    monkeypatch.setattr(feedback, "_diagnostic_groups", lambda review: {})
    monkeypatch.setattr(feedback, "_intended_target", lambda actual, target: None)
    diagnostics = {"schema_version": feedback.VERSION, "training_code": {source: "a" * 64},
        "stats": actual, "groups": {}, "created_at": "2026-10-17T02:00:00Z"}
    if valid:
        _frozen_feedback_diagnostics(tmp_path, diagnostics)
    else:
        with pytest.raises(ValueError, match="invalid training binding"):
            _frozen_feedback_diagnostics(tmp_path, diagnostics)
