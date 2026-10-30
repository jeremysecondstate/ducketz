"""Synthetic private-report fixtures; no operating Scout data or transport."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ui.scout_stats_report import load_scout_report, scout_report_sessions


DAY = "2030-01-02"
TARGET = ("Third 15-minute candle close compared with the first reference candle open; "
          "unchanged counts as not-down.")


def example_report():
    rows = []
    for symbol, forecasts, scored, missing, synthetic, correct, brier, confusion, statuses, submitted, filled, unknown, buy, sell, buy_amount, sell_amount in (
        ("TEST", 2, 1, 1, 0, 1, .1, {"true_not_down":1}, {"FILLED":1,"not_submitted":1}, 10, 10, 0, 10, 0, "20.00", "0"),
        ("DEMO", 3, 2, 0, 1, 1, .3, {"true_down":1,"false_not_down":1}, {"FILLED":1,"CANCELLED":1,"UNKNOWN":1}, 7, 5, 1, 0, 5, "0", "15.00"),
    ):
        positive = confusion.get("true_not_down",0) + confusion.get("false_down",0)
        rows.append(dict(session_id=DAY, pc="Scout", symbol=symbol, forecasts=forecasts, scored=scored,
            missing_outcomes=missing, synthetic_outcomes=synthetic, other_unscored_outcomes=0,
            unscored_outcomes=forecasts-scored, late_forecasts=0, accuracy=correct/scored, brier=brier,
            observed_not_down_fraction=positive/scored, majority_baseline_accuracy=max(positive,scored-positive)/scored,
            extreme_probability_count=0, wrong_extreme_probability_count=0,
            submitted_quantity=submitted, filled_quantity=filled, submission_unknown=unknown,
            realized_pnl=None, trade_result_basis="saved_execution_projections_only",
            limitations=["Synthetic test evidence; P&L is unknown."], status="reviewed", correct=correct,
            scored_coverage=scored/forecasts, execution_status_counts=statuses, confusion_matrix=confusion,
            filled_orders=1, fill_details=dict(buy_quantity=buy, sell_quantity=sell,
                buy_notional=buy_amount, sell_notional=sell_amount, fill_events=1)))
    aggregate = dict(forecasts=5, scored=3, missing_outcomes=1, synthetic_outcomes=1,
        other_unscored_outcomes=0, unscored_outcomes=2, late_forecasts=0,
        extreme_probability_count=0, wrong_extreme_probability_count=0,
        submitted_quantity=17, filled_quantity=15, submission_unknown=1, correct=2,
        accuracy=2/3, brier=(.1+.3*2)/3, scored_coverage=3/5,
        observed_not_down_fraction=1/3, realized_pnl=None,
        execution_status_counts={"FILLED":2,"not_submitted":1,"CANCELLED":1,"UNKNOWN":1},
        execution_dispositions={"saved":3,"no_capacity":1,"unknown":1},
        confusion_matrix={"true_not_down":1,"true_down":1,"false_not_down":1},
        journal_request_states={"ACCEPTED":3,"UNKNOWN":1}, unique_fill_events=2,
        majority_baseline_accuracy=2/3, buy_quantity=10, buy_notional="20.00",
        sell_quantity=5, sell_notional="15.00")
    return dict(schema="scout-15m-session-stats-v1", producer="Scout", session_date=DAY,
        timezone="America/Los_Angeles", as_of_utc="2030-01-03T01:00:00Z", target_definition=TARGET,
        scope="Synthetic saved own-session aggregate fixtures.", aggregate=aggregate,
        symbols=rows, limitations=["Synthetic observed evidence only."])


def archive(root, report=None, *, completion="20300103T010000Z-synthetic-scout-report", raw=None):
    report = example_report() if report is None else deepcopy(report)
    directory = root / "intraday/scout/statistics-reports/v1" / completion
    directory.mkdir(parents=True)
    json_bytes = json.dumps(report,allow_nan=False).encode() if raw is None else raw
    markdown_bytes = b"# Synthetic aggregate report\n\nP&L is unknown.\n"
    (directory / "report.json").write_bytes(json_bytes)
    (directory / "report.md").write_bytes(markdown_bytes)
    receipt = dict(schema="scout-15m-statistics-report-receipt-v1", producer="Scout",
        completion_id=completion, session_date=report["session_date"],
        as_of_utc=report["as_of_utc"],
        json_sha256=hashlib.sha256(json_bytes).hexdigest(), markdown_sha256=hashlib.sha256(markdown_bytes).hexdigest(),
        verified_at="2030-01-03T04:00:00Z")
    (directory / "receipt.json").write_text(json.dumps(receipt),encoding="utf-8")
    return directory


def repin(directory):
    path = directory / "receipt.json"
    receipt = json.loads(path.read_text("utf-8"))
    receipt["json_sha256"] = hashlib.sha256((directory / "report.json").read_bytes()).hexdigest()
    receipt["markdown_sha256"] = hashlib.sha256((directory / "report.md").read_bytes()).hexdigest()
    path.write_text(json.dumps(receipt),encoding="utf-8")


def assert_unavailable(result):
    assert result.status == "unreadable"
    assert result.aggregate == {} and result.symbols == ()
    assert "could not be verified" in result.message


def test_missing_report_does_not_create_storage(tmp_path):
    before = tuple(tmp_path.iterdir())
    assert scout_report_sessions(tmp_path) == ()
    result = load_scout_report(tmp_path, DAY)
    assert result.session == DAY and result.status == "missing"
    assert result.aggregate == {} and result.symbols == ()
    assert tuple(tmp_path.iterdir()) == before


def test_valid_report_preserves_original_aggregates_without_inventing_detail(tmp_path):
    report = example_report()
    directory = archive(tmp_path,report)
    before = {path.name:path.read_bytes() for path in directory.iterdir()}
    result = load_scout_report(tmp_path,DAY)
    assert result.status == "available" and result.session == DAY
    assert result.aggregate == report["aggregate"]
    assert result.symbols == tuple(report["symbols"])
    assert result.generated_at == report["as_of_utc"] and result.target_definition == TARGET
    assert result.report_sha256 == hashlib.sha256(before["report.json"]).hexdigest()
    assert result.completion_id == directory.name
    assert {row["symbol"] for row in result.symbols} == {"TEST","DEMO"}
    assert not hasattr(result,"forecasts") and not hasattr(result,"orders")
    assert any("individual forecast" in text.lower() for text in result.limitations)
    assert any("p&l is unknown" in text.lower() for text in result.limitations)
    assert {path.name:path.read_bytes() for path in directory.iterdir()} == before


def test_unregistered_staging_directory_is_not_a_saved_report(tmp_path):
    path = tmp_path / "intraday/scout/statistics-reports/v1/staging"
    path.mkdir(parents=True)
    (path / "report.json").write_text("unfinished",encoding="utf-8")
    assert scout_report_sessions(tmp_path) == ()
    assert load_scout_report(tmp_path,DAY).status == "missing"


def test_session_discovery_and_default_load_use_original_session_dates(tmp_path):
    archive(tmp_path)
    later = example_report()
    later["session_date"] = "2030-01-03"
    later["as_of_utc"] = "2030-01-03T03:00:00Z"
    for row in later["symbols"]:
        row["session_id"] = later["session_date"]
    archive(tmp_path,later,completion="20300103T030000Z-synthetic-report-later")
    assert scout_report_sessions(tmp_path) == ("2030-01-03",DAY)
    assert load_scout_report(tmp_path).session == "2030-01-03"
    assert load_scout_report(tmp_path,DAY).aggregate == example_report()["aggregate"]


def test_latest_original_report_cutoff_wins_over_import_or_completion_order(tmp_path):
    original = archive(tmp_path,completion="20300103T030000Z-synthetic-imported-later")
    newer = example_report()
    newer["as_of_utc"] = "2030-01-03T02:00:00Z"
    chosen = archive(tmp_path,newer,completion="20300103T020000Z-synthetic-newer-report")
    result = load_scout_report(tmp_path,DAY)
    assert result.status == "available" and result.completion_id == chosen.name
    assert original.is_dir()


@pytest.mark.parametrize("damage", ["json_bytes","markdown_bytes","missing_markdown","json_hash","malformed_json","missing_json","metrics","earlier_payload_clock","earlier_payload_clock_repinned"])
def test_damaged_latest_report_never_substitutes_older_valid_metrics(tmp_path,damage):
    original = archive(tmp_path)
    before = {path.name:path.read_bytes() for path in original.iterdir()}
    newer = example_report()
    newer["as_of_utc"] = "2030-01-03T02:00:00Z"
    latest = archive(tmp_path,newer,completion="20300103T020000Z-synthetic-newer-report")
    if damage == "json_bytes":
        with (latest/"report.json").open("ab") as handle:
            handle.write(b" ")
    elif damage == "markdown_bytes":
        (latest/"report.md").write_bytes(b"changed")
    elif damage == "missing_markdown":
        (latest/"report.md").unlink()
    elif damage == "json_hash":
        path = latest/"receipt.json"
        receipt = json.loads(path.read_text("utf-8")); receipt["json_sha256"] = "f"*64
        path.write_text(json.dumps(receipt),encoding="utf-8")
    elif damage == "malformed_json":
        (latest/"report.json").write_bytes(b"broken")
    elif damage == "missing_json":
        (latest/"report.json").unlink()
    elif damage == "metrics":
        newer["aggregate"]["accuracy"] = .99
        (latest/"report.json").write_text(json.dumps(newer),encoding="utf-8"); repin(latest)
    else:
        newer["as_of_utc"] = "2030-01-03T00:00:00Z"
        (latest/"report.json").write_text(json.dumps(newer),encoding="utf-8")
        if damage == "earlier_payload_clock_repinned":
            repin(latest)
    result = load_scout_report(tmp_path,DAY)
    assert_unavailable(result)
    assert result.completion_id == latest.name
    assert scout_report_sessions(tmp_path) == (DAY,)
    assert {path.name:path.read_bytes() for path in original.iterdir()} == before


@pytest.mark.parametrize("damage", [
    "producer","timezone","session","target","coverage","correct","accuracy","brier",
    "weighted_brier","scored_coverage","confusion","observed_fraction","baseline","status_counts",
    "dispositions","journal_unknown","submitted_quantity","symbol_sum","duplicate_symbol","symbol_owner",
    "symbol_session","filled_orders","fill_quantity","fill_events","notional","nonfinite_notional",
    "exponent_notional","pnl","extra_raw_rows","missing_widget_field","bool_count",
])
def test_valid_hashes_do_not_hide_incompatible_or_inconsistent_metrics(tmp_path,damage):
    report = example_report(); a = report["aggregate"]; row = report["symbols"][0]
    if damage == "producer": report["producer"] = "Atlas"
    elif damage == "timezone": report["timezone"] = "UTC"
    elif damage == "session": report["session_date"] = "2030-01-01"
    elif damage == "target": report["target_definition"] = "Different price target."
    elif damage == "coverage": a["missing_outcomes"] += 1
    elif damage == "correct": a["correct"] = 4
    elif damage == "accuracy": a["accuracy"] = .5
    elif damage == "brier": a["brier"] = None
    elif damage == "weighted_brier": a["brier"] = .8
    elif damage == "scored_coverage": a["scored_coverage"] = .1
    elif damage == "confusion": a["confusion_matrix"]["true_down"] += 1
    elif damage == "observed_fraction": a["observed_not_down_fraction"] = .9
    elif damage == "baseline": a["majority_baseline_accuracy"] = .1
    elif damage == "status_counts": a["execution_status_counts"]["FILLED"] += 1
    elif damage == "dispositions": a["execution_dispositions"]["saved"] += 1
    elif damage == "journal_unknown": a["journal_request_states"]["UNKNOWN"] = 0
    elif damage == "submitted_quantity": row["submitted_quantity"] = 0
    elif damage == "symbol_sum": row["late_forecasts"] = 1
    elif damage == "duplicate_symbol": report["symbols"][1]["symbol"] = "TEST"
    elif damage == "symbol_owner": row["pc"] = "Atlas"
    elif damage == "symbol_session": row["session_id"] = "2030-01-01"
    elif damage == "filled_orders": row["filled_orders"] = 0
    elif damage == "fill_quantity": row["fill_details"]["buy_quantity"] = 0
    elif damage == "fill_events": a["unique_fill_events"] += 1
    elif damage == "notional": row["fill_details"]["buy_notional"] = "99.0"
    elif damage == "nonfinite_notional": a["buy_notional"] = "NaN"
    elif damage == "exponent_notional": a["buy_notional"] = "1e99999999999999"
    elif damage == "pnl": a["realized_pnl"] = 10
    elif damage == "extra_raw_rows": report["orders"] = [{"request_id":"synthetic-raw-identity"}]
    elif damage == "missing_widget_field": del a["unique_fill_events"]
    elif damage == "bool_count": a["forecasts"] = True
    directory = archive(tmp_path,report)
    if damage == "session":
        receipt = json.loads((directory/"receipt.json").read_text("utf-8"))
        receipt["session_date"] = DAY
        (directory/"receipt.json").write_text(json.dumps(receipt),encoding="utf-8")
    assert_unavailable(load_scout_report(tmp_path,DAY))


def test_zero_population_keeps_accuracy_and_brier_unknown(tmp_path):
    report = example_report(); aggregate = report["aggregate"]
    for name,value in list(aggregate.items()):
        if type(value) is int: aggregate[name] = 0
        elif type(value) is float: aggregate[name] = None
        elif isinstance(value,dict): aggregate[name] = {}
        elif isinstance(value,str): aggregate[name] = "0"
    report["symbols"] = []
    archive(tmp_path,report)
    result = load_scout_report(tmp_path,DAY)
    assert result.status == "available"
    assert result.aggregate["forecasts"] == 0
    assert result.aggregate["accuracy"] is result.aggregate["brier"] is None


@pytest.mark.parametrize("damage", ["duplicate_key","nonfinite_json","oversize_json","oversize_markdown","oversize_receipt"])
def test_bounded_strict_json_and_receipt_validation(tmp_path,damage):
    directory = archive(tmp_path)
    if damage == "duplicate_key":
        original = (directory/"report.json").read_text("utf-8")
        (directory/"report.json").write_text(original[:-1]+',"producer":"Scout"}',encoding="utf-8")
        repin(directory)
    elif damage == "nonfinite_json":
        raw = (directory/"report.json").read_text("utf-8").replace('"accuracy": 0.6666666666666666','"accuracy": NaN')
        (directory/"report.json").write_text(raw,encoding="utf-8"); repin(directory)
    elif damage == "oversize_json":
        (directory/"report.json").write_bytes(b" "*(2*1024*1024+1)); repin(directory)
    elif damage == "oversize_markdown":
        (directory/"report.md").write_bytes(b" "*(2*1024*1024+1)); repin(directory)
    else:
        (directory/"receipt.json").write_bytes(b" "*65537)
    assert_unavailable(load_scout_report(tmp_path,DAY))


def test_receipt_metadata_may_include_source_links_without_changing_report(tmp_path):
    directory = archive(tmp_path)
    path = directory/"receipt.json"; receipt = json.loads(path.read_text("utf-8"))
    receipt.update(created_at="2030-01-03T03:00:00Z", source_url="https://example.invalid/synthetic")
    path.write_text(json.dumps(receipt),encoding="utf-8")
    assert load_scout_report(tmp_path,DAY).status == "available"


def test_untrusted_receipt_timestamp_or_identity_stays_unavailable(tmp_path):
    directory = archive(tmp_path)
    path = directory/"receipt.json"; receipt = json.loads(path.read_text("utf-8"))
    receipt["verified_at"] = "2030-01-03T00:00:00Z"
    path.write_text(json.dumps(receipt),encoding="utf-8")
    assert_unavailable(load_scout_report(tmp_path,DAY))


def test_linked_report_bytes_are_not_read(tmp_path):
    directory = archive(tmp_path)
    path = directory/"report.json"
    outside = tmp_path/"synthetic-outside.json"; outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    assert_unavailable(load_scout_report(tmp_path,DAY))


def test_windows_reparse_report_is_unavailable_without_symlink_privilege(tmp_path,monkeypatch):
    directory = archive(tmp_path)
    report_path = directory/"report.json"
    original = Path.lstat
    def recorded_reparse(path, *args, **kwargs):
        info = original(path,*args,**kwargs)
        if path == report_path:
            return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=1024)
        return info
    monkeypatch.setattr(Path,"lstat",recorded_reparse)
    assert_unavailable(load_scout_report(tmp_path,DAY))


def test_receipt_missing_pinned_cutoff_never_exposes_older_report(tmp_path):
    archive(tmp_path)
    report = example_report(); report["as_of_utc"] = "2030-01-03T02:00:00Z"
    latest = archive(tmp_path,report,completion="20300103T020000Z-synthetic-newer-report")
    path=latest/"receipt.json"; receipt=json.loads(path.read_text("utf-8"))
    del receipt["as_of_utc"]
    path.write_text(json.dumps(receipt),encoding="utf-8")
    result=load_scout_report(tmp_path,DAY)
    assert_unavailable(result)
    assert result.completion_id == latest.name


def test_other_session_receipt_damage_does_not_replace_selected_report(tmp_path):
    archive(tmp_path)
    report = example_report(); report["session_date"] = "2030-01-01"
    for row in report["symbols"]: row["session_id"] = report["session_date"]
    bad = archive(tmp_path,report,completion="20300103T020000Z-synthetic-other-session")
    receipt_path=bad/"receipt.json"; receipt=json.loads(receipt_path.read_text("utf-8"))
    receipt["json_sha256"] = "invalid"
    receipt_path.write_text(json.dumps(receipt),encoding="utf-8")
    assert load_scout_report(tmp_path,DAY).status == "available"
