import json
from datetime import timedelta

import pytest

from ml.intraday import live_model_recipe, live_session
from ml.intraday.prediction import instant
from test_intraday_live_session import (
    isolate_minute_acquisition_in_execution_fixtures,
    module_fixture,
    scout_manual_fixture,
)


@pytest.mark.parametrize("started,wake", [
    ("2026-10-15T09:00:00Z", "2026-10-15T11:00:00Z"),
    ("2026-10-15T18:00:00Z", "2026-10-15T11:00:00Z"),
    ("2026-10-17T09:00:00Z", "2026-10-19T11:00:00Z"),
    ("2026-07-03T09:00:00Z", "2026-07-06T11:00:00Z"),
    ("2026-11-01T08:30:00Z", "2026-11-02T12:00:00Z"),
    ("2026-03-08T09:30:00Z", "2026-03-09T11:00:00Z"),
    ("2026-11-27T10:00:00Z", "2026-11-27T14:30:00Z"),
    ("2026-10-15T00:01:00Z", "2026-10-14T11:00:00Z"),
])
def test_manual_wake_respects_session_calendar_dst_and_existing_after_close(started, wake):
    assert live_session.manual_wake_at(started) == instant(wake)


@pytest.mark.parametrize("started,wake", [
    ("2026-10-08T10:58:30Z", "2026-10-08T11:00:00Z"),
    ("2026-10-10T09:00:00Z", "2026-10-12T11:00:00Z"),
])
def test_early_manual_start_defers_every_operating_boundary_until_wake(tmp_path, monkeypatch, started, wake):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now, wake = [instant(started)], instant(wake)
    kwargs["clock"] = lambda: now[0]
    recipes, sleeps = [], []

    def recipe(config, **options):
        assert now[0] >= wake
        recipes.append(now[0])
        return config, {}

    def sleep(seconds):
        if now[0] >= wake:
            raise KeyboardInterrupt()
        assert calls == executors == recipes == []
        status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
        assert status["status"] == "WAITING" and instant(status["wake_at"]) == wake
        assert instant(status["automatic_stop_at"]) == live_session.automatic_stop_deadline(wake)
        assert 0 < seconds <= 60
        sleeps.append(seconds)
        # Advance directly on the weekend case; real sleeps stay bounded.
        now[0] = min(wake, now[0] + timedelta(seconds=seconds)) if len(sleeps) == 1 else wake

    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", recipe)
    kwargs["sleep"] = sleep
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED"
    assert sleeps and recipes == [wake] and len(executors) == 1
    assert calls[:3] == ["preflight", "history", "provider"]
    assert calls.count("preflight") == calls.count("forecast") == calls.count("scoring") == 1
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "manual_stop"
    assert instant(status["started_at"]) == instant(started)
    assert not executors[0].kwargs["manual_authority"]()


def test_ctrl_c_during_wait_cancels_start_before_recipe_broker_or_workers(tmp_path, monkeypatch):
    from app.services import schwab
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    kwargs["clock"] = lambda: "2026-10-15T09:00:00Z"
    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", lambda *a, **kw: pytest.fail("recipe before wake"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: pytest.fail("broker before wake"))
    monkeypatch.setattr(live_session.threading, "Thread", lambda **kw: pytest.fail("worker before wake"))
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and calls == executors == []
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["status"] == "STOPPED" and status["stop_reason"] == "manual_stop"
    assert status["broker_orders_automatically_cancelled"] is False
    # The lock is released and another separately requested manual invocation
    # can wait/cancel normally, without inheriting the first session's intent.
    again = live_session.run_manual(**kwargs)
    assert again["session_id"] != result["session_id"] and calls == executors == []


def test_clock_jump_past_selected_day_cutoff_never_activates(tmp_path, monkeypatch):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-10T09:00:00Z")]
    kwargs["clock"] = lambda: now[0]
    kwargs["sleep"] = lambda seconds: now.__setitem__(0, instant("2026-10-13T00:00:00Z"))
    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", lambda *a, **kw: pytest.fail("recipe after cutoff"))
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and calls == executors == []
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["automatic_stop_at"] == "2026-10-13T00:00:00+00:00"
    assert status["stop_reason"] == "automatic_17_00_pacific"


def test_already_open_launch_does_not_wait_and_normal_first_boundary_is_unchanged(tmp_path, monkeypatch):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and calls[0] == "preflight"
    assert not live_session.eligible_reference("2026-10-15T10:45:00Z")
    assert live_session.eligible_reference("2026-10-15T11:00:00Z")
    assert live_session.latest_completed_reference("2026-10-15T11:15:30Z",
        publication_offset_seconds=30) == instant("2026-10-15T11:00:00Z")


def test_atlas_reads_recipe_and_fresh_compatibility_only_after_wait(tmp_path, monkeypatch):
    import dotenv
    from app.services import schwab, databento_market_data
    now, observed = [instant("2026-10-15T10:59:30Z")], []
    wake = instant("2026-10-15T11:00:00Z")
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(schwab, "SchwabSession", lambda: pytest.fail("broker after failed compatibility"))
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider",
        lambda: pytest.fail("provider after failed compatibility"))
    monkeypatch.setattr(live_session.threading, "Thread", lambda **kw: pytest.fail("worker after failed compatibility"))

    def recipe(config, **options):
        assert now[0] == wake
        observed.append("recipe")
        return config, {}

    def compatibility(*args, **options):
        assert now[0] == wake
        observed.append("compatibility")
        return {"ready": False, "reason_codes": ["FRESH_FIXTURE_FAILURE"]}

    def sleep(seconds):
        assert not observed and seconds == 30
        assert json.loads((tmp_path / "intraday/manual-session.json").read_text())["status"] == "WAITING"
        now[0] = wake

    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", recipe)
    module_fixture(monkeypatch, "ml.intraday.account_execution", IntradayAccountExecutor=object)
    module_fixture(monkeypatch, "ml.intraday.runtime_compatibility",
        read_gameplan_runtime_compatibility=compatibility)
    with pytest.raises(live_session.ContractError, match="FRESH_FIXTURE_FAILURE"):
        live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
            watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(),
            clock=lambda: now[0], sleep=sleep)
    assert observed == ["recipe", "compatibility"]
