from copy import deepcopy
import json

import pandas as pd
import pytest

from ml.account_gameplan.accuracy import aggregate_accuracy, read_source_accuracy
from ml.artifacts import file_checksum, write_manifest
from test_account_gameplan_sources import DAY, native_case, export, save


def result_for(bundle, *, evaluated=3, correct=2, neutral=1, missing=4):
    rows = bundle.forecasts.copy()
    # Test fixtures may set a source's neutral forecast before export; matching
    # frozen direction remains mandatory for both reads and aggregation.
    rows["actuals_status"] = "PENDING_MATURITY"
    rows["actual_return"] = None
    rows["direction_correct"] = None
    evaluated_indices = rows.index[:evaluated]
    for offset, index in enumerate(evaluated_indices):
        change = .01 if offset < correct else -.01
        rows.loc[index, "actuals_status"] = "EVALUATED"
        rows.loc[index, "actual_return"] = change
        rows.loc[index, "direction_correct"] = change > 0 if rows.loc[index, "direction"] == "BULLISH" else None
    rows.loc[rows.index[evaluated:evaluated + missing], "actuals_status"] = "MATURE_AWAITING_DATA"
    return {"producer_id": bundle.metadata["producer_id"], "source_receipt_sha256": bundle.metadata["source_receipt_sha256"],
        "source_bundle_manifest_sha256": bundle.manifest_sha256, "action_date": DAY,
        "result_manifest_sha256": "a" * 64, "rows": rows}


@pytest.fixture
def pair(tmp_path, monkeypatch):
    atlas = native_case(tmp_path, monkeypatch, producer="Atlas", symbol="AAPL")
    scout = native_case(tmp_path, monkeypatch, producer="Scout", symbol="MU")
    return [export(atlas), export(scout)]


def test_accuracy_uses_correct_and_evaluated_counts_not_average_percentages(pair):
    results = [result_for(pair[0], evaluated=2, correct=2, missing=3),
               result_for(pair[1], evaluated=10, correct=2, missing=4)]
    report = aggregate_accuracy(pair, results)
    assert report["totals"] == {"forecasts": 48, "evaluated": 12, "directional_evaluated": 12,
        "correct": 4, "incorrect": 8, "direction_accuracy": 4 / 12, "neutral_total": 0, "neutral_evaluated": 0,
        "pending_maturity": 29, "mature_awaiting_data": 7}
    assert report["totals"]["direction_accuracy"] != (1 + .2) / 2
    assert report["by_producer"]["Atlas"]["correct"] == 2
    assert report["promoted_totals"]["directional_evaluated"] == 0
    assert report["promoted_totals"]["direction_accuracy"] is None
    assert report["by_model_status"]["RESEARCH_NOT_PROMOTED"] == report["totals"]
    assert len(set(report["forecast_identities"])) == 48
    assert report["forecast_identities"][0].startswith("Atlas:" + pair[0].metadata["source_receipt_sha256"] + ":")


def test_neutral_pending_and_missing_remain_outside_direction_denominator(pair):
    # Preserve the source and result together for a pure aggregation fixture.
    pair[0].forecasts.loc[0, "direction"] = "NO_EDGE"
    pair[0].forecasts.loc[0, "calibrated_probability"] = .5
    results = [result_for(pair[0], evaluated=2, correct=2), result_for(pair[1], evaluated=0, correct=0)]
    report = aggregate_accuracy(pair, results)
    assert report["totals"]["evaluated"] == 2
    assert report["totals"]["directional_evaluated"] == report["totals"]["correct"] == 1
    assert report["totals"]["neutral_evaluated"] == report["totals"]["neutral_total"] == 1
    assert report["by_producer"]["Scout"]["direction_accuracy"] is None


@pytest.mark.parametrize("failure", ["one", "duplicate", "producer", "receipt", "bundle", "date", "missing_row", "changed_probability", "pending_return", "wrong_correct", "nan", "unknown_status", "wrong_score"])
def test_accuracy_rejects_source_substitution_and_forged_outcomes(pair, failure):
    results = [result_for(source) for source in pair]
    if failure == "one": results.pop()
    elif failure == "duplicate": results[1] = results[0]
    elif failure == "producer": results[1]["producer_id"] = "unknown"
    elif failure == "receipt": results[1]["source_receipt_sha256"] = "0" * 64
    elif failure == "bundle": results[1]["source_bundle_manifest_sha256"] = "0" * 64
    elif failure == "date": results[1]["action_date"] = "2026-10-06"
    elif failure == "missing_row": results[1]["rows"] = results[1]["rows"].iloc[1:]
    elif failure == "changed_probability": results[1]["rows"].loc[0, "calibrated_probability"] = .7
    elif failure == "pending_return": results[1]["rows"].loc[23, "actual_return"] = .02
    elif failure == "wrong_correct": results[1]["rows"].loc[0, "direction_correct"] = False
    elif failure == "nan": results[1]["rows"].loc[0, "actual_return"] = float("nan")
    elif failure == "unknown_status": results[1]["rows"].loc[0, "actuals_status"] = "FAKE_PASS"
    else: results[1]["rows"]["model_brier_score"] = 999
    with pytest.raises(ValueError):
        aggregate_accuracy(pair, results)


def native_accuracy(tmp_path, bundle):
    run = tmp_path / "actuals"
    run.mkdir()
    result = result_for(bundle)
    result["rows"].to_parquet(run / "forecast-results.parquet", index=False)
    metadata = bundle.metadata
    report = {"schema_version": "gameplan-actuals-review-v1", "status": "COMPLETE", "action_date": DAY,
        "source_gameplan_run": metadata["source_gameplan_run"], "target_price_source_contract": metadata["target_price_source_contract"]}
    save(run / "report.json", report)
    write_manifest(run, run_timestamp="2026-10-06T06:00:00Z", input_files=[],
        output_files=["report.json", "forecast-results.parquet"], configuration={"action_date": DAY})
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["input_files"] = [{"path": metadata["source_gameplan_run"] + "/receipt.json", "status": "present",
                               "checksum_sha256": metadata["source_receipt_sha256"]}]
    save(run / "manifest.json", manifest)
    save(run / "receipt.json", {"schema_version": "gameplan-actuals-review-v1", "status": "COMPLETE", "action_date": DAY,
        "manifest_sha256": file_checksum(run / "manifest.json"), "orders_placed": 0, "broker_orders_enabled": False})
    return run


def test_native_accuracy_reads_explicit_source_and_no_latest_selection(tmp_path, pair):
    run = native_accuracy(tmp_path, pair[0])
    result = read_source_accuracy(run, bundle=pair[0], expected_manifest_sha256=file_checksum(run / "manifest.json"))
    assert len(result["rows"]) == 24 and result["producer_id"] == "Atlas"
    with pytest.raises(ValueError, match="receipt"):
        read_source_accuracy(run, bundle=pair[1], expected_manifest_sha256=file_checksum(run / "manifest.json"))


@pytest.mark.parametrize("failure", ["manifest", "output", "source_input", "report_source", "incomplete"])
def test_native_accuracy_rejects_invalid_proof_even_after_output_rebinding(tmp_path, pair, failure):
    run = native_accuracy(tmp_path, pair[0])
    expected = file_checksum(run / "manifest.json")
    if failure == "manifest": expected = "0" * 64
    elif failure == "output":
        with (run / "forecast-results.parquet").open("ab") as stream: stream.write(b"changed")
    else:
        manifest = json.loads((run / "manifest.json").read_text())
        if failure == "source_input": manifest["input_files"][0]["checksum_sha256"] = "0" * 64
        else:
            path = run / "report.json"
            report = json.loads(path.read_text())
            report["source_gameplan_run" if failure == "report_source" else "status"] = "wrong"
            save(path, report)
            manifest["output_files"]["report.json"]["checksum_sha256"] = file_checksum(path)
            manifest["output_files"]["report.json"]["size"] = path.stat().st_size
        save(run / "manifest.json", manifest)
        expected = file_checksum(run / "manifest.json")
        receipt = json.loads((run / "receipt.json").read_text())
        receipt["manifest_sha256"] = expected
        save(run / "receipt.json", receipt)
    with pytest.raises((ValueError, RuntimeError)):
        read_source_accuracy(run, bundle=pair[0], expected_manifest_sha256=expected)


def test_native_accuracy_parses_only_exact_manifest_bound_bytes(tmp_path, pair, monkeypatch):
    from ml.account_gameplan import accuracy
    run = native_accuracy(tmp_path, pair[0])
    expected = file_checksum(run / "manifest.json")
    original = accuracy.verify_manifest
    def replace_after_verification(path):
        manifest = original(path)
        rows = pd.read_parquet(run / "forecast-results.parquet")
        rows.loc[0, "actual_return"] = .2
        rows.to_parquet(run / "forecast-results.parquet", index=False)
        return manifest
    monkeypatch.setattr(accuracy, "verify_manifest", replace_after_verification)
    with pytest.raises(ValueError, match="Portable bytes differ"):
        read_source_accuracy(run, bundle=pair[0], expected_manifest_sha256=expected)
