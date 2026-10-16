import json

import pandas as pd
import pytest

import ml.gameplan_stats_handoff as stats_handoff
from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import publish_completed_session_review
from ml.gameplan_stats_handoff import export_stats_package, adopt_combined_stats, read_stats_package, NO_SAVED_FORECASTS, ROW_FIELDS
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET, LEGACY_COST_TARGET
from tests.gameplan_stats_fixture import forecast, write_review


@pytest.fixture(autouse=True)
def isolated_nightly_context(monkeypatch):
    monkeypatch.delenv("DUCKETZ_NIGHTLY_ACTION_DATE", raising=False)


@pytest.fixture
def packages(tmp_path):
    owners = {"Atlas": ("AAPL",), "Scout": ("DOCU",)}
    paths = {}
    for actor, symbols in owners.items():
        root = tmp_path / actor
        rows = [forecast(symbols[0]), forecast(symbols[0], hour=5, direction="NO_EDGE", probability=.5)]
        rows[0].update(account_number="PRIVATE", available_cash=1234, trade_quantity=42, private_model_path="PRIVATE")
        write_review(root, rows)
        paths[actor] = export_stats_package(root, producer=actor, symbols=symbols,
                                            destination=tmp_path / "packages" / f"{actor}.json")
    return paths, owners


def adopt(root, paths, owners):
    return adopt_combined_stats(root, packages=paths, expected_symbols=owners,
        expected_sha256={actor: file_checksum(path) for actor, path in paths.items()}, reviewed_at="2026-09-12T07:00Z")


def test_sanitized_packages_roundtrip_into_existing_stats_ui(tmp_path, packages):
    paths, owners = packages
    for actor, path in paths.items():
        text = path.read_text()
        assert "PRIVATE" not in text and "trade_quantity" not in text and "available_cash" not in text
        payload, frame = read_stats_package(path, expected_sha256=file_checksum(path))
        assert payload["producer"] == actor and len(frame) == 2
    run = adopt(tmp_path / "combined", paths, owners)
    verify_manifest(run)
    review = load_gameplan_stats(tmp_path / "combined")
    assert review.run_directory == run and review.symbols == ("AAPL", "DOCU")
    assert review.metrics().total == 4 and review.metrics().scored == 2 and review.metrics().neutral == 2
    assert review.metrics().brier == pytest.approx((.04 + .25) / 2)
    report = json.loads((run / "report.json").read_text())
    assert report["producers"]["Scout"]["package_sha256"] == file_checksum(paths["Scout"])


def test_package_hash_and_unknown_fields_fail_before_adoption(tmp_path, packages):
    paths, owners = packages
    before = file_checksum(paths["Scout"])
    payload = json.loads(paths["Scout"].read_text())
    payload["rows"][0]["account_number"] = "PRIVATE"
    paths["Scout"].write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="reviewed package hash"):
        read_stats_package(paths["Scout"], expected_sha256=before)
    with pytest.raises(ValueError, match="unsupported prediction fields"):
        adopt(tmp_path / "combined", paths, owners)
    assert not (tmp_path / "combined/ml/gameplan-actuals-review-latest/run.json").exists()


@pytest.mark.parametrize("damage,message", [
    ("producer", "producer or frozen"), ("duplicate", "duplicate forecast"),
    ("metric", "probability error"), ("future", "after its cutoff"),
])
def test_combined_validation_keeps_existing_pointer(tmp_path, packages, damage, message):
    paths, owners = packages
    root = tmp_path / "combined"
    adopt(root, paths, owners)
    pointer = root / "ml/gameplan-actuals-review-latest/run.json"
    before = pointer.read_bytes()
    scout = json.loads(paths["Scout"].read_text())
    if damage == "producer":
        scout["producer"] = "Atlas"
    elif damage == "duplicate":
        scout["rows"][0]["id"] = json.loads(paths["Atlas"].read_text())["rows"][0]["id"]
    elif damage == "metric":
        scout["rows"][0]["model_brier_score"] = .9
    else:
        scout["rows"][0]["actual_end_observed_at"] = "2026-09-12T02:00Z"
    paths["Scout"].write_text(json.dumps(scout))
    with pytest.raises(ValueError, match=message):
        adopt(root, paths, owners)
    assert pointer.read_bytes() == before


def test_different_probability_targets_are_preserved_and_not_averaged(tmp_path, packages):
    paths, owners = packages
    root = tmp_path / "raw"
    row = forecast("DOCU")
    row.update(probability_target_contract=RAW_DIRECTION_TARGET, gameplan_variant="YG")
    write_review(root, [row])
    paths["Scout"] = export_stats_package(root, producer="Scout", symbols=owners["Scout"], destination=tmp_path / "raw.json")
    payload, frame = read_stats_package(paths["Scout"], expected_sha256=file_checksum(paths["Scout"]))
    assert payload["probability_target_contract"] == RAW_DIRECTION_TARGET
    assert frame.probability_target_contract.eq(RAW_DIRECTION_TARGET).all()
    with pytest.raises(ValueError, match="different probability targets"):
        adopt(tmp_path / "combined", paths, owners)


def test_export_is_idempotent_but_cannot_overwrite_new_results(tmp_path, packages):
    paths, owners = packages
    root = tmp_path / "Scout"
    assert export_stats_package(root, producer="Scout", symbols=owners["Scout"], destination=paths["Scout"]) == paths["Scout"]
    original = paths["Scout"].read_bytes()
    write_review(root, [forecast("DOCU", probability=.3)], version="02")
    with pytest.raises(ValueError, match="different bytes"):
        export_stats_package(root, producer="Scout", symbols=owners["Scout"], destination=paths["Scout"])
    assert paths["Scout"].read_bytes() == original


def test_incomplete_or_overlapping_membership_cannot_be_combined(tmp_path, packages):
    paths, owners = packages
    with pytest.raises(ValueError, match="ownership overlaps"):
        adopt(tmp_path / "combined", paths, {"Atlas": ("AAPL",), "Scout": ("AAPL",)})
    with pytest.raises(ValueError, match="frozen symbol ownership"):
        adopt(tmp_path / "combined", paths, {"Atlas": ("AAPL",), "Scout": ("DOCU", "DBX")})


def test_packages_from_different_sessions_are_not_combined(tmp_path, packages):
    paths, owners = packages
    root = tmp_path / "different"
    write_review(root, [forecast("DOCU", session="2026-09-10")], session="2026-09-10")
    paths["Scout"] = export_stats_package(root, producer="Scout", symbols=owners["Scout"], destination=tmp_path / "different.json")
    with pytest.raises(ValueError, match="same completed session"):
        adopt(tmp_path / "combined", paths, owners)


def baseline_package(tmp_path, actor, symbols, *, session="2026-09-11", reviewed_at="2026-09-12T06:00Z"):
    root = tmp_path / f"baseline-{actor}"
    run = publish_completed_session_review(root, action_date=session, reviewed_at=reviewed_at,
        price_loader=lambda *a, **kw: pytest.fail("No saved forecasts must not load prices"))
    path = export_stats_package(root, producer=actor, symbols=symbols,
        review_run=run, destination=tmp_path / f"baseline-{actor}.json")
    return path, run


def test_verified_no_history_baseline_exports_no_invented_rows(tmp_path):
    path, run = baseline_package(tmp_path, "Scout", ["DOCU", "DBX"])
    payload, frame = read_stats_package(path, expected_sha256=file_checksum(path))
    assert payload["coverage_status"] == NO_SAVED_FORECASTS
    assert payload["probability_target_contract"] == RAW_DIRECTION_TARGET
    assert payload["source_report_sha256"] == file_checksum(run / "report.json")
    assert payload["source_receipt_sha256"] == file_checksum(run / "receipt.json")
    assert payload["rows"] == [] and frame.empty and tuple(frame.columns) == ROW_FIELDS
    assert payload["symbols"] == ["DOCU", "DBX"]


def test_empty_source_without_verified_no_history_status_cannot_export(tmp_path):
    root = tmp_path / "source"
    write_review(root, [])
    with pytest.raises(ValueError, match="verified no-saved-Gameplan baseline"):
        export_stats_package(root, producer="Scout", symbols=["DOCU"], destination=tmp_path / "empty.json")
    assert not (tmp_path / "empty.json").exists()


@pytest.mark.parametrize("missing", [("Scout",), ("Atlas", "Scout")])
def test_baselines_combine_without_scoring_missing_owners(tmp_path, packages, missing):
    paths, owners = packages
    for actor in missing:
        paths[actor], _ = baseline_package(tmp_path, actor, owners[actor])
    run = adopt(tmp_path / "combined", paths, owners)
    verify_manifest(run)
    review = load_gameplan_stats(tmp_path / "combined")
    report = json.loads((run / "report.json").read_text())
    assert report["missing_history_owners"] == list(missing)
    assert report["coverage_status"] == (NO_SAVED_FORECASTS if len(missing) == 2 else "PARTIAL_SAVED_FORECASTS")
    assert review.probability_target_contract == (RAW_DIRECTION_TARGET if len(missing) == 2 else LEGACY_COST_TARGET)
    assert review.metrics().total == (0 if len(missing) == 2 else 2)
    assert review.symbols == (() if len(missing) == 2 else ("AAPL",))
    if len(missing) == 2:
        assert review.metrics().accuracy is None and review.metrics().brier is None
    text = (run / "Gameplan-results.md").read_text(encoding="utf-8")
    for actor in missing:
        assert report["producers"][actor]["symbols"] == list(owners[actor])
        assert report["producers"][actor]["coverage_status"] == NO_SAVED_FORECASTS
        assert f"{actor}: no saved historical forecasts" in text


@pytest.mark.parametrize("coverage", ["SAVED_FORECASTS", "UNKNOWN"])
def test_empty_packages_require_explicit_no_history_coverage(tmp_path, coverage):
    path, _ = baseline_package(tmp_path, "Scout", ["DOCU"])
    payload = json.loads(path.read_text())
    payload["coverage_status"] = coverage
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="explicit coverage status"):
        read_stats_package(path, expected_sha256=file_checksum(path))


@pytest.mark.parametrize("status", ["RESEARCH_NOT_PROMOTED", "RESEARCH_NO_TARGET_HISTORY"])
def test_research_rows_are_retained_but_never_scored(tmp_path, packages, status):
    paths, owners = packages
    scout = json.loads(paths["Scout"].read_text())
    scout["rows"][0]["model_status"] = status
    paths["Scout"].write_text(json.dumps(scout))
    _, frame = read_stats_package(paths["Scout"], expected_sha256=file_checksum(paths["Scout"]))
    assert frame.model_status.eq(status).sum() == 1
    run = adopt(tmp_path / "combined", paths, owners)
    review = load_gameplan_stats(tmp_path / "combined")
    assert review.excluded_forecasts == 1
    assert review.metrics().total == 3 and review.metrics().scored == 1
    assert pd.read_parquet(run / "forecast-results.parquet").model_status.eq(status).sum() == 1


@pytest.mark.parametrize("field,value", [
    ("calibrated_probability", 1.1),
    ("model_brier_score", .9),
    ("model_observed_target", 1),
    ("actual_end_observed_at", "2026-09-12T02:00Z"),
])
def test_rejected_research_evidence_is_validated_before_pointer_change(tmp_path, packages, field, value):
    paths, owners = packages
    root = tmp_path / "combined"
    adopt(root, paths, owners)
    pointer = root / "ml/gameplan-actuals-review-latest/run.json"
    before = pointer.read_bytes()
    scout = json.loads(paths["Scout"].read_text())
    scout["rows"][0].update(model_status="RESEARCH_NOT_PROMOTED", **{field: value})
    paths["Scout"].write_text(json.dumps(scout))
    with pytest.raises(ValueError):
        adopt(root, paths, owners)
    assert pointer.read_bytes() == before


@pytest.mark.parametrize("damage,message", [
    ("missing_symbol", "does not cover"),
    ("duplicate_route", "duplicate forecast"),
    ("invalid_hash", "invalid source provenance"),
    ("wrong_cutoff", "outcome cutoff differs"),
    ("unclosed_session", "has not closed"),
])
def test_additional_package_contract_failures_preserve_both_pointers(tmp_path, packages, damage, message):
    paths, owners = packages
    root = tmp_path / "combined"
    adopt(root, paths, owners)
    pointers = [root / "ml/gameplan-actuals-review-latest/run.json",
                root / "ml/gameplan-actuals-review-by-date/2026-09-11/run.json"]
    before = [path.read_bytes() for path in pointers]
    scout = json.loads(paths["Scout"].read_text())
    if damage == "missing_symbol":
        scout["symbols"].append("DBX")
    elif damage == "duplicate_route":
        scout["rows"][1]["route"] = scout["rows"][0]["route"]
    elif damage == "invalid_hash":
        scout["source_manifest_sha256"] = "g" * 64
    elif damage == "wrong_cutoff":
        scout["outcomes_through"] = "2026-09-12T01:00Z"
    else:
        scout["reviewed_at"] = "2026-09-11T23:59Z"
    paths["Scout"].write_text(json.dumps(scout))
    with pytest.raises(ValueError, match=message):
        adopt(root, paths, owners)
    assert [path.read_bytes() for path in pointers] == before


@pytest.mark.parametrize("damage", ["missing_start", "missing_end", "reversed", "wrong_side", "outside_tolerance"])
def test_evaluated_endpoint_evidence_must_match_its_target(tmp_path, packages, damage):
    paths, owners = packages
    root = tmp_path / "combined"
    adopt(root, paths, owners)
    pointer = root / "ml/gameplan-actuals-review-latest/run.json"
    before = pointer.read_bytes()
    scout = json.loads(paths["Scout"].read_text())
    row = scout["rows"][0]
    start, end = (pd.Timestamp(row[key]) for key in ("target_window_start", "target_window_end"))
    if damage == "missing_start":
        row.pop("actual_start_observed_at")
    elif damage == "missing_end":
        row["actual_end_observed_at"] = None
    elif damage == "reversed":
        row["actual_start_observed_at"], row["actual_end_observed_at"] = end.isoformat(), start.isoformat()
    elif damage == "wrong_side":
        row["actual_start_observed_at"] = (start - pd.Timedelta(minutes=1)).isoformat()
    else:
        row["actual_end_observed_at"] = (end - pd.Timedelta(minutes=5, seconds=1)).isoformat()
    paths["Scout"].write_text(json.dumps(scout))
    with pytest.raises(ValueError):
        adopt(root, paths, owners)
    assert pointer.read_bytes() == before


@pytest.mark.parametrize("role", ["EXECUTION", "OUTLOOK", "OPENING_GAP_RESEARCH"])
def test_endpoint_evidence_accepts_existing_five_minute_boundary(tmp_path, packages, role):
    paths, _ = packages
    scout = json.loads(paths["Scout"].read_text())
    row = scout["rows"][0]
    row["target_role"] = role
    start, end = (pd.Timestamp(row[key]) for key in ("target_window_start", "target_window_end"))
    offset = pd.Timedelta(minutes=-5 if role == "OPENING_GAP_RESEARCH" else 5)
    row["actual_start_observed_at"] = (start + offset).isoformat()
    row["actual_end_observed_at"] = (end - offset).isoformat()
    paths["Scout"].write_text(json.dumps(scout))
    _, frame = read_stats_package(paths["Scout"], expected_sha256=file_checksum(paths["Scout"]))
    assert len(frame) == 2


@pytest.mark.parametrize("status", ["MATURE_AWAITING_DATA", "PENDING_MATURITY"])
def test_unscored_rows_allow_real_partial_endpoint_observations(tmp_path, status):
    root = tmp_path / "source"
    row = forecast("DOCU", status=status, horizon="1w" if status == "PENDING_MATURITY" else "1h")
    write_review(root, [row])
    path = export_stats_package(root, producer="Scout", symbols=["DOCU"], destination=tmp_path / "partial.json")
    payload, frame = read_stats_package(path, expected_sha256=file_checksum(path))
    assert len(frame) == 1 and payload["rows"][0]["actual_end_observed_at"] is None
    outcome = stats_handoff._outcome(payload["rows"][0])
    assert outcome.correct is None and outcome.brier is None


@pytest.mark.parametrize("mutation_phase", ["after_validation", "reissued_after_validation", "after_hash_capture"])
def test_source_mutation_during_export_cannot_create_a_package(tmp_path, packages, monkeypatch, mutation_phase):
    _, owners = packages
    root = tmp_path / "Scout"
    source = load_gameplan_stats(root).run_directory
    receipt_path = source / "receipt.json"
    destination = tmp_path / "changed.json"
    mutated = False

    def mutate_receipt():
        nonlocal mutated
        if not mutated:
            receipt = json.loads(receipt_path.read_text())
            if mutation_phase == "reissued_after_validation":
                report_path = source / "report.json"
                report = json.loads(report_path.read_text())
                report["reviewed_at"] = (pd.Timestamp(report["reviewed_at"]) + pd.Timedelta(minutes=1)).isoformat()
                report_path.write_text(json.dumps(report))
                manifest_path = source / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["output_files"]["report.json"] = {
                    "size": report_path.stat().st_size,
                    "checksum_sha256": file_checksum(report_path),
                }
                manifest_path.write_text(json.dumps(manifest))
                receipt["manifest_sha256"] = file_checksum(manifest_path)
            else:
                receipt["status"] = "INVALIDATED"
            receipt_path.write_text(json.dumps(receipt))
            if mutation_phase == "reissued_after_validation":
                for pointer_path in (root / "ml/gameplan-actuals-review-latest/run.json",
                                     root / "ml/gameplan-actuals-review-by-date/2026-09-11/run.json"):
                    pointer = json.loads(pointer_path.read_text())
                    pointer["current"]["receipt_sha256"] = file_checksum(receipt_path)
                    pointer_path.write_text(json.dumps(pointer))
            mutated = True

    if mutation_phase in {"after_validation", "reissued_after_validation"}:
        original_load = stats_handoff.load_gameplan_stats

        def load_then_mutate(*args, **kwargs):
            review = original_load(*args, **kwargs)
            mutate_receipt()
            return review

        monkeypatch.setattr(stats_handoff, "load_gameplan_stats", load_then_mutate)
    else:
        original_read = stats_handoff.pd.read_parquet
        reads = 0

        def read_then_mutate(*args, **kwargs):
            nonlocal reads
            reads += 1
            # The initial verified reader loads once; export reads the saved
            # rows again only after it has captured all four source hashes.
            if reads == 2:
                mutate_receipt()
            return original_read(*args, **kwargs)

        monkeypatch.setattr(stats_handoff.pd, "read_parquet", read_then_mutate)
    with pytest.raises(ValueError):
        export_stats_package(root, producer="Scout", symbols=owners["Scout"], destination=destination)
    assert mutated and not destination.exists()


def test_reissued_excluded_research_cannot_replace_the_initial_export_source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    research = forecast("DOCU", hour=5)
    research["model_status"] = "RESEARCH_NOT_PROMOTED"
    source = write_review(root, [forecast("DOCU"), research])
    original_load = stats_handoff.load_gameplan_stats
    initial = original_load(root)
    destination = tmp_path / "changed-research.json"
    mutated = False

    def load_then_reissue(*args, **kwargs):
        nonlocal mutated
        review = original_load(*args, **kwargs)
        if not mutated:
            results_path = source / "forecast-results.parquet"
            results = pd.read_parquet(results_path)
            mask = results.model_status.eq("RESEARCH_NOT_PROMOTED")
            results.loc[mask, "calibrated_probability"] = .3
            results.loc[mask, "model_brier_score"] = .3 ** 2
            results.to_parquet(results_path, index=False)
            manifest_path = source / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["output_files"]["forecast-results.parquet"] = {
                "size": results_path.stat().st_size,
                "checksum_sha256": file_checksum(results_path),
            }
            manifest_path.write_text(json.dumps(manifest))
            receipt_path = source / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["manifest_sha256"] = file_checksum(manifest_path)
            receipt_path.write_text(json.dumps(receipt))
            for pointer_path in (root / "ml/gameplan-actuals-review-latest/run.json",
                                 root / "ml/gameplan-actuals-review-by-date/2026-09-11/run.json"):
                pointer = json.loads(pointer_path.read_text())
                pointer["current"]["receipt_sha256"] = file_checksum(receipt_path)
                pointer_path.write_text(json.dumps(pointer))
            mutated = True
        return review

    monkeypatch.setattr(stats_handoff, "load_gameplan_stats", load_then_reissue)
    with pytest.raises(ValueError, match="source changed before export snapshot"):
        export_stats_package(root, producer="Scout", symbols=["DOCU"], destination=destination)
    reissued = original_load(root)
    assert mutated and not destination.exists()
    assert initial.outcomes == reissued.outcomes
    assert initial.excluded_forecasts == reissued.excluded_forecasts == 1
    assert initial.receipt_sha256 != reissued.receipt_sha256


def test_pointer_advance_during_export_keeps_the_original_immutable_run(tmp_path, packages, monkeypatch):
    _, owners = packages
    root = tmp_path / "Scout"
    original_load = stats_handoff.load_gameplan_stats
    selected = original_load(root).run_directory
    receipt_hash = file_checksum(selected / "receipt.json")
    results_hash = file_checksum(selected / "forecast-results.parquet")
    advanced = False

    def load_then_advance(*args, **kwargs):
        nonlocal advanced
        review = original_load(*args, **kwargs)
        if not advanced:
            write_review(root, [forecast("DOCU", probability=.3)], version="02")
            advanced = True
        return review

    monkeypatch.setattr(stats_handoff, "load_gameplan_stats", load_then_advance)
    path = export_stats_package(root, producer="Scout", symbols=owners["Scout"],
                                review_run=selected, destination=tmp_path / "original.json")
    payload, frame = read_stats_package(path, expected_sha256=file_checksum(path))
    assert advanced and original_load(root).run_directory != selected
    assert payload["source_receipt_sha256"] == receipt_hash
    assert payload["source_results_sha256"] == results_hash
    assert len(frame) == 2 and frame.iloc[0].calibrated_probability == .2
