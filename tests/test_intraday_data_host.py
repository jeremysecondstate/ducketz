"""Data-only installed bootstrap; no provider, broker or trader is invoked."""
from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import intraday_data_host as host


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def installation(root):
    runtime = {"python_relative": "python/python.exe", "files": {"python/python.exe": digest(b"offline python fixture")}}
    runtime_raw = encoded(runtime)
    runtime_id = digest(runtime_raw)
    runtime_path = root / "runtimes" / runtime_id
    (runtime_path / "python").mkdir(parents=True)
    (runtime_path / "manifest.json").write_bytes(runtime_raw)
    (runtime_path / "python/python.exe").write_bytes(b"offline python fixture")
    sources = {host.HOST: b"# fixture data bootstrap\n", host.INSTALLER: b"# fixture installer\n", host.DATA_PROTOCOL: b"# fixture data protocol\n"}
    manifest = {"schema": host.SCHEMA, "protocol": 1,
                "source_files": {name: digest(raw) for name, raw in sources.items()},
                "runtime": {"id": runtime_id, "manifest_sha256": runtime_id}}
    raw = encoded(manifest)
    generation = root / "generations" / digest(raw)
    (generation / "source/tools").mkdir(parents=True)
    (generation / "manifest.json").write_bytes(raw)
    for name, value in sources.items():
        (generation / "source" / name).write_bytes(value)
    (root / "active.json").write_bytes(encoded({"schema": host.SCHEMA, "protocol": 1,
        "generation": generation.name, "manifest_sha256": generation.name}))
    return generation, runtime_path


def options(tmp_path, **changes):
    values = dict(repository=tmp_path / "own checkout", datastore=tmp_path / "own datastore",
                  profile=tmp_path / "own profile.json", watchlist=tmp_path / "own watchlist.txt",
                  publication_offset_seconds=30, symbol_workers=None, lookback_days=None,
                  daily_data_wake=None, data_weekdays=None, once=False, verify_only=False)
    values.update(changes)
    return Namespace(**values)


def test_selects_exact_complete_bootstrap_without_importing_application(tmp_path):
    generation, runtime = installation(tmp_path)
    selected = host.select_generation(tmp_path)
    assert selected == {"generation": generation, "python": runtime / "python/python.exe"}


@pytest.mark.parametrize("member", ["generation", "host", "installer", "protocol", "runtime", "python"])
def test_bootstrap_byte_corruption_is_rejected(tmp_path, member):
    generation, runtime = installation(tmp_path)
    target = {"generation": generation / "manifest.json", "host": generation / "source" / host.HOST,
              "installer": generation / "source" / host.INSTALLER, "protocol": generation / "source" / host.DATA_PROTOCOL, "runtime": runtime / "manifest.json",
              "python": runtime / "python/python.exe"}[member]
    target.write_bytes(target.read_bytes() + b"corrupt")
    with pytest.raises(ValueError):
        host.select_generation(tmp_path)


@pytest.mark.parametrize("field,value", [("generation", "../other"), ("manifest_sha256", "0" * 64),
                                          ("protocol", 2), ("schema", "other")])
def test_unknown_or_traversing_active_pointer_never_falls_back(tmp_path, field, value):
    installation(tmp_path)
    pointer = json.loads((tmp_path / "active.json").read_bytes())
    pointer[field] = value
    (tmp_path / "active.json").write_bytes(encoded(pointer))
    with pytest.raises(ValueError):
        host.select_generation(tmp_path)


def test_one_frozen_data_subprocess_has_no_trading_activation_or_restart(tmp_path, monkeypatch):
    generation, runtime = installation(tmp_path)
    calls = []
    monkeypatch.setattr(host.subprocess, "run", lambda command, **kwargs:
                        calls.append((command, kwargs)) or SimpleNamespace(returncode=7))
    settings = options(tmp_path, once=True, symbol_workers=3, lookback_days=90)
    assert host.launch(tmp_path, settings) == 7
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:5] == [str(runtime / "python/python.exe"), "-I", "-S", "-B",
                            str(generation / "source" / host.HOST)]
    assert command[5:7] == ["--generation", str(generation)]
    assert "--once" in command and "--live" not in command and "--trader" not in command
    assert "--request" not in command and "--continue" not in command
    assert command[command.index("--symbol-workers") + 1] == "3"
    assert kwargs == {"close_fds": True, "creationflags": host.subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                      "check": False}


def test_verify_only_does_not_import_or_start_service(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host, "bootstrap", lambda generation: {"generation": "frozen", "runtime_id": "runtime"})
    monkeypatch.setattr(host.importlib, "import_module", lambda name: pytest.fail("Service imported by verification"))
    assert host.run_generation(tmp_path, options(tmp_path, verify_only=True)) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["service_started"] is False


def test_worker_enters_only_data_service_with_original_local_bindings(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(host, "bootstrap", lambda generation: {"generation": "frozen", "runtime_id": "runtime"})
    def load(name):
        assert name == "ml.intraday.split_service"
        return SimpleNamespace(main=lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(host.importlib, "import_module", load)
    settings = options(tmp_path)
    assert host.run_generation(tmp_path, settings) == 0
    assert calls == [host.service_arguments(settings)]
    assert str(settings.profile) in calls[0] and str(settings.watchlist) in calls[0]


def test_mutable_checkout_cannot_claim_to_be_frozen_worker(tmp_path):
    generation, _ = installation(tmp_path)
    with pytest.raises(ValueError, match="selected immutable generation"):
        host.bootstrap(generation)


def test_entry_requires_explicit_absolute_local_roots(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        host.main(["--repository", "relative", "--datastore", str(tmp_path)])
    with pytest.raises(SystemExit):
        host.main(["--repository", str(tmp_path), "--datastore", str(tmp_path), "--profile", "relative"])
    calls = []
    monkeypatch.setattr(host, "launch", lambda root, settings: calls.append((root, settings)) or 0)
    assert host.main(["--repository", str(tmp_path), "--datastore", str(tmp_path / "own")]) == 0
    root, settings = calls[0]
    assert root == tmp_path / "scratch/trader-installation"
    assert settings.profile == tmp_path / "scratch/cross-pc/local-profile.json"
    assert settings.watchlist == tmp_path / "datafetching/watchlist.local.txt"


def test_bootstrap_failure_records_own_lifecycle_and_preserves_manual_stop(tmp_path, monkeypatch):
    stop = tmp_path / "intraday/manual-session.json"
    stop.parent.mkdir()
    stop.write_bytes(b'{"status":"STOPPED","original":"manual-stop"}\n')
    original = stop.read_bytes()
    def fail(*args):
        raise ValueError("private detail must not enter the bounded bootstrap event")
    monkeypatch.setattr(host, "launch", fail)
    with pytest.raises(ValueError):
        host.main(["--repository", str(tmp_path), "--datastore", str(tmp_path)])
    assert stop.read_bytes() == original
    status = json.loads((tmp_path / "intraday/system-1a/bootstrap-status.json").read_bytes())
    assert status["status"] == "FAILED" and status["error_type"] == "ValueError"
    events = (tmp_path / "intraday/system-1a/bootstrap-events.jsonl").read_text()
    assert "private detail" not in events and len(events.splitlines()) == 2


def test_verify_entry_does_not_mutate_existing_lifecycle(tmp_path, monkeypatch):
    status = tmp_path / "intraday/system-1a/bootstrap-status.json"
    status.parent.mkdir(parents=True)
    status.write_bytes(b'{"original":"data-service-running"}\n')
    original = status.read_bytes()
    monkeypatch.setattr(host, "launch", lambda *args: 0)
    assert host.main(["--repository", str(tmp_path), "--datastore", str(tmp_path), "--verify-only"]) == 0
    assert status.read_bytes() == original
    assert not (status.parent / "bootstrap-events.jsonl").exists()


def test_existing_schedule_is_explicit_data_only_binding_and_forwarded_unchanged(tmp_path, monkeypatch):
    captured=[]
    monkeypatch.setattr(host,"launch",lambda root,settings: captured.append(settings) or 0)
    base=["--repository",str(tmp_path),"--datastore",str(tmp_path),"--verify-only"]
    assert host.main(base)==0
    assert "--daily-data-wake" not in host.service_arguments(captured[-1])
    assert host.main([*base,"--daily-data-wake","03:55","--data-weekdays","0,1,2,3,4"])==0
    args=host.service_arguments(captured[-1])
    assert args[-4:]==["--daily-data-wake","03:55","--data-weekdays","0,1,2,3,4"]
    assert "--live" not in args and "--trader" not in args


@pytest.mark.parametrize("binding",[
    ["--daily-data-wake","03:55"], ["--data-weekdays","0,1"],
    ["--daily-data-wake","25:00","--data-weekdays","0,1"],
    ["--daily-data-wake","03:55","--data-weekdays","0,7"],
    ["--daily-data-wake","03:55","--data-weekdays","0,0"],
])
def test_unknown_or_partial_schedule_does_not_invent_rollover_authority(tmp_path,binding):
    with pytest.raises(SystemExit):
        host.main(["--repository",str(tmp_path),"--datastore",str(tmp_path),*binding])


def test_continuous_launch_enters_one_data_lifetime_not_an_error_retry(tmp_path, monkeypatch):
    generation,runtime=installation(tmp_path);calls=[];bound=[]
    monkeypatch.setattr(host,"bind_parent_lifetime",lambda selected:bound.append(selected))
    monkeypatch.setattr(host.subprocess,"run",lambda argv,**kwargs:calls.append(argv) or SimpleNamespace(returncode=9))
    assert host.launch(tmp_path,options(tmp_path))==9
    assert len(calls)==len(bound)==1 and "--controller" in calls[0]
    assert "--request" not in calls[0] and "--trader" not in calls[0]


def test_verification_never_enters_controller_or_kernel_job(tmp_path,monkeypatch):
    installation(tmp_path);calls=[]
    monkeypatch.setattr(host,"bind_parent_lifetime",lambda selected:pytest.fail("Verification created lifetime"))
    monkeypatch.setattr(host.subprocess,"run",lambda argv,**kwargs:calls.append(argv) or SimpleNamespace(returncode=0))
    assert host.launch(tmp_path,options(tmp_path,verify_only=True))==0
    assert "--verify-only" in calls[0] and "--controller" not in calls[0]


def test_each_lane_selects_current_generation_and_unproven_runtime_stays_full_verified(tmp_path,monkeypatch):
    settings=options(tmp_path);settings.installation=tmp_path
    generation,runtime=installation(tmp_path);selections=[];commands=[]
    monkeypatch.setattr(host,"select_generation",lambda root:selections.append(root) or {"generation":generation,"python":runtime/"python/python.exe"})
    import tools.intraday_data_renewal as protocol
    import contextlib
    @contextlib.contextmanager
    def owned(root):yield
    monkeypatch.setattr(protocol,"own_data_session",owned)
    monkeypatch.setattr(protocol,"bind_kernel_lifetime",lambda:None)
    def run(**kwargs):
        commands.extend(kwargs['command']('prep-0',tmp_path/('request'+str(i)+'.json')) for i in range(3))
        assert kwargs['envelope']['session']['session_id'].startswith('data-atlas-1a:')
        assert kwargs['envelope']['options']['symbol_workers'] is None
        return 0
    monkeypatch.setattr(protocol,"run_protocol",run)
    assert host.run_controller(settings)==0
    assert len(selections)==4 and all('--request' in cmd for cmd in commands)
    assert all('--full-verify' in cmd for cmd in commands)  # No completed verified receipt yet.
    assert all('--live' not in cmd and '--trader' not in cmd for cmd in commands)


@pytest.mark.parametrize('matching',[True,False])
def test_runtime_cache_requires_exact_completed_bootstrap_receipt(tmp_path,monkeypatch,matching):
    import contextlib
    import tools.intraday_data_renewal as protocol
    settings=options(tmp_path);settings.installation=tmp_path
    generation,runtime=installation(tmp_path);manifest=json.loads((generation/'manifest.json').read_bytes())
    selected={'generation':generation,'python':runtime/'python/python.exe'}
    monkeypatch.setattr(host,'select_generation',lambda root:selected)
    @contextlib.contextmanager
    def owned(root):yield
    monkeypatch.setattr(protocol,'own_data_session',owned);monkeypatch.setattr(protocol,'bind_kernel_lifetime',lambda:None)
    def run(**kwargs):
        first=tmp_path/'first.json';raw=encoded({'turn':'turn-a','token':'token-a'});first.write_bytes(raw)
        assert '--full-verify' in kwargs['command']('prep-0',first)
        first.with_suffix('.claim.json').write_bytes(encoded({'turn':'turn-a','token':'token-a' if matching else 'wrong','pid':123}))
        first.with_suffix('.loaded.json').write_bytes(encoded({'turn':'turn-a','worker_pid':123,
            'runtime_id':manifest['runtime']['id'],'full_runtime_verified':True,'request_sha256':digest(raw)}))
        second=kwargs['command']('prep-0',tmp_path/'second.json')
        assert ('--full-verify' not in second)==matching
        return 0
    monkeypatch.setattr(protocol,'run_protocol',run)
    assert host.run_controller(settings)==0
