"""Permanent Historical-to-Live routing and publication safety checks."""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml import stock_target_history as history
from datafetching import xnas_replay_archive as replay
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS


@pytest.fixture
def fallback(tmp_path, monkeypatch):
    native_builder = history.build_target_history_manifest
    monkeypatch.setattr(history, "utc_timestamp", lambda: pd.Timestamp("2026-09-09T02:00Z"))
    monkeypatch.setattr(history, "latest_completed_through", lambda: date(2026, 9, 9))
    cursors = {s: {"completed_through": "2026-09-05"} for s in STOCK_TRADER_SYMBOLS}
    monkeypatch.setattr(history, "_verified_target_cursor", lambda root, symbol: cursors[symbol])
    manifest = {"requests": [{"symbol_scope": [s], "start": "2026-09-03", "end": "2026-09-09"}
                              for s in STOCK_TRADER_SYMBOLS]}
    monkeypatch.setattr(history, "build_target_history_manifest", lambda *a, **kw: manifest)
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2018-05-01", "end": "2026-09-08"}})
    monkeypatch.setattr(replay, "session_bounds", lambda session: (
        pd.Timestamp(session, tz="America/Los_Angeles").tz_convert("UTC") + pd.Timedelta(hours=4),
        pd.Timestamp(session, tz="America/Los_Angeles").tz_convert("UTC") + pd.Timedelta(hours=17)))
    calls, published = [], {}
    def destination(root, *, symbol, session):
        return root / "replay" / symbol / session
    def verify(path, *, root):
        return published[str(path)]
    def publish(root, *, symbol, session, **kwargs):
        calls.append(("publish", symbol, session))
        path = destination(root, symbol=symbol, session=session)
        path.mkdir(parents=True)
        (path / "manifest.json").write_text(json.dumps({"symbol": symbol, "session": session}))
        result = {"symbol": symbol, "session": session, "manifest_path": path / "manifest.json"}
        published[str(path)] = result
        return result
    monkeypatch.setattr(replay, "partition_directory", destination)
    monkeypatch.setattr(replay, "verify_partition", verify)
    monkeypatch.setattr(replay, "publish_session", publish)
    def cost(**kw):
        calls.append(("cost", kw))
        return 0.0
    client = SimpleNamespace(metadata=SimpleNamespace(get_cost=cost,
        list_unit_prices=lambda **kw: [{"feed_mode": "live", "prices": {"ohlcv-1m": 0.0}}]))
    return SimpleNamespace(root=tmp_path, calls=calls, client=client, cursors=cursors,
                           published=published, publish=publish, manifest=manifest,
                           native_builder=native_builder)


def test_unavailable_historical_range_permanently_routes_all_symbols_to_exact_live_sessions(fallback):
    state = fallback
    run = history.maintain_target_history(state.root, client=state.client, execute=True, api_key="fixture")
    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["status"] == "COMPLETE"
    assert receipt["coverage_basis"] == "HISTORICAL_PLUS_VERIFIED_XNAS_ACTION_SESSIONS"
    assert len(receipt["replay_manifests"]) == len(STOCK_TRADER_SYMBOLS)
    assert set(receipt["historical_cursor_ends"].values()) == {"2026-09-05"}
    assert [x[0] for x in state.calls] == ["cost"] * len(STOCK_TRADER_SYMBOLS) + ["publish"] * len(STOCK_TRADER_SYMBOLS)
    for _, request in state.calls[:len(STOCK_TRADER_SYMBOLS)]:
        assert request["dataset"] == "XNAS.ITCH"
        assert request["schema"] == "ohlcv-1m"
        assert request["stype_in"] == "raw_symbol"
        assert request["start"] == "2026-09-08T11:00:00+00:00"
        assert request["end"] == "2026-09-09T00:00:00+00:00"
    assert {x[2] for x in state.calls[len(STOCK_TRADER_SYMBOLS):]} == {"2026-09-08"}  # holiday is not replayed


def test_restart_reuses_verified_replay_without_cost_queries_subscriptions_or_cursor_relabel(fallback):
    state = fallback
    history.maintain_target_history(state.root, client=state.client, execute=True, api_key="fixture")
    state.calls.clear()
    run = history.maintain_target_history(state.root, client=state.client, execute=True, api_key="fixture")
    assert not state.calls
    assert json.loads((run / "receipt.json").read_text())["status"] == "COMPLETE"
    assert {v["completed_through"] for v in state.cursors.values()} == {"2026-09-05"}


def test_failed_live_subscription_does_not_publish_completion_or_repeat_other_symbols(fallback, monkeypatch):
    def denied(*args, **kwargs):
        raise RuntimeError("fixture-secret not entitled")
    monkeypatch.setattr(replay, "publish_session", denied)
    with pytest.raises(RuntimeError, match="REDACTED") as caught:
        history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture-secret")
    assert "fixture-secret" not in str(caught.value)
    runs = list((fallback.root / "ml/stock-target-history-runs").iterdir())
    assert len(runs) == 1
    assert not (runs[0] / "receipt.json").exists()
    progress = json.loads((runs[0] / "live-fallback-progress.json").read_text())
    assert progress["status"] == "FAILED"
    assert not fallback.published


def test_all_exact_costs_must_pass_before_any_live_subscription(fallback):
    fallback.client.metadata.get_cost = lambda **kw: 0.1 if kw["symbols"] == ["COST"] else 0.0
    with pytest.raises(ValueError, match="zero-cost"):
        history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture")
    assert not fallback.published


def test_historical_access_errors_do_not_trigger_live(fallback, monkeypatch):
    def denied(*args, **kwargs):
        raise RuntimeError("invalid account credential")
    monkeypatch.setattr(history, "discover_dataset_catalog", denied)
    with pytest.raises(RuntimeError, match="credential"):
        history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture")
    assert not fallback.calls


def test_live_cannot_bootstrap_missing_historical_baseline(fallback):
    fallback.cursors["COST"] = None
    with pytest.raises(ValueError, match="Historical baseline"):
        history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture")
    assert not fallback.calls


def test_preflight_only_never_subscribes(fallback):
    run = history.maintain_target_history(fallback.root, client=fallback.client, execute=False)
    assert json.loads((run / "receipt.json").read_text())["status"] == "PREFLIGHTED"
    assert not fallback.published


def test_historical_remains_first_choice_when_required_range_is_available(fallback, monkeypatch):
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2018-05-01", "end": "2026-09-09"}})
    def native(*args, **kwargs):
        for name in ("cost-preflight.json", "preflight.json"):
            (kwargs["run"] / name).write_text("{}")
        fallback.calls.append(("historical",))
        for cursor in fallback.cursors.values():
            cursor["completed_through"] = "2026-09-09"
        return {"downloaded": len(STOCK_TRADER_SYMBOLS)}
    monkeypatch.setattr(history, "_historical_acquisition", native)
    run = history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture")
    assert fallback.calls == [("historical",)]
    assert json.loads((run / "receipt.json").read_text())["status"] == "COMPLETE"


def test_old_missing_exchange_session_is_caught_up_by_historical_first(fallback, monkeypatch):
    for cursor in fallback.cursors.values():
        cursor["completed_through"] = "2026-09-04"
    def native(*args, **kwargs):
        assert kwargs["prefix"] == "historical-prefix-"
        fallback.calls.append(("historical_prefix",))
        for cursor in fallback.cursors.values():
            cursor["completed_through"] = "2026-09-08"
        return {"downloaded": len(STOCK_TRADER_SYMBOLS)}
    monkeypatch.setattr(history, "_historical_acquisition", native)
    history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture")
    assert fallback.calls[0] == ("historical_prefix",)
    assert all(x[2] == "2026-09-08" for x in fallback.calls if x[0] == "publish")


def test_corrupt_saved_replay_blocks_reuse_instead_of_silent_recapture(fallback, monkeypatch):
    fallback.publish(fallback.root, symbol="AAPL", session="2026-09-08")
    def corrupted(*args, **kwargs):
        raise ValueError("replay receipt checksum mismatch")
    monkeypatch.setattr(replay, "verify_partition", corrupted)
    with pytest.raises(ValueError, match="checksum"):
        history.maintain_target_history(fallback.root, client=fallback.client, execute=True, api_key="fixture")
    assert len(fallback.published) == 1


@pytest.mark.parametrize("cursor,through,expected", [
    (date(2026, 9, 9), date(2026, 9, 9), ()),
    (date(2026, 11, 4), date(2026, 11, 4), ("2026-11-03",)),
    (date(2026, 11, 5), date(2026, 11, 4), ()),
    (date(2026, 9, 5), date(2026, 9, 9), ("2026-09-08",)),
])
def test_utc_cursor_requires_actual_pacific_action_close_including_winter(cursor, through, expected):
    assert history._missing_sessions(cursor, through) == expected


def test_generic_live_tariff_is_not_mistaken_for_account_incremental_cost(fallback):
    fallback.client.metadata.list_unit_prices = lambda **kw: [
        {"mode": "live", "unit_prices": {"ohlcv-1m": 14.4}}]
    run = history.maintain_target_history(fallback.root, client=fallback.client, execute=False)
    evidence = json.loads((run / "live-fallback-preflight.json").read_text())
    assert evidence["generic_unit_prices_informational_only"][0]["unit_prices"]["ohlcv-1m"] == 14.4
    assert evidence["live_cost_basis"]["exact_live_interval_quote"] is False
    assert evidence["live_cost_basis"]["requires_native_access_acceptance"] is True


@pytest.mark.parametrize("through,native_end", [
    (date(2026, 9, 9), date(2026, 9, 9)),
    (date(2026, 10, 31), date(2026, 10, 31)),
    (date(2026, 11, 3), date(2026, 11, 4)),
    (date(2026, 11, 4), date(2026, 11, 5)),
    (date(2026, 11, 28), date(2026, 11, 29)),  # action close remains 17:00 on an early-close date
])
def test_native_historical_date_end_covers_exact_final_action_close(through, native_end):
    assert history._historical_request_through(through) == native_end


def test_winter_latest_completed_through_keeps_action_date_contract():
    assert history.latest_completed_through("2026-11-04T00:59:59Z") == date(2026, 11, 3)
    assert history.latest_completed_through("2026-11-04T01:00:00Z") == date(2026, 11, 4)


def _winter_state(fallback, monkeypatch, *, now, latest, cursor_end, provider_end):
    monkeypatch.setattr(history, "utc_timestamp", lambda: pd.Timestamp(now))
    monkeypatch.setattr(history, "latest_completed_through", lambda: latest)
    monkeypatch.setattr(history, "build_target_history_manifest", fallback.native_builder)
    for cursor in fallback.cursors.values():
        cursor["completed_through"] = cursor_end
    monkeypatch.setattr(history, "discover_dataset_catalog", lambda *a, **kw: {
        "ohlcv-1m": {"start": "2018-05-01", "end": provider_end}})
    def native(root, **kwargs):
        manifest, prefix = kwargs["manifest"], kwargs.get("prefix", "")
        fallback.calls.append(("historical", prefix, manifest["as_of"]))
        assert manifest["as_of"] <= provider_end
        history._validate_manifest_checksum(manifest)
        history._validate_manifest_included_scope(manifest)
        for request in manifest["requests"]:
            history._validate_execution_request_identity(root, request)
            assert request["end"] <= provider_end
            if kwargs["execute"]:
                fallback.cursors[request["symbol_scope"][0]]["completed_through"] = request["end"]
        for name in ("cost-preflight.json", "preflight.json"):
            (kwargs["run"] / f"{prefix}{name}").write_text("{}")
        return {"downloaded": len(manifest["requests"])} if kwargs["execute"] else {}
    monkeypatch.setattr(history, "_historical_acquisition", native)


def test_available_winter_tail_uses_historical_even_after_live_retention(fallback, monkeypatch):
    _winter_state(fallback, monkeypatch, now="2026-11-05T02:00Z", latest=date(2026, 11, 5),
                  cursor_end="2026-11-04", provider_end="2026-11-05")
    run = history.maintain_target_history(fallback.root, client=fallback.client,
        through=date(2026, 11, 4), execute=True, api_key="fixture")
    assert fallback.calls == [("historical", "", "2026-11-05")]
    assert not fallback.published
    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["status"] == "COMPLETE"
    assert receipt["completed_through"] == "2026-11-04"
    assert json.loads((run / "manifest.json").read_text())["as_of"] == "2026-11-05"
    assert {cursor["completed_through"] for cursor in fallback.cursors.values()} == {"2026-11-05"}
    assert not history._needs_session_coverage(fallback.root, date(2026, 11, 4))


def test_unavailable_winter_tail_replays_full_action_window_without_future_historical_request(fallback, monkeypatch):
    _winter_state(fallback, monkeypatch, now="2026-11-04T02:00Z", latest=date(2026, 11, 4),
                  cursor_end="2026-11-04", provider_end="2026-11-04")
    run = history.maintain_target_history(fallback.root, client=fallback.client,
        through=date(2026, 11, 4), execute=True, api_key="fixture")
    assert [call[0] for call in fallback.calls] == ["cost"] * len(STOCK_TRADER_SYMBOLS) + ["publish"] * len(STOCK_TRADER_SYMBOLS)
    for _, request in fallback.calls[:len(STOCK_TRADER_SYMBOLS)]:
        assert request["start"] == "2026-11-03T12:00:00+00:00"
        assert request["end"] == "2026-11-04T01:00:00+00:00"
    assert {call[2] for call in fallback.calls[len(STOCK_TRADER_SYMBOLS):]} == {"2026-11-03"}
    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["status"] == "COMPLETE"
    assert receipt["completed_through"] == "2026-11-04"
    assert set(receipt["historical_cursor_ends"].values()) == {"2026-11-04"}


def test_winter_historical_prefix_stays_within_provider_date_and_preserves_missing_tail(fallback, monkeypatch):
    _winter_state(fallback, monkeypatch, now="2026-11-05T02:00Z", latest=date(2026, 11, 5),
                  cursor_end="2026-11-04", provider_end="2026-11-05")
    run = history.maintain_target_history(fallback.root, client=fallback.client,
        through=date(2026, 11, 5), execute=True, api_key="fixture")
    assert fallback.calls[0] == ("historical", "historical-prefix-", "2026-11-05")
    assert json.loads((run / "manifest.json").read_text())["as_of"] == "2026-11-06"
    assert json.loads((run / "historical-prefix-manifest.json").read_text())["as_of"] == "2026-11-05"
    assert [call[0] for call in fallback.calls[1:]] == ["cost"] * len(STOCK_TRADER_SYMBOLS) + ["publish"] * len(STOCK_TRADER_SYMBOLS)
    for _, request in fallback.calls[1:8]:
        assert request["start"] == "2026-11-04T12:00:00+00:00"
        assert request["end"] == "2026-11-05T01:00:00+00:00"
    receipt = json.loads((run / "receipt.json").read_text())
    assert set(receipt["historical_cursor_ends"].values()) == {"2026-11-05"}
    assert len(receipt["replay_manifests"]) == len(STOCK_TRADER_SYMBOLS)


def test_unavailable_old_winter_tail_does_not_claim_historical_completion(fallback, monkeypatch):
    _winter_state(fallback, monkeypatch, now="2026-11-05T02:00Z", latest=date(2026, 11, 5),
                  cursor_end="2026-11-04", provider_end="2026-11-04")
    with pytest.raises(ValueError, match="outside Live replay retention"):
        history.maintain_target_history(fallback.root, client=fallback.client,
            through=date(2026, 11, 4), execute=True, api_key="fixture")
    assert not fallback.calls
    assert not fallback.published
    assert not list((fallback.root / "ml/stock-target-history-runs").glob("*/receipt.json"))
