"""Offline ownership and error reporting for the data-only continuation command."""
from contextlib import contextmanager
import json

from tools import intraday_history_catchup as cli


def setup_local(tmp_path, monkeypatch, result):
    from datafetching import runtime_lock
    from ml.intraday import runtime, supervisor

    watch = tmp_path / "watchlist.txt"
    profile = tmp_path / "profile.json"
    config = tmp_path / "config.json"
    watch.write_text("ABC\n")
    profile.write_text(json.dumps({"actor": "Atlas", "machine": "pc-original",
        "symbols": ["ABC"], "symbol_profile_path": str(watch)}))
    config.write_text(json.dumps({"source_dataset": "EQUS.MINI", "selection": {"unchanged": True}}))
    calls = []

    @contextmanager
    def owner(path, **kwargs):
        calls.append(("lock", path))
        yield
        calls.append(("unlock", path))

    def refresh(datastore, symbols, *, source_dataset):
        assert calls[-1][0] == "lock"
        calls.append(("refresh", symbols, source_dataset))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runtime_lock, "exclusive_runtime_lock", owner)
    monkeypatch.setattr(supervisor, "refresh_ohlcv", refresh)
    monkeypatch.setattr(supervisor, "supervise", lambda **kw: (_ for _ in ()).throw(AssertionError("No training")))
    return dict(datastore=tmp_path, profile=profile, watchlist=watch, config=config), calls


def test_reuses_exact_source_and_supervisor_owner(tmp_path, monkeypatch):
    args, calls = setup_local(tmp_path, monkeypatch, {"ABC": {"status": "refreshed", "returned_minute_rows": 0}})
    original = args["config"].read_bytes()
    report, path = cli.catch_up(**args)
    assert report["status"] == "complete"
    assert calls == [("lock", tmp_path / "locks/intraday-model-supervision.lock"),
                     ("refresh", ("ABC",), "EQUS.MINI"),
                     ("unlock", tmp_path / "locks/intraday-model-supervision.lock")]
    assert json.loads(path.read_text()) == report
    assert args["config"].read_bytes() == original
    assert report["model_fitting"] is report["trader_activation"] is False


def test_missing_symbol_never_becomes_success(tmp_path, monkeypatch):
    args, _ = setup_local(tmp_path, monkeypatch, {})
    report, _ = cli.catch_up(**args)
    assert report["status"] == "incomplete"


def test_actual_error_is_durable_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "private-test-value")
    args, _ = setup_local(tmp_path, monkeypatch, RuntimeError("failed private-test-value"))
    report, path = cli.catch_up(**args)
    assert report["status"] == "error"
    assert report["error"]["failure_type"] == "RuntimeError"
    assert "private-test-value" not in path.read_text()


def test_mismatched_watchlist_prevents_fetch(tmp_path, monkeypatch):
    import pytest
    args, calls = setup_local(tmp_path, monkeypatch, {})
    args["watchlist"].write_text("XYZ\n")
    with pytest.raises(ValueError, match="membership disagree"):
        cli.catch_up(**args)
    assert not calls
