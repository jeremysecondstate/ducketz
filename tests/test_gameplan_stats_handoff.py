import json

import pytest

from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import publish_completed_session_review
from ml.gameplan_stats_handoff import export_stats_package, adopt_combined_stats, read_stats_package, NO_SAVED_FORECASTS, ROW_FIELDS
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET, LEGACY_COST_TARGET
from tests.gameplan_stats_fixture import forecast, write_review


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
