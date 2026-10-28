"""Synthetic-only history archive tests; no private operating evidence."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import pytest

from app.ui.gameplan_stats_data import _outcome, prediction_metrics
from app.ui.scout_stats_history import (verify_history, history_15m_sessions, horizon_history_sessions,
    load_history_15m, load_horizon_history)
from app.ui.scout_stats_peer import peer_stats_sessions, load_peer_stats
from ml.gameplan_probability_target import LEGACY_COST_TARGET, probability_target_metadata
from tests.gameplan_stats_fixture import forecast
from test_scout_stats_peer import export_fixture


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    path.write_bytes(raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def reseal_history(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    for name in manifest["files"]:
        raw = (directory / name).read_bytes()
        manifest["files"][name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    pin = _write(directory / "manifest.json", manifest)["sha256"]
    _write(directory / "receipt.json", dict(completion_id=manifest["completion_id"], manifest_sha256=pin,
                                           verified_at="2026-10-28T03:00:00Z"))
    return pin


def history_fixture(root, *, session="2026-10-13", baseline="2026-10-01",
                    completion_id="20261028T013851Z-synthetic-scout-history",
                    contract=LEGACY_COST_TARGET, revisions=2):
    """Make baseline + scored horizon history and an independent 15-minute day."""
    directory = root / "intraday/scout/display-history/v1" / completion_id
    directory.mkdir(parents=True)
    generated_at = "2026-10-28T01:40:46Z"
    common = dict(producer="Scout", recipient="Atlas", machine="pc-new", completion_id=completion_id,
                  generated_at=generated_at)
    source15 = {"app/ui/scout_stats_data.py": "a" * 64, "app/ui/scout_stats_widgets.py": "b" * 64}
    source_horizon = {"app/ui/gameplan_stats_data.py": "c" * 64, "ml/gameplan_stats_handoff.py": "d" * 64}
    manifest = dict(common, schema_version="scout-stats-history-delivery-v1", root_index="index.json", files={},
                    scout_15m_dates=[session], gameplan_horizon_dates=sorted([baseline, session]),
                    gameplan_horizon_package_count=revisions + 1, scout_15m_source_files=source15,
                    horizon_source_files=source_horizon)
    files = manifest["files"]
    source = export_fixture(root / "_synthetic_15m" / completion_id, session=session)
    payload = json.loads((source / "snapshot.json").read_text())
    payload.update(generated_at=generated_at, completion_id=completion_id + "-" + session.replace("-", ""))
    path = f"sessions/{session}/snapshot.json"
    files[path] = _write(directory / path, payload)
    report = f"sessions/{session}/report.html"
    (directory / report).write_text("<p>Synthetic historical session</p>")
    files[report] = {}
    index15 = dict(path=path, report=report, session=session, sha256=files[path]["sha256"],
        prediction_summary=payload["prediction_summary"], execution_summary=payload["execution_summary"],
        **{key: payload["snapshot"][key] for key in ("status", "prediction_status", "execution_status", "warnings")})
    horizon = dict(schema_version="scout-gameplan-horizon-history-index-v1", producer="Scout", recipient="Atlas",
        source_files=source_horizon, source_session_before="2026-10-27", combined_publications_excluded=0,
        rejected_source_count=0, limitations=["Synthetic fixture"], scope="Historical Scout-only Stats",
        session_count=2, package_count=revisions + 1, sessions=[])
    for day in sorted([baseline, session]):
        saved = []
        for revision in range(1 if day == baseline else revisions):
            receipt_hash = hashlib.sha256(f"{day}-{revision}".encode()).hexdigest()
            rows = []
            if day != baseline:
                row = forecast("TEST", session=day, probability=.2 + revision * .1)
                row.update(probability_target_contract=contract, assumed_round_trip_cost=.001,
                           gameplan_variant=probability_target_metadata(contract)["gameplan_variant"])
                row = {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}
                rows = [row]
            package = dict(schema_version="cross-pc-gameplan-stats-v1", producer="Scout", action_date=day,
                symbols=["TEST"], reviewed_at=f"{day}T22:{revision:02d}:00-07:00",
                outcomes_through=f"{day}T17:00:00-07:00", probability_target_contract=contract,
                source_receipt_sha256=receipt_hash, source_manifest_sha256="a" * 64,
                source_results_sha256="b" * 64, source_report_sha256="c" * 64,
                coverage_status="SAVED_FORECASTS" if rows else "NO_SAVED_INDEPENDENT_GAMEPLAN", rows=rows)
            filename = f"packages/{day}/{receipt_hash[:16]}.json"
            files["gameplan-horizons/" + filename] = _write(directory / "gameplan-horizons" / filename, package)
            metrics = prediction_metrics(tuple(_outcome(row) for row in rows if row["model_status"] == "PROMOTED"))
            metric_values = dict(asdict(metrics), accuracy=metrics.accuracy)
            saved.append(dict(file=filename, session=day, **files["gameplan-horizons/" + filename], rows=len(rows),
                metrics=metric_values, source_kind="Synthetic saved Scout-only projection",
                **{key: package[key] for key in ("coverage_status", "outcomes_through", "reviewed_at",
                    "source_receipt_sha256", "probability_target_contract")}))
        chosen = saved[-1]
        horizon["sessions"].append(dict(session=day, revisions=saved, selected_file=chosen["file"],
            selected_sha256=chosen["sha256"], **{key: chosen[key] for key in
                ("coverage_status", "metrics", "outcomes_through", "reviewed_at")}))
    index = dict(common, schema_version="scout-stats-history-export-v1", timezone="America/Los_Angeles",
        gameplan_horizons_index="gameplan-horizons/index.json", limitations=["Synthetic fixture"],
        scout_15m_calendar_gaps=[], scout_15m_sessions=[index15])
    files["index.json"] = _write(directory / "index.json", index)
    files["gameplan-horizons/index.json"] = _write(directory / "gameplan-horizons/index.json", horizon)
    (directory / "report.html").write_text("<p>Synthetic history export</p>")
    files["report.html"] = {}
    _write(directory / "manifest.json", manifest)
    reseal_history(directory)
    return directory


def _pin(directory):
    return json.loads((directory / "receipt.json").read_text())["manifest_sha256"]


def test_history_full_verification_and_discovery(tmp_path):
    directory = history_fixture(tmp_path)
    bundle = verify_history(directory, _pin(directory))
    assert set(bundle["scout_15m"]) == {"2026-10-13"}
    assert set(bundle["horizons"]) == {"2026-10-01", "2026-10-13"}
    assert history_15m_sessions(tmp_path) == ("2026-10-13",)
    assert horizon_history_sessions(tmp_path) == ("2026-10-13", "2026-10-01")
    selected = load_horizon_history(tmp_path, "2026-10-13")
    assert selected.revision_count == 2 and selected.review.metrics().total == 1
    assert selected.review.metrics().brier == pytest.approx(.09)
    assert selected.review.probability_target_contract == LEGACY_COST_TARGET
    assert load_history_15m(tmp_path, "2026-10-13")["session"] == "2026-10-13"
    baseline = load_horizon_history(tmp_path, "2026-10-01")
    assert baseline.coverage_status == "NO_SAVED_INDEPENDENT_GAMEPLAN"
    assert baseline.review.metrics().total == 0 and baseline.review.metrics().accuracy is None


def test_peer_discovery_retains_original_october27_export(tmp_path):
    history_fixture(tmp_path)
    export_fixture(tmp_path)
    assert peer_stats_sessions(tmp_path) == ("2026-10-27", "2026-10-13")
    assert load_peer_stats(tmp_path, "2026-10-27")["session"] == "2026-10-27"
    assert load_peer_stats(tmp_path, "2026-10-13")["session"] == "2026-10-13"


def test_corrupt_history_keeps_independent_direct_export_with_visible_warning(tmp_path):
    history = history_fixture(tmp_path)
    direct = export_fixture(tmp_path)
    original = (direct / "snapshot.json").read_bytes()
    with (history / "report.html").open("ab") as handle:
        handle.write(b"corruption")
    assert peer_stats_sessions(tmp_path) == ("2026-10-27",)
    payload = load_peer_stats(tmp_path, "2026-10-27")
    assert any("Historical Scout import could not be verified" in text for text in payload["snapshot"]["warnings"])
    assert payload["prediction_summary"]["scored"] == 1
    assert (direct / "snapshot.json").read_bytes() == original
    with pytest.raises(ValueError):
        load_peer_stats(tmp_path, "2026-10-13")


@pytest.mark.parametrize("filename", ["index.json", "gameplan-horizons/index.json", "sessions/2026-10-13/snapshot.json", "report.html"])
def test_history_all_files_remain_hash_pinned(tmp_path, filename):
    directory = history_fixture(tmp_path)
    with (directory / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="hash differs"):
        verify_history(directory, _pin(directory))


@pytest.mark.parametrize("damage", ["selected_missing", "index_metrics", "ownership", "unknown_row", "metric", "producer"])
def test_history_semantic_corruption_rejected_after_resealing(tmp_path, damage):
    directory = history_fixture(tmp_path)
    hpath = directory / "gameplan-horizons/index.json"
    horizon = json.loads(hpath.read_text())
    session = horizon["sessions"][-1]
    if damage == "selected_missing":
        session["selected_file"] = "packages/2026-10-13/0000000000000000.json"
    elif damage == "index_metrics":
        session["metrics"]["total"] = 999
    else:
        revision = session["revisions"][-1]
        ppath = directory / "gameplan-horizons" / revision["file"]
        package = json.loads(ppath.read_text())
        if damage == "ownership":
            package["symbols"] = ["OTHER"]
        elif damage == "unknown_row":
            package["rows"][0]["private_model"] = "forbidden synthetic field"
        elif damage == "metric":
            package["rows"][0]["model_brier_score"] = .99
        elif damage == "producer":
            package["producer"] = "Atlas"
        item = _write(ppath, package)
        revision.update(item)
        session["selected_sha256"] = item["sha256"]
    _write(hpath, horizon)
    reseal_history(directory)
    with pytest.raises(ValueError):
        verify_history(directory, _pin(directory))


def test_unknown_history_paths_rejected(tmp_path):
    directory = history_fixture(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["files"]["../outside.json"] = {"bytes": 0, "sha256": "a" * 64}
    pin = _write(directory / "manifest.json", manifest)["sha256"]
    with pytest.raises(ValueError, match="path"):
        verify_history(directory, pin)


def test_explicit_preferred_revision_can_be_older(tmp_path):
    directory = history_fixture(tmp_path)
    hpath = directory / "gameplan-horizons/index.json"
    horizon = json.loads(hpath.read_text())
    session = horizon["sessions"][-1]
    preferred = session["revisions"][0]
    session.update(selected_file=preferred["file"], selected_sha256=preferred["sha256"])
    session.update({key: preferred[key] for key in ("coverage_status", "metrics", "outcomes_through", "reviewed_at")})
    _write(hpath, horizon)
    reseal_history(directory)
    verified = verify_history(directory, _pin(directory))
    snapshot = verified["horizons"][session["session"]]
    assert snapshot.package_sha256 == preferred["sha256"]
    assert snapshot.review.metrics().brier == pytest.approx(.04)
    assert snapshot.revision_count == 2 and snapshot.review.metrics().total == 1


def test_raw_direction_contract_remains_original(tmp_path):
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    history_fixture(tmp_path, contract=RAW_DIRECTION_TARGET)
    snapshot = load_horizon_history(tmp_path, "2026-10-13")
    assert snapshot.review.probability_target_contract == RAW_DIRECTION_TARGET
    assert all(row.probability_target_contract == RAW_DIRECTION_TARGET for row in snapshot.review.outcomes)


def test_missing_history_is_not_zero_metrics(tmp_path):
    assert history_15m_sessions(tmp_path) == horizon_history_sessions(tmp_path) == ()
    assert load_horizon_history(tmp_path, "2026-10-01") is None
    assert load_history_15m(tmp_path, "2026-10-01") is None


def test_default_none_root_uses_existing_datastore_binding(tmp_path, monkeypatch):
    history_fixture(tmp_path)
    monkeypatch.setenv("DUCKETS_DATASTORE_DIR", str(tmp_path))
    assert history_15m_sessions() == ("2026-10-13",)
    assert horizon_history_sessions(None) == ("2026-10-13", "2026-10-01")
    assert load_history_15m(None, "2026-10-13")["session"] == "2026-10-13"
    assert load_horizon_history(None, "2026-10-01").coverage_status == "NO_SAVED_INDEPENDENT_GAMEPLAN"
    assert horizon_history_sessions(tmp_path / "other") == ()
