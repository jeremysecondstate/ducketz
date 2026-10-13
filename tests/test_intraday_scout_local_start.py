"""Manual local launcher checks use injected services and temporary books only."""
import json
import sys
from types import SimpleNamespace

import pytest

from ml.intraday import live_session, scout_prestart
from test_intraday_live_session import scout_manual_fixture


def test_local_manual_start_runs_forecast_execution_and_scoring_without_atlas(tmp_path,monkeypatch):
    kwargs,calls,executors = scout_manual_fixture(tmp_path,monkeypatch)
    remote = sys.modules["ml.intraday.scout_execution"].ScoutAccountExecutor
    class LocalExecutor(remote):
        def preflight(self):
            calls.append("local_preflight")
            assert not self.kwargs["manual_authority"]()
            return dict(ready=True,mode="scout-local-broker-account",session_id=self.kwargs["session_id"])
    monkeypatch.setitem(sys.modules,"ml.intraday.scout_local_execution",SimpleNamespace(ScoutLocalAccountExecutor=LocalExecutor))
    monkeypatch.setattr(live_session,"_scout_startup_proof",lambda *a,**k:pytest.fail("Atlas prerequisite"))
    kwargs["scout_admission"] = "broker-local"
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED"
    assert calls == ["local_preflight","history","provider","forecast","scoring","step","recover"]
    status = json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["scout_admission"] == "broker-local"


def test_cli_live_selects_local_broker_account_by_default(tmp_path,monkeypatch,capsys):
    observed = []
    monkeypatch.setattr(live_session,"run_manual",lambda **kw: observed.append(kw) or {"status":"STOPPED"})
    assert live_session.main(["--repository",str(tmp_path),"--live"]) == 0
    assert observed[0]["scout_admission"] == "broker-local"


def test_cli_without_live_only_checks_local_prerequisites(tmp_path,monkeypatch,capsys):
    profile = tmp_path/"profile.json"
    profile.write_text('{"actor":"Scout"}')
    monkeypatch.setattr(scout_prestart,"run_local_checks",lambda *a:dict(ready=True,mode="broker-local"))
    monkeypatch.setattr(live_session,"run_manual",lambda **k:pytest.fail("No manual start requested"))
    assert live_session.main(["--repository",str(tmp_path),"--profile",str(profile)]) == 2
    assert json.loads(capsys.readouterr().out)["ready"] is True


def local_prerequisites(tmp_path,monkeypatch):
    from ml.account_gameplan import config
    monkeypatch.setattr(scout_prestart,"verify_release",lambda _:dict(verified=True))
    monkeypatch.setattr(scout_prestart,"verify_profile",lambda _:dict(symbols=["DOCU"]))
    monkeypatch.setattr(scout_prestart,"verify_installation",lambda *a:dict(verified=True))
    monkeypatch.setattr(scout_prestart,"verify_dependencies",lambda _:dict(verified=True))
    monkeypatch.setattr(scout_prestart,"inspect_session",lambda _:dict(status="not_started_yet"))
    monkeypatch.setattr(scout_prestart,"inspect_local_account_writers",lambda *a:pytest.fail("Other traders are not a startup prerequisite"))
    monkeypatch.setattr(scout_prestart,"inspect_books",lambda *a:dict(status="no_journals_yet"))
    monkeypatch.setattr(config,"load_account_config",lambda _:pytest.fail("Gameplan configuration is not a trader prerequisite"))


def test_prestart_needs_no_remote_binding_and_no_cash_or_inventory(tmp_path,monkeypatch):
    local_prerequisites(tmp_path,monkeypatch)
    monkeypatch.setattr(scout_prestart,"check_connection",lambda *a:pytest.fail("No Atlas connection"))
    result = scout_prestart.run_local_checks(tmp_path,tmp_path)
    assert result["ready"] and result["local_checks_passed"]
    assert result["atlas_connection_required"] is False
    assert list(tmp_path.iterdir()) == []


def test_first_start_books_need_no_gameplan_configuration_or_broker_call(tmp_path,monkeypatch):
    original = scout_prestart.inspect_books
    local_prerequisites(tmp_path,monkeypatch)
    monkeypatch.setattr(scout_prestart,"inspect_books",original)
    result = scout_prestart.run_local_checks(tmp_path,tmp_path)
    assert result["ready"]
    assert result["checks"]["books"]["status"] == "no_journals_yet"
    assert result["checks"]["broker_account"]["gameplan_configuration_required"] is False
    assert list(tmp_path.iterdir()) == []
