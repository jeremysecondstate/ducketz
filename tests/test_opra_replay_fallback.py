import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from datafetching import opra_replay_fallback as fallback
from datafetching import options_runtime


def test_completed_action_session_preserves_friday_over_holiday_weekend():
    assert fallback.completed_action_session("2026-09-05T09:00:00Z") == "2026-09-04"
    assert fallback.completed_action_session("2026-09-07T23:00:00Z") == "2026-09-04"
    assert fallback.completed_action_session("2026-09-04T23:59:00Z") == "2026-09-03"
    assert fallback.completed_action_session("2026-09-05T00:05:00Z") == "2026-09-04"


def test_cbbo_replay_covers_entire_regular_session_with_honest_bounds():
    start, end = fallback.replay_session_bounds("2026-09-04", "cbbo-1m", observed_at="2026-09-05T09:00Z")
    assert start == pd.Timestamp("2026-09-04T12:30Z")
    assert end == pd.Timestamp("2026-09-05T00:00Z")
    for schema in ("definition", "ohlcv-1h"):
        start, end = fallback.replay_session_bounds("2026-09-04", schema, observed_at="2026-09-05T09:00Z")
        assert start == pd.Timestamp("2026-09-04T00:00Z")


@pytest.mark.parametrize("session,schema,now", [
    ("2026-09-05", "cbbo-1m", "2026-09-06T09:00Z"),
    ("2026-09-07", "cbbo-1m", "2026-09-08T09:00Z"),
    ("2026-09-04", "cbbo-1m", "2026-09-04T21:00Z"),
    ("2026-09-04", "cmbp-1", "2026-09-05T09:00Z"),
])
def test_replay_rejects_non_session_premature_old_or_unrequested_schema(session, schema, now):
    with pytest.raises(RuntimeError):
        fallback.replay_session_bounds(session, schema, observed_at=now)


def test_saved_session_bounds_remain_valid_after_live_retention_expires():
    assert fallback.replay_session_bounds(
        "2026-09-04", "definition", observed_at="2026-09-07T09:00Z"
    ) == (pd.Timestamp("2026-09-04T00:00Z"), pd.Timestamp("2026-09-05T00:00Z"))


def _cursor(root: Path, through="2026-09-04"):
    return options_runtime._write_opra_symbol_history_cursor(root, symbol="AAPL", schema="definition", completed_through=through)


def test_failed_replay_preserves_existing_cursor(tmp_path, monkeypatch):
    from datafetching import databento_opra_replay as replay
    cursor = _cursor(tmp_path)
    before = cursor.read_bytes()
    monkeypatch.setattr(fallback, "utc_timestamp", lambda value=None: pd.Timestamp(value) if value else pd.Timestamp("2026-09-05T09:00Z"))
    monkeypatch.setattr(fallback.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(replay, "capture_replay", lambda **_: (_ for _ in ()).throw(RuntimeError("incomplete replay")))
    complete, failed, size = fallback.complete_session_from_replay(tmp_path, api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), session="2026-09-04", entitlement={}, max_bytes=10_000)
    assert (complete, failed, size) == (0, 1, 0)
    assert cursor.read_bytes() == before


def test_replay_cannot_jump_an_older_missing_exchange_session(tmp_path, monkeypatch):
    from datafetching import databento_opra_replay as replay
    cursor = _cursor(tmp_path, "2026-09-03")
    before = cursor.read_bytes()
    monkeypatch.setattr(replay, "capture_replay", lambda **_: pytest.fail("must not subscribe across missing older session"))
    assert fallback.complete_session_from_replay(tmp_path, api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), session="2026-09-04", entitlement={}, max_bytes=10_000)[:2] == (0, 1)
    assert cursor.read_bytes() == before


def test_stale_historical_metadata_uses_replay_without_unchanged_fetch(tmp_path, monkeypatch):
    _cursor(tmp_path)
    monkeypatch.setattr("databento.Historical", lambda _: object())
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: {"entitlements": {"definition": {"entitled_end": "2026-09-04"}}})
    monkeypatch.setattr(options_runtime, "synchronize", lambda *a, **k: pytest.fail("unchanged Historical range must not refetch"))
    monkeypatch.setattr(fallback, "replay_session_bounds", lambda *a, **k: (None, None))
    calls = []
    def complete(*args, **kwargs):
        calls.append(kwargs)
        options_runtime._write_opra_symbol_history_cursor(tmp_path, symbol="AAPL", schema="definition", completed_through="2026-09-05")
        return 1, 0, 1234
    monkeypatch.setattr(fallback, "complete_session_from_replay", complete)
    monkeypatch.setattr(options_runtime, "publish_health", lambda _: tmp_path / "health.json")
    result = options_runtime.synchronize_option_history(SimpleNamespace(root_dir=tmp_path), api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), reporter=None, bootstrap_missing=False, live_replay_fallback=True, required_session="2026-09-04", max_estimated_download_bytes=10_000)
    assert result.completed_scopes == result.live_replay_completed_scopes == 1
    assert result.failed_scopes == 0
    assert result.live_replay_bytes == 1234
    assert calls[0]["session"] == "2026-09-04"


def test_historical_metadata_cannot_regress_a_completed_replay_cursor(tmp_path, monkeypatch):
    cursor = _cursor(tmp_path, "2026-09-05")
    before = cursor.read_bytes()
    monkeypatch.setattr("databento.Historical", lambda _: object())
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: {"entitlements": {"definition": {"entitled_end": "2026-09-04"}}})
    monkeypatch.setattr(options_runtime, "synchronize", lambda *a, **k: pytest.fail("cannot request backward range"))
    result = options_runtime.synchronize_option_history(SimpleNamespace(root_dir=tmp_path), api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), reporter=None, bootstrap_missing=False)
    assert result.completed_scopes == 1
    assert cursor.read_bytes() == before


def _freeze_after_retention(monkeypatch):
    monkeypatch.setattr(fallback, "utc_timestamp", lambda value=None: pd.Timestamp(value) if value else pd.Timestamp("2026-09-07T09:00Z"))


def _saved_replay(tmp_path, monkeypatch, *, publish_cursor=True):
    from ml.artifacts import file_checksum
    directory = fallback.partition_directory(tmp_path, schema="definition", day="2026-09-04", symbols=("AAPL.OPT",), segment="live-session")
    directory.mkdir(parents=True)
    manifest = {
        "provider": "databento-opra", "dataset": "OPRA.PILLAR", "schema": "definition",
        "partition_date": "2026-09-04", "symbol_scope": "AAPL.OPT", "time_segment": "live-session",
        "partition_start": "2026-09-04T00:00Z", "partition_end": "2026-09-05T00:00Z",
        "request": {"dataset": "OPRA.PILLAR", "schema": "definition", "symbols": ["AAPL.OPT"],
                    "stype_in": "parent", "start": "2026-09-04T00:00Z", "end": "2026-09-05T00:00Z"},
        "provider_delivery": {"mode": "live-intraday-replay"}, "normalized": {"row_count": 1},
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest))
    # Native archive verification is tested by the publisher. These tests cover
    # the cursor binding and require that strict verifier to be called.
    calls = []
    def verify(directory, **_):
        calls.append(directory)
        return {"manifest": json.loads((directory / "manifest.json").read_text())}
    monkeypatch.setattr(fallback, "verify_partition", verify)
    if publish_cursor:
        cursor = options_runtime._write_opra_symbol_history_cursor(
            tmp_path, symbol="AAPL", schema="definition", completed_through="2026-09-05",
            replay_coverage={"basis": "VERIFIED_EXCHANGE_SESSION", "session": "2026-09-04",
                "partition_start": manifest["partition_start"], "partition_end": manifest["partition_end"],
                "manifest_path": str(path.relative_to(tmp_path)), "manifest_checksum_sha256": file_checksum(path)},
        )
    else:
        cursor = _cursor(tmp_path)
    return path, cursor, calls


def test_existing_replay_can_restore_cursor_after_retention(tmp_path, monkeypatch):
    _freeze_after_retention(monkeypatch)
    path, cursor, calls = _saved_replay(tmp_path, monkeypatch, publish_cursor=False)
    monkeypatch.setattr("datafetching.databento_opra_replay.capture_replay", lambda **_: pytest.fail("saved evidence must not resubscribe"))
    assert fallback.complete_session_from_replay(tmp_path, api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), session="2026-09-04", entitlement={}, max_bytes=0) == (1, 0, 0)
    assert json.loads(cursor.read_text())["completed_through"] == "2026-09-05"
    assert calls == [path.parent]


def test_new_capture_after_retention_fails_without_subscribing(tmp_path, monkeypatch):
    _freeze_after_retention(monkeypatch)
    cursor = _cursor(tmp_path)
    before = cursor.read_bytes()
    monkeypatch.setattr("datafetching.databento_opra_replay.capture_replay", lambda **_: pytest.fail("expired replay must not subscribe"))
    assert fallback.complete_session_from_replay(tmp_path, api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), session="2026-09-04", entitlement={}, max_bytes=1000) == (0, 1, 0)
    assert cursor.read_bytes() == before


def test_preflight_after_retention_does_not_call_live(tmp_path, monkeypatch):
    _freeze_after_retention(monkeypatch)
    _cursor(tmp_path)
    monkeypatch.setattr("databento.Historical", lambda _: object())
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: {"entitlements": {"definition": {"entitled_end": "2026-09-05"}}})
    monkeypatch.setattr(options_runtime, "storage_preflight", lambda *a, **k: {"estimated_download_size_bytes": 1, "estimated_cost_usd": 0.0})
    monkeypatch.setattr(options_runtime, "publish_storage_preflight", lambda _, value: value)
    monkeypatch.setattr(options_runtime, "synchronize", lambda *a, **k: pytest.fail("preflight must not download"))
    monkeypatch.setattr(fallback, "complete_session_from_replay", lambda *a, **k: pytest.fail("preflight must not invoke Live"))
    result = options_runtime.synchronize_option_history(SimpleNamespace(root_dir=tmp_path), api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), reporter=None, bootstrap_missing=False, live_replay_fallback=True, required_session="2026-09-04", preflight_only=True)
    assert result.failed_scopes == 0
    assert result.preflighted_scopes == 1


def test_replay_cursor_remains_verifiable_after_retention(tmp_path, monkeypatch):
    _freeze_after_retention(monkeypatch)
    path, _, calls = _saved_replay(tmp_path, monkeypatch)
    assert options_runtime._read_opra_symbol_history_cursor(tmp_path, symbol="AAPL", schema="definition") is not None
    assert calls == [path.parent]


@pytest.mark.parametrize("damage", ["hash", "outside_path", "wrong_scope", "missing_source", "cursor_date", "coverage_bounds", "source_integrity"])
def test_replay_cursor_rejects_damaged_or_mismatched_evidence(tmp_path, monkeypatch, damage):
    path, cursor, calls = _saved_replay(tmp_path, monkeypatch)
    payload = json.loads(cursor.read_text())
    if damage == "hash":
        payload["replay_coverage"]["manifest_checksum_sha256"] = "0" * 64
    elif damage == "outside_path":
        payload["replay_coverage"]["manifest_path"] = str(tmp_path.parent / "outside" / "manifest.json")
    elif damage == "wrong_scope":
        payload["replay_coverage"]["manifest_path"] = str(path).replace("AAPL.OPT", "NVDA.OPT")
    elif damage == "missing_source":
        path.unlink()
    elif damage == "cursor_date":
        payload["completed_through"] = "2026-09-06"
    elif damage == "coverage_bounds":
        payload["replay_coverage"]["partition_start"] = "2026-09-04T13:30:00Z"
    else:
        monkeypatch.setattr(fallback, "verify_partition", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("source checksum mismatch")))
    cursor.write_text(json.dumps(payload))
    assert options_runtime._read_opra_symbol_history_cursor(tmp_path, symbol="AAPL", schema="definition") is None


def test_invalid_replay_ahead_of_historical_metadata_is_not_counted_complete(tmp_path, monkeypatch):
    _, cursor, _ = _saved_replay(tmp_path, monkeypatch)
    payload = json.loads(cursor.read_text())
    payload["replay_coverage"]["manifest_checksum_sha256"] = "0" * 64
    cursor.write_text(json.dumps(payload))
    monkeypatch.setattr("databento.Historical", lambda _: object())
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: {"entitlements": {"definition": {"entitled_end": "2026-09-04"}}})
    result = options_runtime.synchronize_option_history(SimpleNamespace(root_dir=tmp_path), api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), reporter=None, bootstrap_missing=False)
    assert result.completed_scopes == 0
    assert result.bootstrap_required_scopes == 1


@pytest.mark.parametrize("consumer", ["strategy", "gameplan"])
def test_freshness_readers_reject_tampered_replay_source(tmp_path, monkeypatch, consumer):
    for schema in ("definition", "cbbo-1m", "ohlcv-1h"):
        options_runtime._write_opra_symbol_history_cursor(tmp_path, symbol="AAPL", schema=schema, completed_through="2026-09-05")
    _, cursor, _ = _saved_replay(tmp_path, monkeypatch)
    payload = json.loads(cursor.read_text())
    payload["replay_coverage"]["manifest_checksum_sha256"] = "0" * 64
    cursor.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="manifest binding is invalid"):
        if consumer == "strategy":
            from ml.strategy_profit_training_runtime import _validate_opra_history_freshness
            _validate_opra_history_freshness(
                pd.DataFrame({"horizon": ["1d"], "label_status": ["COMPLETE"], "target_window_start": [pd.Timestamp("2026-09-04T13:30Z")]}),
                execution_report={"fit_sessions": ["2026-09-03"], "assessment_sessions": ["2026-09-04"]},
                datastore_root=tmp_path, symbols=("AAPL",),
            )
        else:
            from ml.nightly_gameplan import _verify_opra_history
            _verify_opra_history(tmp_path, symbols=("AAPL",), action_date=date(2026, 9, 8), required_completed_through=date(2026, 9, 5))


@pytest.mark.parametrize("historical", ["success_then_replay", "provider_failure", "capacity_blocked", "corrupt_source"])
def test_final_scope_accounting_requires_verified_repair(tmp_path, monkeypatch, historical):
    from datafetching.databento_opra_history import OpraCapacityError
    _cursor(tmp_path, "2026-09-03" if historical == "success_then_replay" else "2026-09-04")
    monkeypatch.setattr("databento.Historical", lambda _: object())
    entitled_end = "2026-09-04" if historical == "success_then_replay" else "2026-09-05"
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: {"entitlements": {"definition": {"entitled_end": entitled_end}}})
    def history(*a, **k):
        if historical == "capacity_blocked":
            raise OpraCapacityError("Historical transfer staging exceeded free space")
        error = "checksum verification failed" if historical == "corrupt_source" else "provider dataset unavailable"
        return SimpleNamespace(errors={} if historical == "success_then_replay" else {"definition/2026-09-04": error},
            completed_rows=1 if historical == "success_then_replay" else 0, health_path=tmp_path / "health.json",
            status="COMPLETE", completed_partitions=1, skipped_partitions=0)
    monkeypatch.setattr(options_runtime, "synchronize", history)
    def complete(*args, **kwargs):
        options_runtime._write_opra_symbol_history_cursor(tmp_path, symbol="AAPL", schema="definition", completed_through="2026-09-05")
        return 1, 0, 10
    monkeypatch.setattr(fallback, "complete_session_from_replay", complete)
    monkeypatch.setattr(options_runtime, "publish_health", lambda _: tmp_path / "health.json")
    result = options_runtime.synchronize_option_history(SimpleNamespace(root_dir=tmp_path), api_key="test-placeholder", symbols=("AAPL",), schemas=("definition",), reporter=None, bootstrap_missing=False, live_replay_fallback=True, required_session="2026-09-04")
    assert result.requested_scopes == 1
    assert result.live_replay_completed_scopes == 1
    assert result.capacity_blocked_scopes == 0
    assert result.completed_scopes == (0 if historical == "corrupt_source" else 1)
    assert result.failed_scopes == (1 if historical == "corrupt_source" else 0)


def test_one_repaired_scope_does_not_clear_another_incomplete_scope(tmp_path, monkeypatch):
    for symbol in ("AAPL", "NVDA"):
        options_runtime._write_opra_symbol_history_cursor(tmp_path, symbol=symbol, schema="definition", completed_through="2026-09-04")
    monkeypatch.setattr("databento.Historical", lambda _: object())
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: {"entitlements": {"definition": {"entitled_end": "2026-09-04"}}})
    def complete(*args, **kwargs):
        options_runtime._write_opra_symbol_history_cursor(tmp_path, symbol="AAPL", schema="definition", completed_through="2026-09-05")
        return 1, 1, 10
    monkeypatch.setattr(fallback, "complete_session_from_replay", complete)
    monkeypatch.setattr(options_runtime, "publish_health", lambda _: tmp_path / "health.json")
    result = options_runtime.synchronize_option_history(SimpleNamespace(root_dir=tmp_path), api_key="test-placeholder", symbols=("AAPL", "NVDA"), schemas=("definition",), reporter=None, bootstrap_missing=False, live_replay_fallback=True, required_session="2026-09-04")
    assert result.requested_scopes == 2
    assert result.completed_scopes == 1
    assert result.failed_scopes == 1
