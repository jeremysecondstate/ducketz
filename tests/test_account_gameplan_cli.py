import json
from pathlib import Path

import pytest

from ml.account_gameplan.cli import main
from ml.artifacts import file_checksum
from test_account_gameplan_sources import DAY, native_case, export, save
from test_account_gameplan_accuracy import native_accuracy
from test_gameplan_cash_ledger import snapshot


NOW = "2026-10-05T06:00:00Z"


@pytest.fixture
def selected(tmp_path, monkeypatch):
    sources = [export(native_case(tmp_path, monkeypatch, producer=producer, symbol=symbol))
               for producer, symbol in (("pc-original", "AAPL"), ("pc-new", "MU"))]
    registry = tmp_path / "sources.json"
    save(registry, {"sources": [{"producer_id": source.metadata["producer_id"],
        "bundle_path": source.path.relative_to(tmp_path).as_posix(), "manifest_sha256": source.manifest_sha256,
        "symbols": source.metadata["symbols"]} for source in sources]})
    state = snapshot(("AAPL", "MU"), cash=1000, equity=10000)
    state["observed_at"] = NOW
    state["account_fingerprint"] = "a" * 64
    snapshot_file = tmp_path / "snapshot.json"
    save(snapshot_file, state)
    return tmp_path, sources, registry, snapshot_file


def plan_args(selected, *, output="combined"):
    root, _, registry, snapshot_file = selected
    return ["plan", "--sources-json", str(registry), "--snapshot-json", str(snapshot_file),
            "--observed-at", NOW, "--output", str(root / output)]


def test_cli_export_and_verify_preserves_sources_and_never_activates(tmp_path, monkeypatch, capsys):
    native = native_case(tmp_path, monkeypatch)
    output = tmp_path / "exported"
    before = {path: file_checksum(path) for directory in (native.source, native.trade) for path in directory.iterdir()}
    args = ["export-source", "--datastore-root", str(native.root), "--gameplan-run", str(native.source),
        "--trade-plan-run", str(native.trade), "--output", str(output), "--producer", "Atlas", "--symbols", "AAPL"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "EXPORTED" and result["deployment_status"] == "NO_REGISTERED_HANDOFF"
    assert result["orders_placed"] == 0 and result["activation_changed"] is False
    assert main(["verify-source", "--bundle", str(output), "--manifest-sha256", result["manifest_sha256"],
                 "--producer", "Atlas", "--symbols", "AAPL", "--action-date", DAY]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"
    assert {path: file_checksum(path) for path in before} == before
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "FileExistsError"


def test_cli_plan_and_verify_use_explicit_inputs_without_updating_current_pointer(selected, capsys):
    root, _, _, _ = selected
    assert main(plan_args(selected), clock=lambda: NOW) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PUBLISHED" and result["forecast_rows"] == 48
    assert result["direction_projection_status"] == "COMPLETE"
    assert result["activation_changed"] is False
    assert not (root / "ml/account-gameplan-latest/run.json").exists()
    assert main(["verify", "--run", str(root / "combined"), "--manifest-sha256", result["manifest_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"
    assert main(plan_args(selected), clock=lambda: NOW) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "FileExistsError"


@pytest.mark.parametrize("failure", ["missing", "duplicate", "unpinned", "extra", "bad_snapshot", "deadline"])
def test_cli_fails_closed_without_guessing_or_activating(selected, capsys, failure):
    root, _, registry, snapshot_file = selected
    clock = lambda: NOW
    if failure == "missing": registry.unlink()
    elif failure in {"duplicate", "unpinned", "extra"}:
        document = json.loads(registry.read_text())
        if failure == "duplicate": document["sources"][1] = document["sources"][0]
        elif failure == "unpinned": document["sources"][0]["manifest_sha256"] = "0" * 64
        else: document["current"] = "guess-this"
        save(registry, document)
    elif failure == "bad_snapshot":
        state = json.loads(snapshot_file.read_text())
        state["ownership"]["safe_for_planning"] = False
        save(snapshot_file, state)
    else: clock = lambda: "2026-10-05T11:01:00Z"
    assert main(plan_args(selected), clock=clock) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "ERROR" and result["activation_changed"] is False
    assert not (root / "ml/account-gameplan-latest/run.json").exists()
    assert not (root / "combined/receipt.json").exists()


def test_cli_accuracy_binds_both_explicit_results_and_count_weights(selected, capsys):
    root, sources, registry, _ = selected
    results = []
    for source in sources:
        directory = root / f"results-{source.metadata['producer_id']}"
        directory.mkdir()
        run = native_accuracy(directory, source)
        results.append({"producer_id": source.metadata["producer_id"], "run_path": run.relative_to(root).as_posix(),
                        "manifest_sha256": file_checksum(run / "manifest.json")})
    specs = root / "results.json"
    save(specs, {"results": results})
    args = ["accuracy", "--sources-json", str(registry), "--results-json", str(specs), "--output", str(root / "accuracy")]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["totals"]["correct"] == 4 and summary["totals"]["directional_evaluated"] == 6
    report = json.loads((root / "accuracy/report.json").read_text())
    assert len(report["sources"]) == 2 and len(report["forecast_identities"]) == 48
    assert report["promoted_totals"]["direction_accuracy"] is None
    assert summary["manifest_sha256"] == file_checksum(root / "accuracy/manifest.json")
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "FileExistsError"


def test_cli_requires_pins_and_has_no_current_or_execute_defaults():
    with pytest.raises(SystemExit):
        main(["verify", "--run", "missing"])
    with pytest.raises(SystemExit):
        main(["plan", "--execute"])


def test_cli_register_accuracy_uses_explicit_verified_transport_and_is_idempotent(tmp_path, monkeypatch, capsys):
    from test_account_gameplan_review import setup, unregistered_results
    case = setup(tmp_path, monkeypatch, status="PREPARING")
    path, records = unregistered_results(case)
    spec = tmp_path / "transported-results.json"
    save(spec, {"results": records})
    args = ["register-accuracy", "--datastore-root", str(case.root), "--action-date", DAY,
            "--results", str(spec), "--expected-config", case.config.fingerprint]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "REGISTERED" and summary["activation_changed"] is False
    assert summary["orders_placed"] == 0 and summary["broker_orders_enabled"] is False
    before = path.read_bytes()
    assert main(args) == 0 and path.read_bytes() == before
    capsys.readouterr()
    save(spec, {"results": records, "current": "guess"})
    assert main(args) == 2 and path.read_bytes() == before
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"
