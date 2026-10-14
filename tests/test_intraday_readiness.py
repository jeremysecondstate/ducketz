"""Readiness verifies fixture evidence without activating or changing originals."""
import ast
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import pytest

from ml.intraday import readiness
from ml.intraday.prediction import STEP, TARGET_VERSION, digest, encoded, instant

NOW = "2026-10-12T17:16:00+00:00"
PRIVATE_VALUE = "fixture-secret-account-not-for-output"


def inventory(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


@pytest.fixture
def local(tmp_path):
    repository = tmp_path / "repository"
    datastore = tmp_path / "datastore"
    repository.mkdir()
    datastore.mkdir()
    profile = repository / "local-profile.json"
    watchlist = repository / "watchlist.local.txt"
    watchlist.write_text("# Locally configured symbols\nAAPL\nIONQ\n", encoding="utf-8")
    profile.write_text(json.dumps({"actor": "Atlas", "machine": "pc-original", "symbols": ["AAPL", "IONQ"],
                                  "symbol_profile_path": str(watchlist), "private_extra": PRIVATE_VALUE}), encoding="utf-8")
    for relative in readiness.PUBLIC_SOURCES:
        source = repository / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# public fixture source\n", encoding="utf-8")
    (repository / "ml/account_gameplan/authority.py").write_text(
        "HORIZONS = frozenset({'1h','4h','1d','1w'})\n", encoding="utf-8")
    return dict(repository=repository, datastore=datastore, profile=profile, watchlist=watchlist)


def account(local, **changes):
    path = local["datastore"] / "state/account-gameplan/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"machine_id": "pc-original", "participants": {"pc-original": ["AAPL", "IONQ"]},
             "account_id": PRIVATE_VALUE, "api_key": PRIVATE_VALUE, "activation": "live"}
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def report(local, **changes):
    return readiness.inspect_readiness(**{**local, "as_of": NOW, **changes})


def pair(*, symbol="AAPL", reference="2026-10-12T17:00:00+00:00", issue=None,
         mode="shadow", source="EQUS.MINI", incomplete=False, recorded=None, **changes):
    ref = instant(reference)
    cutoff = ref+STEP+timedelta(seconds=25)
    issue = instant(issue) if issue else cutoff+timedelta(seconds=5)
    availability = "actual" if mode in {"shadow","live"} else "assumed_historical_close"
    basis = "observed" if mode in {"shadow","live"} else "assumed_historical"
    available = ref+STEP+timedelta(seconds=20)
    source_id = f"databento:{source}:ohlcv-1m"
    inference = dict(symbol=symbol, reference_at=ref.isoformat(), reference_open=100.,
                     data_through=(ref+STEP).isoformat(), information_available_at=available.isoformat(),
                     target_open=(ref+2*STEP).isoformat(), target_close=(ref+3*STEP).isoformat(),
                     target_version=TARGET_VERSION, source=source_id, source_dataset=source,
                     availability_kind=availability)
    snapshot = dict(cutoff=cutoff.isoformat(), inference=inference,
                    label_available_at=[(ref+STEP).isoformat()],
                    target_close_at=[(ref+STEP).isoformat()], reference_at=[(ref-2*STEP).isoformat()],
                    candles=[dict(symbol=symbol, opened_at=ref.isoformat(), available_at=available.isoformat(),
                                  open=100., high=101., low=99., close=100.5, volume=100,
                                  source_id=source_id, source_dataset=source, availability_kind=availability)])
    models = [dict(name=f"fixture_model_{i}", status="success", version=str(i)*64, probability_not_down=.5)
              for i in range(10)]
    if incomplete:
        models[-1] = dict(name="fixture_model_9", status="failed", version=None,
                          probability_not_down=None, error=PRIVATE_VALUE)
    payload = dict(pc="Atlas", symbol=symbol, cycle_id=digest([symbol, reference, mode, source, str(issue)]),
                   reference_at=ref.isoformat(), reference_open="100", data_through=(ref+STEP).isoformat(),
                   target_open=(ref+2*STEP).isoformat(), target_close=(ref+3*STEP).isoformat(),
                   information_available_at=available.isoformat(), training_cutoff=cutoff.isoformat(),
                   issued_at=issue.isoformat(), recorded_at=recorded or (issue+timedelta(milliseconds=1)).isoformat(),
                   target_version=TARGET_VERSION, mode=mode, availability_basis=basis,
                   snapshot_hash=digest(snapshot), source_dataset=source, source_id=source_id,
                   configured_count=10, successful_count=9 if incomplete else 10, models=models,
                   probability_not_down=.5, classification="not-down", ensemble_status="incomplete" if incomplete else "full",
                   issue_after_target=issue >= ref+3*STEP, private_error=PRIVATE_VALUE)
    payload.update(changes)
    payload["forecast_id"] = digest(payload)
    return payload, snapshot


def write_store(local, pairs, *, wal=False):
    path = local["datastore"] / "intraday/shadow.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    if wal:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("PRAGMA wal_autocheckpoint=0")
    db.executescript("""
        CREATE TABLE forecasts(id TEXT PRIMARY KEY,pc TEXT,symbol TEXT,cycle_id TEXT,reference_at TEXT,issued_at TEXT,payload TEXT);
        CREATE TABLE snapshots(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE symbol_inputs(cycle_id TEXT,symbol TEXT,snapshot_id TEXT);
    """)
    for payload, snapshot in pairs:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", tuple(payload[key] for key in (
            "forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")) + (encoded(payload),))
        db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?)", (digest(snapshot), encoded(snapshot)))
        db.execute("INSERT INTO symbol_inputs VALUES(?,?,?)", (payload["cycle_id"], payload["symbol"], payload["snapshot_hash"]))
    db.commit()
    if not wal:
        db.close()
    return db if wal else path


def rehash(payload):
    payload = deepcopy(payload)
    payload.pop("forecast_id")
    payload["forecast_id"] = digest(payload)
    return payload


def test_missing_store_and_account_do_not_create_files_or_change_cwd(local):
    before, cwd = inventory(local["datastore"]), Path.cwd()
    result = report(local)
    assert result["saved_forecasting"]["status"] == "missing"
    assert result["local_binding"]["account_binding"] == "absent_research_only"
    assert result["local_binding"]["symbols"] == ["AAPL", "IONQ"]
    assert result["manual_live_start_ready"] is False and result["orders_placed"] == 0
    assert inventory(local["datastore"]) == before and Path.cwd() == cwd
    assert not (local["datastore"] / "intraday").exists()


def test_full_current_saved_forecasts_never_establish_live_readiness(local):
    account(local)
    write_store(local, [pair(symbol="AAPL"), pair(symbol="IONQ")])
    before = inventory(local["datastore"])
    result = report(local)
    saved = result["saved_forecasting"]
    assert saved["status"] == "read" and saved["forecast_count"] == 2
    assert saved["latest_state_counts"]["shadow_full_current"] == 2
    assert saved["missing_shadow_symbols"] == []
    assert all(row["bound_data"]["status"] == "verified_recorded_inputs" for row in saved["latest_forecasts"])
    assert result["local_binding"]["account_binding"] == "membership_verified"
    assert result["capabilities"]["existing_shared_authority_horizons"] == ["1d", "1h", "1w", "4h"]
    assert result["capabilities"]["existing_shared_authority_supports_15m"] is False
    assert not result["live_execution_implemented"] and not result["manual_live_start_ready"]
    assert not result["provider_availability_verified"] and not result["broker_reconciliation_performed"]
    assert not result["execution_authorized"] and not result["activation_changed"]
    assert inventory(local["datastore"]) == before
    rendered = json.dumps(result)
    assert PRIVATE_VALUE not in rendered and str(local["datastore"]) not in rendered


def test_latest_per_symbol_source_and_mode_with_separate_incomplete_replay_stale_and_late(local):
    old = "2026-10-12T16:45:00+00:00"
    late = pair(symbol="IONQ", reference="2026-10-12T16:15:00+00:00", issue="2026-10-12T17:00:00+00:00")
    write_store(local, [pair(reference=old), pair(incomplete=True), pair(mode="historical_replay"),
                        pair(mode="historical_replay", source="XNAS.ITCH"), late])
    result = report(local)["saved_forecasting"]
    assert result["forecast_count"] == 5 and len(result["latest_forecasts"]) == 4
    assert result["latest_mode_counts"] == {"shadow": 2, "live":0, "historical_replay": 2}
    current = next(row for row in result["latest_forecasts"] if row["symbol"] == "AAPL" and row["mode"] == "shadow")
    assert current["configured_count"] == 10 and current["successful_count"] == 9
    assert current["ensemble_status"] == "incomplete" and not current["shadow_full_current"]
    assert current["data_state"] == "current_reference" and current["target_state"] == "target_pending"
    late_row = next(row for row in result["latest_forecasts"] if row["symbol"] == "IONQ")
    assert late_row["target_state"] == "late_issue" and late_row["data_state"] == "stale_reference"
    replay = [row for row in result["latest_forecasts"] if row["mode"] == "historical_replay"]
    assert all(row["data_state"] == "historical_replay" and not row["shadow_full_current"] for row in replay)
    assert {row["source_dataset"] for row in replay} == {"EQUS.MINI", "XNAS.ITCH"}


def test_future_issue_or_recording_and_wrong_symbol_cannot_supply_local_current_evidence(local):
    write_store(local, [pair(issue="2026-10-12T17:20:00+00:00"), pair(symbol="IONQ", recorded="2026-10-13T00:00:00+00:00"),
                        pair(symbol="ABCL")])
    saved = report(local)["saved_forecasting"]
    assert saved["future_record_count"] == 2 and saved["outside_membership_count"] == 1
    assert saved["latest_forecasts"] == [] and saved["missing_shadow_symbols"] == ["AAPL", "IONQ"]


@pytest.mark.parametrize("change", [
    {"target_close": "2026-10-12T17:30:00+00:00"},
    {"training_cutoff": "2026-10-12T17:15:31+00:00"},
    {"information_available_at": "2026-10-12T17:15:26+00:00"},
    {"availability_basis": "assumed_historical"}, {"configured_count": True},
    {"successful_count": 9}, {"ensemble_status": "incomplete"}, {"classification": "down"},
    {"probability_not_down": .75}, {"target_version": "legacy-positive"},
    {"source_id": "databento:XNAS.ITCH:ohlcv-1m"}, {"issue_after_target": True},
])
def test_rehashed_but_incompatible_contract_is_invalid_evidence(local, change):
    payload, snapshot = pair(**change)
    write_store(local, [(payload, snapshot)])
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "invalid_evidence" and saved["invalid_forecast_count"] == 1
    assert saved["latest_forecasts"] == []


@pytest.mark.parametrize("corruption", ["payload", "index", "version", "probability", "model_duplicate"])
def test_hash_identity_and_independent_model_evidence_detect_corruption(local, corruption):
    payload, snapshot = pair()
    if corruption == "version":
        payload["models"][0]["version"] = "unfitted-adapter"
        payload = rehash(payload)
    elif corruption == "probability":
        payload["models"][0]["probability_not_down"] = 1.5
        payload = rehash(payload)
    elif corruption == "model_duplicate":
        payload["models"][0]["name"] = payload["models"][1]["name"]
        payload = rehash(payload)
    path = write_store(local, [(payload, snapshot)])
    if corruption in ("payload", "index"):
        with sqlite3.connect(path) as db:
            if corruption == "payload":
                payload["reference_open"] = "101"
                db.execute("UPDATE forecasts SET payload=?", (encoded(payload),))
            else:
                db.execute("UPDATE forecasts SET symbol='IONQ'")
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "invalid_evidence" and saved["invalid_forecast_count"] == 1


@pytest.mark.parametrize("change", ["hash", "binding", "unmatured", "future_input", "reference_price", "source", "malformed", "assumed_shadow_reference", "uncovered_reference_availability"])
def test_bound_snapshot_hash_clocks_reference_and_source_are_verified(local, change):
    payload, snapshot = pair()
    path = write_store(local, [(payload, snapshot)])
    with sqlite3.connect(path) as db:
        if change == "hash":
            snapshot["cutoff"] = "2026-10-12T17:16:00+00:00"
            db.execute("UPDATE snapshots SET payload=?", (encoded(snapshot),))
        elif change == "binding":
            db.execute("UPDATE symbol_inputs SET snapshot_id=?", ("a"*64,))
        else:
            if change == "unmatured":
                snapshot["label_available_at"] = ["2026-10-12T17:16:00+00:00"]
            elif change == "future_input":
                snapshot["candles"][0]["available_at"] = "2026-10-12T17:16:00+00:00"
            elif change == "reference_price":
                snapshot["candles"][0]["open"] = 101.
            elif change == "source":
                snapshot["inference"]["source"] = "databento:XNAS.ITCH:ohlcv-1m"
            elif change == "assumed_shadow_reference":
                snapshot["candles"][0]["availability_kind"] = "assumed_historical_close"
            elif change == "uncovered_reference_availability":
                snapshot["candles"][0]["available_at"] = "2026-10-12T17:15:24+00:00"
            else:
                snapshot["inference"] = [PRIVATE_VALUE]
            payload["snapshot_hash"] = digest(snapshot)
            payload = rehash(payload)
            db.execute("UPDATE snapshots SET id=?,payload=?", (digest(snapshot), encoded(snapshot)))
            db.execute("UPDATE symbol_inputs SET snapshot_id=?", (digest(snapshot),))
            db.execute("UPDATE forecasts SET id=?,payload=?", (payload["forecast_id"], encoded(payload)))
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "invalid_evidence" and saved["invalid_bound_data_count"] == 1
    assert not saved["latest_forecasts"][0]["shadow_full_current"]
    assert saved["latest_forecasts"][0]["bound_data"]["status"] == "invalid_recorded_inputs"
    assert PRIVATE_VALUE not in json.dumps(saved)


def test_runtime_rounded_candle_keeps_original_inference_price_authoritative(local):
    from pandas import DataFrame
    payload, snapshot = pair()
    raw = 100.12345678901234
    snapshot["inference"]["reference_open"] = raw
    stored = json.loads(DataFrame({"open": [raw]}).to_json(orient="records", double_precision=10))[0]["open"]
    assert stored != raw
    snapshot["candles"][0]["open"] = stored
    payload["reference_open"] = str(raw)
    payload["snapshot_hash"] = digest(snapshot)
    payload = rehash(payload)
    path = write_store(local, [(payload, snapshot)])
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "read" and saved["latest_forecasts"][0]["shadow_full_current"]
    # A different forecast anchor cannot hide behind serialized candle precision.
    payload["reference_open"] = str(raw+1e-12)
    payload = rehash(payload)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE forecasts SET id=?,payload=?", (payload["forecast_id"], encoded(payload)))
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "invalid_evidence" and saved["invalid_bound_data_count"] == 1


def test_legacy_unrecorded_source_replay_is_retained_and_not_claimed_current(local):
    payload, snapshot = pair(mode="historical_replay", recorded=NOW)
    payload.pop("source_dataset")
    payload.pop("source_id")
    payload = rehash(payload)
    write_store(local, [(payload, snapshot)])
    saved = report(local)["saved_forecasting"]
    row = saved["latest_forecasts"][0]
    assert saved["status"] == "read" and row["source_dataset"] == "Unrecorded"
    assert row["bound_data"]["status"] == "verified_recorded_inputs"
    assert row["data_state"] == "historical_replay" and not row["shadow_full_current"]


def test_active_wal_is_read_without_changing_original_database_wal_or_shm(local):
    db = write_store(local, [pair()], wal=True)
    try:
        path = local["datastore"] / "intraday/shadow.sqlite3"
        assert Path(str(path)+"-wal").is_file() and Path(str(path)+"-shm").is_file()
        before = inventory(local["datastore"])
        saved = report(local)["saved_forecasting"]
        assert saved["status"] == "read" and saved["forecast_count"] == 1
        assert saved["latest_state_counts"]["shadow_full_current"] == 1
        assert inventory(local["datastore"]) == before
    finally:
        db.close()


def test_uncheckpointed_wal_without_original_shm_is_not_ignored_or_repaired(local, tmp_path):
    db = write_store(local, [pair()], wal=True)
    try:
        original = local["datastore"] / "intraday/shadow.sqlite3"
        cloned_datastore = tmp_path / "detached"
        copied = cloned_datastore / "intraday/shadow.sqlite3"
        copied.parent.mkdir(parents=True)
        for suffix in ("", "-wal"):
            Path(str(copied)+suffix).write_bytes(Path(str(original)+suffix).read_bytes())
        before = inventory(cloned_datastore)
        assert "intraday/shadow.sqlite3-shm" not in before
        saved = report(local, datastore=cloned_datastore)["saved_forecasting"]
        assert saved["status"] == "read" and saved["forecast_count"] == 1
        assert saved["latest_state_counts"]["shadow_full_current"] == 1
        assert inventory(cloned_datastore) == before
    finally:
        db.close()


def test_changing_snapshot_is_explicitly_unavailable_without_original_write(local, monkeypatch):
    write_store(local, [pair()])
    before = inventory(local["datastore"])
    read = readiness._snapshot_files
    count = 0
    def unstable(path):
        nonlocal count
        count += 1
        result = read(path)
        return result if count == 1 else {**result, "-wal": b"different observation"}
    monkeypatch.setattr(readiness, "_snapshot_files", unstable)
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "unavailable" and saved["findings"] == ["SHADOW_STORE_CHANGED_DURING_READ"]
    assert inventory(local["datastore"]) == before


@pytest.mark.parametrize("malformed", ["profile", "watchlist", "account", "account_membership", "account_identity"])
def test_invalid_binding_never_falls_back_or_exposes_private_values(local, malformed):
    account_path = account(local)
    if malformed == "profile":
        local["profile"].write_text("{"+PRIVATE_VALUE, encoding="utf-8")
    elif malformed == "watchlist":
        local["watchlist"].write_text("AAPL\nABCL\n", encoding="utf-8")
    elif malformed == "account":
        account_path.write_text("{"+PRIVATE_VALUE, encoding="utf-8")
    elif malformed == "account_membership":
        account(local, participants={"pc-original": ["AAPL", "ABCL"]})
    else:
        account(local, machine_id="pc-new")
    write_store(local, [pair()])
    before = inventory(local["datastore"])
    result = report(local)
    assert result["local_binding"]["status"] == "invalid"
    assert result["saved_forecasting"]["status"] == "not_inspected"
    assert not result["manual_live_start_ready"] and PRIVATE_VALUE not in json.dumps(result)
    assert inventory(local["datastore"]) == before


def test_missing_profile_watchlist_and_unreadable_store_are_bounded(local):
    local["profile"].unlink()
    assert report(local)["local_binding"]["findings"] == ["LOCAL_PROFILE_MISSING"]
    local["profile"].write_text(json.dumps({"actor": "Atlas", "machine": "pc-original", "symbols": ["AAPL", "IONQ"]}))
    local["watchlist"].unlink()
    assert report(local)["local_binding"]["findings"] == ["LOCAL_WATCHLIST_MISSING"]
    local["watchlist"].write_text("AAPL\nIONQ\n")
    account(local)
    path = local["datastore"] / "intraday/shadow.sqlite3"
    path.parent.mkdir()
    path.write_bytes(PRIVATE_VALUE.encode())
    before = inventory(local["datastore"])
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "unreadable" and saved["findings"] == ["SHADOW_STORE_UNREADABLE"]
    assert inventory(local["datastore"]) == before and PRIVATE_VALUE not in json.dumps(saved)


@pytest.mark.parametrize("as_of,cutoff,state", [
    ("2026-10-12T16:44:59.999999-07:00", "2026-10-12T23:45:00+00:00", "before_submission_cutoff"),
    ("2026-10-12T16:45:00-07:00", "2026-10-12T23:45:00+00:00", "submission_cutoff"),
    ("2026-11-02T16:45:00-08:00", "2026-11-03T00:45:00+00:00", "submission_cutoff"),
])
def test_cutoff_report_preserves_strict_pacific_clock_dst_and_eligibility_gap(local, as_of, cutoff, state):
    result = report(local, as_of=as_of)
    assert result["submission_time"]["cutoff_at"] == cutoff
    assert result["submission_time"]["state"] == state
    assert result["submission_time"]["eligible_session_not_verified"] is True
    assert not result["execution_authorized"]


def test_capabilities_use_only_public_source_not_account_activation(local):
    account(local)
    result = report(local)
    capabilities = result["capabilities"]
    assert capabilities["research_forecasting_source_present"]
    assert "SOURCE_CAPABILITY_UNVERIFIED:shared_cancellation" in capabilities["missing_integration"]
    assert capabilities["scout_prestart"] == {
        "module": "ml.intraday.scout_prestart",
        "required": ["SCOUT_AUTHENTICATED_PRESTART_REQUIRED"],
        "connection_verified": False,
    }
    assert not capabilities["manual_live_start_ready"]
    assert all(re.fullmatch("[a-f0-9]{64}", row["sha256"]) for row in capabilities["public_source_evidence"].values())
    (local["repository"] / "ml/account_gameplan/authority.py").write_text("broken source(")
    assert report(local)["capabilities"]["existing_shared_authority_horizons"] == []


def test_cli_outputs_safe_json_exit_two_and_does_not_change_inputs_or_cwd(local, capsys):
    account(local)
    write_store(local, [pair()])
    before = inventory(local["datastore"])
    cwd = Path.cwd()
    args = ["--as-of", NOW]
    for name in ("repository", "datastore", "profile", "watchlist"):
        args.extend(["--"+name, str(local[name])])
    assert readiness.main(args) == 2
    output = capsys.readouterr()
    assert output.err == "" and PRIVATE_VALUE not in output.out
    assert json.loads(output.out)["manual_live_start_ready"] is False
    assert inventory(local["datastore"]) == before and Path.cwd() == cwd
    args[1] = "2026-10-12T17:16:00"
    assert readiness.main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "INPUT_INVALID"


def test_module_has_no_provider_training_broker_activation_or_ledger_constructor_calls():
    tree = ast.parse(Path(readiness.__file__).read_text(encoding="utf-8"))
    imports = [(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    prohibited = ("runtime", "store", "models", "datafetch", "authority", "ledger", "broker", "provider", "schwab", "subprocess", "threading")
    assert not any(any(word in name for word in prohibited) for name in imports if name != "runtime_compatibility")
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not calls & {"ShadowStore", "OfflineBook", "AccountAuthority", "AccountLedger", "Popen", "setup_account"}
    connections = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "connect"]
    assert connections
    assert not any("immutable=1" in node.value for call in connections for arg in call.args
                   for node in ast.walk(arg) if isinstance(node, ast.Constant) and isinstance(node.value, str))


def test_manual_readiness_cmd_calls_only_readiness_module_and_forwards_diagnostic_arguments():
    repository = Path(__file__).resolve().parents[1]
    command = (repository / "Check-15m-Readiness.cmd").read_text(encoding="utf-8")
    launch = [line for line in command.splitlines() if re.search(r"python\.exe\".* -m ", line)]
    assert len(launch) == 1
    assert launch[0].startswith('"%~dp0.venv\\Scripts\\python.exe" -B -m ml.intraday.readiness ')
    assert launch[0].endswith(" %*")
    for option in ("--repository", "--datastore", "--profile", "--watchlist"):
        assert option in launch[0]
    assert "readinessExitCode=%errorlevel%" in command and "exit /b %readinessExitCode%" in command
    assert "pushd" in command and "popd" in command
    assert not re.search(r"(?im)^\s*(start\s|schtasks\s|powershell\s|call\s)", command)
    assert not any(token in launch[0].lower() for token in ("activation", "launcher", "provider", "broker", "runtime", "train"))


def test_public_source_inventory_includes_every_loaded_compatibility_dependency():
    from ml.intraday.runtime_compatibility import REQUIRED_CODE
    assert set(REQUIRED_CODE) <= set(readiness.PUBLIC_SOURCES)


def installed_sources(local):
    repository = Path(__file__).resolve().parents[1]
    for relative in readiness.PUBLIC_SOURCES:
        destination = local["repository"] / relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((repository/relative).read_bytes())


def verified_absence(**kwargs):
    return {"ready":True,"mode":"verified_gameplan_absent","reason_codes":[],
            "reviewed_installed_code_checked":True,"loaded_code_proven":False}


def test_live_observed_forecasts_are_distinct_from_shadow_and_replay(local):
    write_store(local,[pair(mode="live"),pair(mode="shadow"),pair(mode="historical_replay"),
                       pair(mode="live",symbol="IONQ",incomplete=True)])
    saved = report(local)["saved_forecasting"]
    assert saved["status"] == "read"
    assert saved["latest_mode_counts"] == {"live":2,"shadow":1,"historical_replay":1}
    assert saved["latest_state_counts"]["live_full_current"] == 1
    assert saved["latest_state_counts"]["shadow_full_current"] == 1
    assert saved["missing_live_symbols"] == []
    row = next(row for row in saved["latest_forecasts"] if row["mode"]=="live" and row["symbol"]=="AAPL")
    assert row["availability_basis"] == "observed" and row["bound_data"]["status"] == "verified_recorded_inputs"
    assert row["live_full_current"] and not row["shadow_full_current"]


@pytest.mark.parametrize("changes",[
    {"availability_basis":"assumed_historical"},{"target_version":"legacy-positive"},
    {"recorded":"2026-10-12T17:15:29+00:00"},
    {"target_close":"2026-10-12T17:30:00+00:00"},
])
def test_live_mode_cannot_bypass_actual_availability_recording_or_fixed_target(local,changes):
    write_store(local,[pair(mode="live",**changes)])
    saved = report(local)["saved_forecasting"]
    assert saved["status"]=="invalid_evidence" and saved["invalid_forecast_count"]==1
    assert saved["latest_forecasts"]==[]


def test_current_implemented_source_reports_preflight_required_without_starting(local,monkeypatch):
    installed_sources(local)
    account(local)
    write_store(local,[pair(mode="live"),pair(mode="live",symbol="IONQ")])
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *args,**kwargs:verified_absence(**kwargs))
    before = inventory(local["datastore"])
    result = report(local)
    assert result["live_execution_implemented"] and result["manual_live_source_ready"]
    assert result["status"]=="MANUAL_LIVE_PREFLIGHT_REQUIRED"
    assert result["manual_live_start_ready"] is False and result["manual_live_preflight_required"] is True
    assert result["capabilities"]["missing_integration"]==[]
    assert result["capabilities"]["existing_shared_authority_supports_15m"]
    assert result["capabilities"]["existing_shared_authority_horizons"]==["1d","1h","1w","4h"]
    assert result["gameplan_runtime_compatibility"]["status"]=="verified_gameplan_absent"
    assert "FRESH_EXACT_ORDER_STATUS_AND_COHERENT_ACCOUNT_RECONCILIATION" in result["required_live_preflight"]
    assert not result["execution_authorized"] and not result["broker_reconciliation_performed"]
    assert not result["provider_availability_verified"] and result["orders_placed"]==0
    assert PRIVATE_VALUE not in json.dumps(result) and inventory(local["datastore"])==before


def test_new_source_and_fresh_forecasts_do_not_replace_loaded_gameplan_proof(local,monkeypatch):
    installed_sources(local)
    account(local)
    write_store(local,[pair(mode="live")])
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**kw:{
        "ready":False,"reason_codes":["LOADED_GAMEPLAN_COMPATIBILITY_PROOF_REQUIRED"],"loaded_code_proven":False})
    result = report(local)
    assert result["live_execution_implemented"] and not result["manual_live_source_ready"]
    assert result["status"]=="MANUAL_LIVE_START_BLOCKED"
    assert result["gameplan_runtime_compatibility"]["status"]=="blocked"
    assert "LOADED_GAMEPLAN_COMPATIBILITY_PROOF_REQUIRED" in result["reason_codes"]


def test_loaded_code_and_absence_modes_need_their_exact_proof_flags(local,monkeypatch):
    installed_sources(local)
    account(local)
    for mode in ("loaded_gameplan_verified","verified_gameplan_absent"):
        monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**kw:{
            "ready":True,"mode":mode,"reason_codes":[]})
        assert not report(local)["gameplan_runtime_compatibility"]["ready"]
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**kw:{
        "ready":True,"mode":"loaded_gameplan_verified","reason_codes":[],"loaded_code_proven":True})
    checked = report(local)["gameplan_runtime_compatibility"]
    assert checked["ready"] and checked["loaded_code_proven"]


def test_historical_forecast_as_of_never_backdates_current_process_probe(local,monkeypatch):
    installed_sources(local)
    account(local)
    observed=[]
    def probe(*args,**kwargs):
        observed.append(kwargs)
        return verified_absence(**kwargs)
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",probe)
    result = report(local,as_of="2001-01-01T12:00:00+00:00")
    assert instant(result["gameplan_runtime_compatibility"]["observed_at"])>instant(result["observed_at"])
    assert observed[0]["as_of"]==result["gameplan_runtime_compatibility"]["observed_at"]
    assert observed[0]["maximum_heartbeat_age_seconds"]==120 and observed[0]["allow_verified_absence"] is True


def test_comments_empty_adapters_and_unsafe_default_do_not_claim_implemented_source(local):
    installed_sources(local)
    source = local["repository"] / "ml/intraday/account_execution.py"
    source.write_text("# IntradayAccountExecutor.step: size_forecast available_intraday_capacity _submit\n"
                      "class IntradayAccountExecutor:\n    def step(self):\n        pass\n")
    capabilities=readiness._source_capabilities(local["repository"])
    assert not capabilities["live_execution_implemented"]
    assert "SOURCE_CAPABILITY_UNVERIFIED:live_account_adapter" in capabilities["missing_integration"]
    installed_sources(local)
    source = local["repository"] / "ml/intraday/live_session.py"
    text = source.read_text()
    source.write_text(text.replace("if not args.live:","if not args.live:\n        run_manual()",1))
    capabilities=readiness._source_capabilities(local["repository"])
    assert not capabilities["source_component_checks"]["manual_session"]["source_interfaces_verified"]


def test_real_compatibility_reader_uses_injected_read_only_inventory_and_never_opens_ledgers(local,monkeypatch):
    from ml.intraday import runtime_compatibility
    installed_sources(local)
    account(local)
    before=inventory(local["datastore"])
    reader=runtime_compatibility.read_gameplan_runtime_compatibility
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**kw:reader(*a,**kw,process_inventory=lambda:[]))
    result=report(local)
    assert result["gameplan_runtime_compatibility"]["status"]=="verified_gameplan_absent"
    assert inventory(local["datastore"])==before
    assert not (local["datastore"] / "state/account-gameplan/authority.sqlite3").exists()
    assert not (local["datastore"] / "state/independent-stock-trader/holdings.sqlite3").exists()


def test_runtime_probe_errors_are_private_bounded_and_cli_still_exits_two(local,monkeypatch,capsys):
    installed_sources(local)
    account(local)
    def fail(*args,**kwargs):
        raise OSError(PRIVATE_VALUE)
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",fail)
    result=report(local)
    assert result["gameplan_runtime_compatibility"]["reason_codes"]==["GAMEPLAN_COMPATIBILITY_EVIDENCE_UNAVAILABLE"]
    assert PRIVATE_VALUE not in json.dumps(result)
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**kw:verified_absence(**kw))
    args=["--as-of",NOW]
    for name in ("repository","datastore","profile","watchlist"):
        args.extend(["--"+name,str(local[name])])
    assert readiness.main(args)==2
    output=json.loads(capsys.readouterr().out)
    assert output["status"]=="MANUAL_LIVE_PREFLIGHT_REQUIRED" and not output["manual_live_start_ready"]


def test_scout_source_does_not_enable_scout_live_execution(local,monkeypatch):
    installed_sources(local)
    profile=json.loads(local["profile"].read_text())
    profile.update(actor="Scout",machine="pc-new")
    local["profile"].write_text(json.dumps(profile))
    account(local,machine_id="pc-new",participants={"pc-new":["AAPL","IONQ"]})
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**kw:verified_absence(**kw))
    result=report(local)
    assert result["live_execution_implemented"] and not result["manual_live_source_ready"]
    assert "SCOUT_LOCAL_PRESTART_REQUIRED" in result["reason_codes"]
    assert "VERIFIED_EXISTING_ATLAS_ACCOUNT_MEMBERSHIP_REQUIRED" not in result["reason_codes"]
    assert result["capabilities"]["scout_prestart"]["connection_verified"] is False
    assert not result["execution_authorized"] and result["orders_placed"] == 0


def scout_profile(local):
    profile=json.loads(local["profile"].read_text())
    profile.update(actor="Scout",machine="pc-new")
    local["profile"].write_text(json.dumps(profile))


def local_prestart_result(**changes):
    return {"ready":True,"local_checks_passed":True,"mode":"broker-local",
            "runtime_started":False,"orders_or_commitments_created":False,
            "atlas_connection_required":False,**changes}


def test_scout_prepared_manual_source_needs_no_atlas_state_or_process_probe(local,monkeypatch):
    from ml.intraday import scout_prestart
    installed_sources(local)
    scout_profile(local)
    path=local["datastore"] / "state/account-gameplan/config.json"
    path.parent.mkdir(parents=True)
    path.write_text("broken other-trader config")
    monkeypatch.setattr(scout_prestart,"run_local_checks",lambda *a:local_prestart_result())
    monkeypatch.setattr(readiness,"read_gameplan_runtime_compatibility",lambda *a,**k:pytest.fail("Other trader dependency"))
    before=inventory(local["datastore"])
    result=report(local)
    assert result["manual_live_source_ready"] and result["status"]=="MANUAL_LIVE_PREFLIGHT_REQUIRED"
    assert result["reason_codes"]==[]
    assert result["gameplan_runtime_compatibility"]["status"]=="not_applicable_broker_local"
    assert result["capabilities"]["scout_prestart"]["local_checks_passed"]
    assert not result["manual_live_start_ready"] and result["manual_live_preflight_required"]
    assert not result["execution_authorized"] and not result["provider_availability_verified"]
    assert not result["broker_reconciliation_performed"] and result["orders_placed"]==0
    assert inventory(local["datastore"])==before


@pytest.mark.parametrize("changes",[{"ready":False},{"local_checks_passed":False},
    {"runtime_started":True},{"orders_or_commitments_created":True},{"atlas_connection_required":True}])
def test_scout_prestart_integrity_failure_still_blocks(local,monkeypatch,changes):
    from ml.intraday import scout_prestart
    installed_sources(local)
    scout_profile(local)
    monkeypatch.setattr(scout_prestart,"run_local_checks",lambda *a:local_prestart_result(**changes))
    result=report(local)
    assert not result["manual_live_source_ready"]
    assert "SCOUT_LOCAL_PRESTART_REQUIRED" in result["reason_codes"]


def test_scout_missing_prestart_evidence_is_private_and_blocked(local,monkeypatch):
    from ml.intraday import scout_prestart
    installed_sources(local)
    scout_profile(local)
    def missing(*args):
        raise OSError(PRIVATE_VALUE)
    monkeypatch.setattr(scout_prestart,"run_local_checks",missing)
    result=report(local)
    assert result["reason_codes"]==["SCOUT_LOCAL_PRESTART_REQUIRED"]
    assert PRIVATE_VALUE not in json.dumps(result)


def test_model_delegate_requires_actual_helper_fit_wiring(local):
    installed_sources(local)
    source=local["repository"] / "ml/intraday/models.py"
    tree=ast.parse(source.read_text())
    helper=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="fit_calibrated_estimator")
    helper.body=[ast.Expr(value=ast.Constant(value="build_estimator fit")),ast.Pass()]
    source.write_text(ast.unparse(tree))
    result=readiness._source_capabilities(local["repository"])
    assert "SOURCE_CAPABILITY_UNVERIFIED:forecasting_and_scoring" in result["missing_integration"]
    check=result["source_component_checks"]["forecasting_and_scoring"]["interface_evidence"]["ml/intraday/models.py:fit_calibrated_estimator"]
    assert not check["verified"] and check["required_calls_present"]==[]


@pytest.mark.parametrize("method", ["reserve_intraday", "reserve_remote_intraday", "_reserve_intraday"])
def test_shared_capacity_requires_both_participant_wrappers_and_common_pool_core(local, method):
    installed_sources(local)
    source = local["repository"] / "ml/account_gameplan/authority.py"
    tree = ast.parse(source.read_text())
    authority = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AccountAuthority")
    selected = next(node for node in authority.body if isinstance(node, ast.FunctionDef) and node.name == method)
    selected.body = [ast.Pass()]
    source.write_text(ast.unparse(tree))
    capabilities = readiness._source_capabilities(local["repository"])
    assert not capabilities["existing_shared_authority_supports_15m"]
    assert "SOURCE_CAPABILITY_UNVERIFIED:shared_actual_capacity" in capabilities["missing_integration"]
