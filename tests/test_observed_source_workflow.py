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
