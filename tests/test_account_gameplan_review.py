"""Native final handoff tests use frozen synthetic originals and no provider reads."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from ml.account_gameplan import review
from ml.account_gameplan.config import CONFIG, VERSION as CONFIG_VERSION, digest, load_account_config
from ml.artifacts import file_checksum, write_manifest
from test_account_gameplan_sources import DAY, native_case, export, save
from test_account_gameplan_accuracy import result_for
from test_account_gameplan_config import write_config

NEXT = "2026-10-06"
NOW = "2026-10-06T06:00:00Z"
DEADLINE = "2026-10-06T11:00:00Z"


def config(root, machine="pc-original", status="ACTIVE"):
    raw = write_config(root, machine=machine, status=status)
    if status == "ACTIVE":
        receipt = {"schema_version": CONFIG_VERSION, "status": "VERIFIED", "binding_sha256": raw["activation"]["binding_sha256"],
            "machine_id": machine, "coordinator_id": "pc-original", "peer_execution_fenced": True,
            "peer_fence_receipt_sha256": "b"*64, "migration_manifest_sha256": "c"*64,
            "fresh_union_reconciliation_sha256": "d"*64, "installed_source_commit": "e"*40, "orders_placed": 0}
        path = root / "state/account-gameplan/cutovers/reviewed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, receipt)
        raw["activation"].update(receipt_path=path.relative_to(root).as_posix(), receipt_sha256=file_checksum(path))
        save(root/CONFIG, raw)
    return load_account_config(root)


def successor(case):
    path = case.root/"ml/nightly-gameplan-runs/successor"
    path.mkdir()
    save(path/"gameplan.json", {"action_date": NEXT})
    write_manifest(path, run_timestamp=NOW, input_files=[], output_files=["gameplan.json"],
                   configuration={"target_contract_version": "independent-stock-targets-v1", "action_date": NEXT}, datastore_root=case.root)
    save(path/"receipt.json", {"action_date": NEXT, "published_at": NOW,
        "manifest_checksum_sha256": file_checksum(path/"manifest.json")})
    return path


def actuals(case, bundle, successor_path):
    run = case.root/"ml/gameplan-actuals-review-runs/result"
    run.mkdir(parents=True)
    result = result_for(bundle)
    result["rows"].to_parquet(run/"forecast-results.parquet", index=False)
    report = {"schema_version": "gameplan-actuals-review-v1", "status": "COMPLETE", "action_date": DAY,
        "source_gameplan_run": case.source.relative_to(case.root).as_posix(),
        "source_trade_plan_path": case.trade.as_posix(), "target_price_source_contract": "xnas-itch-archive-v1",
        "successor_gameplan_run": successor_path.relative_to(case.root).as_posix(), "successor_action_date": NEXT,
        "deadline_at": DEADLINE, "reviewed_at": NOW, "orders_placed": 0, "broker_orders_enabled": False}
    save(run/"report.json", report)
    bind_actuals(case.root, run, case.source, successor_path)
    return run


def bind_actuals(root, run, original, successor_path):
    report = json.loads((run/"report.json").read_text())
    write_manifest(run, run_timestamp=NOW, input_files=[original/"receipt.json", successor_path/"receipt.json"],
        output_files=["report.json", "forecast-results.parquet"],
        configuration={k: report[k] for k in ("action_date", "successor_gameplan_run")}, datastore_root=root)
    receipt = {"schema_version": "gameplan-actuals-review-v1", "status": "COMPLETE", "action_date": DAY,
        "run_path": run.relative_to(root).as_posix(), "manifest_sha256": file_checksum(run/"manifest.json"),
        "orders_placed": 0, "broker_orders_enabled": False}
    save(run/"receipt.json", receipt)
    pointer = root/"ml/gameplan-actuals-review-latest/run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    save(pointer, {"schema_version": "gameplan-actuals-review-v1", "current": {"run_path": receipt["run_path"],
        "action_date": DAY, "receipt_sha256": file_checksum(run/"receipt.json")}})


def setup(tmp_path, monkeypatch, machine="pc-original", status="ACTIVE"):
    cases = [native_case(tmp_path, monkeypatch, producer=p, symbol=s) for p,s in (("pc-original", "AAPL"),("pc-new", "MU"))]
    for case in cases:
        case.bundle = export(case, case.root/"ml/account-gameplan-source-runs"/file_checksum(case.source/"receipt.json"))
        case.successor = successor(case)
        case.actuals = actuals(case, case.bundle, case.successor)
    selected = next(case for case in cases if case.producer == machine)
    selected.config = config(selected.root, machine, status)
    selected.cases = cases
    return selected


def invoke(case, **kwargs):
    params = dict(successor_gameplan_run=case.successor, deadline=DEADLINE,
                  expected_config=case.config.fingerprint, clock=lambda: NOW)
    params.update(kwargs)
    return review.run_account_review(case.root, **params)


def registry(case):
    records = []
    for original in case.cases:
        if original is case:
            bundle, result = original.bundle.path, original.actuals
        else:
            base = case.root/"transported"/original.producer
            base.mkdir(parents=True)
            bundle, result = base/"bundle", base/"result"
            shutil.copytree(original.bundle.path, bundle)
            shutil.copytree(original.actuals, result)
        records.append({"producer_id": original.producer, "symbols": [original.symbol],
            "bundle_path": bundle.relative_to(case.root).as_posix(), "bundle_manifest_sha256": original.bundle.manifest_sha256,
            "result_path": result.relative_to(case.root).as_posix(), "result_manifest_sha256": file_checksum(result/"manifest.json")})
    path = case.root/f"state/account-gameplan/accuracy-inputs/{DAY}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, {"schema_version": review.VERSION, "action_date": DAY, "results": records})
    return path


@pytest.mark.parametrize("machine,status", [("pc-new","ACTIVE"),("pc-original","PREPARING")])
def test_producer_or_preparing_only_exports_exact_native_evidence(tmp_path, monkeypatch, machine, status):
    case = setup(tmp_path, monkeypatch, machine, status)
    original = {p: file_checksum(p) for folder in (case.source,case.trade,case.actuals) for p in folder.iterdir() if p.is_file()}
    result = invoke(case, sleeper=lambda _: pytest.fail("must not wait"))
    assert result["status"] == "SOURCE_READY" and "totals" not in result
    assert not (case.root/"ml/account-gameplan-accuracy-latest").exists()
    assert {p: file_checksum(p) for p in original} == original


def test_pending_then_complete_reuses_original_results_and_never_rebuilds_estimates(tmp_path, monkeypatch):
    case = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(review, "export_source_bundle", lambda *a, **kw: pytest.fail("existing immutable source must be reused"))
    before = {p: file_checksum(p) for folder in (case.source,case.trade,case.actuals) for p in folder.iterdir() if p.is_file()}
    first = invoke(case)
    assert first["status"] == "PENDING_PEER_RESULT" and "totals" not in first
    assert invoke(case) == first
    pending = case.root/f"state/account-gameplan/accuracy-pending/{DAY}/{first['native_result']['receipt_sha256']}.json"
    pending_bytes = pending.read_bytes()
    registry(case)
    complete = invoke(case)
    assert complete["status"] == "COMPLETE" and complete["totals"]["forecasts"] == 48
    assert complete["totals"]["direction_accuracy"] == 4/6
    assert complete["totals"]["pending_maturity"] == 34 and complete["totals"]["mature_awaiting_data"] == 8
    assert invoke(case) == complete
    assert pending.read_bytes() == pending_bytes
    assert {p: file_checksum(p) for p in before} == before
    assert len(list((case.root/"ml/account-gameplan-accuracy-runs").iterdir())) == 1


@pytest.mark.parametrize("failure", ["successor", "deadline", "result_pin", "source", "trade_missing", "manifest_input", "wrong_config", "late"])
def test_wrong_or_incomplete_local_native_handoff_cannot_publish_numbers(tmp_path, monkeypatch, failure):
    case = setup(tmp_path, monkeypatch)
    params = {}
    if failure in {"successor", "deadline", "source", "trade_missing"}:
        path = case.actuals/"report.json"
        value = json.loads(path.read_text())
        if failure == "successor": value["successor_gameplan_run"] = "ml/nightly-gameplan-runs/other"
        elif failure == "deadline": value["deadline_at"] = "2026-10-06T12:00:00Z"
        elif failure == "source": value["source_gameplan_run"] = "ml/nightly-gameplan-runs/other"
        else: value["source_trade_plan_path"] = None
        save(path, value)
        bind_actuals(case.root, case.actuals, case.source, case.successor)
    elif failure == "result_pin":
        with (case.actuals/"receipt.json").open("ab") as stream: stream.write(b" ")
    elif failure == "manifest_input":
        with (case.successor/"receipt.json").open("ab") as stream: stream.write(b" ")
    elif failure == "wrong_config": params["expected_config"] = "0"*64
    else: params["clock"] = lambda: DEADLINE
    if failure == "trade_missing":
        result = invoke(case)
        assert result["status"] == "UNAVAILABLE_LOCAL_SOURCE" and "totals" not in result
    else:
        with pytest.raises((ValueError, OSError)):
            invoke(case, **params)
    assert not (case.root/"ml/account-gameplan-accuracy-latest").exists()


def test_pending_retry_refuses_a_recomputed_local_result(tmp_path, monkeypatch):
    case = setup(tmp_path, monkeypatch)
    invoke(case)
    path = case.actuals/"report.json"
    value = json.loads(path.read_text())
    value["reviewed_at"] = "2026-10-06T05:59:00Z"
    save(path,value)
    bind_actuals(case.root,case.actuals,case.source,case.successor)
    with pytest.raises(ValueError, match="immutable"):
        invoke(case)


@pytest.mark.parametrize("record", ["accuracy-outgoing", "accuracy-pending"])
def test_source_or_pending_receipt_cannot_return_success_after_deadline(tmp_path, monkeypatch, record):
    case = setup(tmp_path,monkeypatch)
    current = [NOW]
    original = review._immutable
    def late(path, value):
        result = original(path, value)
        if record in str(path): current[0] = DEADLINE
        return result
    monkeypatch.setattr(review,"_immutable",late)
    with pytest.raises(ValueError,match="DEADLINE"):
        invoke(case,clock=lambda:current[0])
    assert not (case.root/"ml/account-gameplan-accuracy-latest/run.json").exists()


@pytest.mark.parametrize("prior", [None, b'{"old":"preserve"}'])
def test_postwrite_deadline_removes_only_new_pointers_and_restores_previous(tmp_path,monkeypatch,prior):
    case=setup(tmp_path,monkeypatch)
    registry(case)
    latest=case.root/"ml/account-gameplan-accuracy-latest/run.json"
    if prior is not None:
        latest.parent.mkdir(parents=True)
        latest.write_bytes(prior)
    current=[NOW]
    original=review.os.replace
    def late(source,target):
        original(source,target)
        if Path(target)==latest: current[0]=DEADLINE
    monkeypatch.setattr(review.os,"replace",late)
    with pytest.raises(ValueError,match="DEADLINE"):
        invoke(case,clock=lambda:current[0])
    assert (latest.read_bytes() if latest.exists() else None)==prior
    assert not (case.root/f"ml/account-gameplan-accuracy-by-date/{DAY}/run.json").exists()


def test_postwrite_independent_pointer_drift_is_preserved_and_reported(tmp_path,monkeypatch):
    case=setup(tmp_path,monkeypatch)
    registry(case)
    latest=case.root/"ml/account-gameplan-accuracy-latest/run.json"
    original=review.os.replace
    def drift(source,target):
        original(source,target)
        if Path(target)==latest: latest.write_bytes(b'{"independent":"writer"}')
    monkeypatch.setattr(review.os,"replace",drift)
    with pytest.raises(RuntimeError,match="rollback incomplete"):
        invoke(case)
    assert latest.read_bytes()==b'{"independent":"writer"}'
    assert not (case.root/f"ml/account-gameplan-accuracy-by-date/{DAY}/run.json").exists()


@pytest.mark.parametrize("failure", ["missing_peer", "same_producer", "bundle_pin", "result_pin", "source_substitution", "outside_root"])
def test_registry_requires_both_exact_original_sources(tmp_path, monkeypatch, failure):
    case = setup(tmp_path,monkeypatch)
    path = registry(case)
    data = json.loads(path.read_text())
    row = data["results"][1]
    if failure == "missing_peer": data["results"].pop()
    elif failure == "same_producer": row["producer_id"] = "pc-original"
    elif failure == "bundle_pin": row["bundle_manifest_sha256"] = "0"*64
    elif failure == "result_pin": row["result_manifest_sha256"] = "0"*64
    elif failure == "source_substitution": row["bundle_path"] = data["results"][0]["bundle_path"]
    else: row["result_path"] = "../outside"
    save(path,data)
    with pytest.raises(ValueError): invoke(case)
    assert not (case.root/"ml/account-gameplan-accuracy-latest").exists()


@pytest.mark.parametrize("status,code", [("PENDING_PEER_RESULT",2),("UNAVAILABLE_LOCAL_SOURCE",2),("SOURCE_READY",0),("COMPLETE",0)])
def test_cli_pending_cannot_claim_native_completion(tmp_path, monkeypatch, capsys, status, code):
    monkeypatch.setattr(review,"run_account_review",lambda *a,**kw:{"status":status})
    monkeypatch.setattr("datafetching.parquet_store.resolve_datastore_dir",lambda **kw:tmp_path)
    assert review.main(["--datastore",str(tmp_path),"--gameplan-run","explicit", "--deadline",DEADLINE,
                        "--expected-config","a"*64]) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


def unregistered_results(case):
    path = registry(case)
    records = json.loads(path.read_text())["results"]
    path.unlink()
    return path, records


def test_register_accuracy_validates_then_reuses_exact_immutable_registry(tmp_path, monkeypatch):
    case = setup(tmp_path, monkeypatch, status="PREPARING")
    path, records = unregistered_results(case)
    saved = review.register_inputs(case.root, action_date=DAY, results=list(reversed(records)),
                                   expected_config=case.config.fingerprint)
    before = path.read_bytes()
    assert [row["producer_id"] for row in saved["results"]] == ["pc-new", "pc-original"]
    assert review.register_inputs(case.root, action_date=DAY, results=records,
                                  expected_config=case.config.fingerprint) == saved
    assert path.read_bytes() == before
    assert not (case.root / "ml/account-gameplan-accuracy-latest").exists()
    changed = deepcopy(records)
    other = case.root / "transported/alternate-result"
    shutil.copytree(case.root / changed[0]["result_path"], other)
    changed[0]["result_path"] = other.relative_to(case.root).as_posix()
    with pytest.raises(ValueError, match="immutable"):
        review.register_inputs(case.root, action_date=DAY, results=changed, expected_config=case.config.fingerprint)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["date", "producer", "symbols", "bundle_pin", "result_pin", "config", "extra"])
def test_register_accuracy_rejects_mismatched_transport_without_writing(tmp_path, monkeypatch, failure):
    case = setup(tmp_path, monkeypatch, status="PREPARING")
    path, records = unregistered_results(case)
    expected, day = case.config.fingerprint, DAY
    if failure == "date": day = NEXT
    elif failure == "producer": records[1]["producer_id"] = records[0]["producer_id"]
    elif failure == "symbols": records[1]["symbols"] = ["IONQ"]
    elif failure in {"bundle_pin", "result_pin"}:
        records[1]["bundle_manifest_sha256" if failure == "bundle_pin" else "result_manifest_sha256"] = "0" * 64
    elif failure == "config": expected = "0" * 64
    else: records[1]["guess_latest"] = True
    with pytest.raises(ValueError):
        review.register_inputs(case.root, action_date=day, results=records, expected_config=expected)
    assert not path.exists() and not (path.parent / f"{NEXT}.json").exists()


def test_register_accuracy_keeps_original_smaller_saved_universes(tmp_path, monkeypatch):
    case = setup(tmp_path, monkeypatch, status="PREPARING")
    _, records = unregistered_results(case)
    value = json.loads((case.root / CONFIG).read_text())
    value["participants"]["pc-original"].append("NVDA")
    value["activation"]["binding_sha256"] = digest({key: val for key, val in value.items() if key != "activation"})
    save(case.root / CONFIG, value)
    saved = review.register_inputs(case.root, action_date=DAY, results=records,
                                   expected_config=load_account_config(case.root).fingerprint)
    assert next(row for row in saved["results"] if row["producer_id"] == "pc-original")["symbols"] == ["AAPL"]


@pytest.mark.parametrize("boundary", ["verification", "commit"])
def test_register_accuracy_refuses_configuration_change_at_write_boundary(tmp_path, monkeypatch, boundary):
    case = setup(tmp_path, monkeypatch, status="PREPARING")
    path, records = unregistered_results(case)
    function = "_read_registry" if boundary == "verification" else "_immutable"
    original = getattr(review, function)
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        value = json.loads((case.root / CONFIG).read_text())
        value["activation"]["status"] = "ACTIVE"
        save(case.root / CONFIG, value)
        return result
    monkeypatch.setattr(review, function, mutate)
    with pytest.raises(ValueError, match="configuration advanced"):
        review.register_inputs(case.root, action_date=DAY, results=records, expected_config=case.config.fingerprint)
    if boundary == "verification":
        assert not path.exists()
    else:
        assert json.loads(path.read_text())["status"] == "FAILED"
        assert len(list(path.parent.glob(f"{DAY}-failed-*.json"))) == 1
