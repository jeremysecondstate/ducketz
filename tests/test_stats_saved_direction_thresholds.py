"""Saved Stats policy metadata survives handoff without relabeling history."""
import json

import pandas as pd
import pytest

from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
from ml.gameplan_stats_handoff import adopt_combined_stats, export_stats_package, read_stats_package
from tests.gameplan_stats_fixture import forecast, write_review


POLICY_FIELDS = ("direction_policy_version", "direction_up_threshold", "direction_down_threshold")


@pytest.fixture(autouse=True)
def isolated_nightly_context(monkeypatch):
    monkeypatch.delenv("DUCKETZ_NIGHTLY_ACTION_DATE", raising=False)


def packages(tmp_path, metadata):
    owners = {"Atlas": ("AAPL",), "Scout": ("DOCU",)}
    paths = {}
    for actor, symbols in owners.items():
        # This historical bullish call falls inside today's HOLD band. It must
        # retain its saved label, probability and observed target.
        row = forecast(symbols[0], direction="BULLISH", probability=.50005, change=.01)
        row.update(probability_target_contract=RAW_DIRECTION_TARGET, gameplan_variant="YG", **metadata)
        root = tmp_path / actor
        write_review(root, [row])
        paths[actor] = export_stats_package(root, producer=actor, symbols=symbols,
            destination=tmp_path / f"{actor}.json")
    return paths, owners


def adopt(root, paths, owners):
    return adopt_combined_stats(root, packages=paths, expected_symbols=owners,
        expected_sha256={actor: file_checksum(path) for actor, path in paths.items()},
        reviewed_at="2026-09-12T07:00Z")


@pytest.mark.parametrize("metadata", [
    {},
    dict.fromkeys(POLICY_FIELDS),
    {"direction_policy_version": "stock-direction-50-v2", "direction_up_threshold": .5,
     "direction_down_threshold": .5},
    {"direction_policy_version": "stock-direction-50-01-v3", "direction_up_threshold": .5001,
     "direction_down_threshold": .4999},
    {"direction_policy_version": "stock-direction-5001-4999-v3", "direction_up_threshold": .5001,
     "direction_down_threshold": .4999},
    {"direction_policy_version": "saved-experiment", "direction_up_threshold": .6,
     "direction_down_threshold": .4},
    {"direction_up_threshold": .5},
    {"direction_down_threshold": .5, "direction_up_threshold": None},
    {"direction_up_threshold": 1, "direction_down_threshold": 0},
])
def test_optional_saved_policy_roundtrips_without_relabeling(tmp_path, metadata):
    paths, owners = packages(tmp_path, metadata)
    frozen = {actor: path.read_bytes() for actor, path in paths.items()}
    for path in paths.values():
        payload, frame = read_stats_package(path, expected_sha256=file_checksum(path))
        row = payload["rows"][0]
        assert row["direction"] == "BULLISH" and row["calibrated_probability"] == .50005
        assert row["model_observed_target"] == 1
        assert row["probability_target_contract"] == RAW_DIRECTION_TARGET
        for key in POLICY_FIELDS:
            if key in metadata:
                assert row[key] == metadata[key]
            else:
                assert key not in row
            if metadata.get(key) is None:
                assert pd.isna(frame.iloc[0][key])
            else:
                assert frame.iloc[0][key] == metadata[key]
    root = tmp_path / "combined"
    run = adopt(root, paths, owners)
    saved = pd.read_parquet(run / "forecast-results.parquet")
    assert saved.direction.eq("BULLISH").all()
    assert saved.calibrated_probability.eq(.50005).all()
    assert saved.model_observed_target.eq(1).all()
    for key in POLICY_FIELDS:
        if metadata.get(key) is None:
            assert saved[key].isna().all()
        else:
            assert saved[key].eq(metadata[key]).all()
    assert load_gameplan_stats(root).metrics().correct == 2
    reexported = export_stats_package(root, producer="Atlas", symbols=("AAPL", "DOCU"),
        destination=tmp_path / "reexported.json")
    payload, _ = read_stats_package(reexported, expected_sha256=file_checksum(reexported))
    for row in payload["rows"]:
        assert row["direction"] == "BULLISH" and row["calibrated_probability"] == .50005
        for key in POLICY_FIELDS:
            assert row.get(key) == metadata.get(key)
    assert all(path.read_bytes() == frozen[actor] for actor, path in paths.items())


@pytest.mark.parametrize("field,value", [
    ("direction_up_threshold", True),
    ("direction_down_threshold", False),
    ("direction_up_threshold", ".5"),
    ("direction_up_threshold", []),
    ("direction_down_threshold", {}),
    ("direction_up_threshold", float("nan")),
    ("direction_up_threshold", float("inf")),
    ("direction_down_threshold", float("-inf")),
    ("direction_up_threshold", 1.01),
    ("direction_down_threshold", -.01),
    ("direction_up_threshold", .4),
    ("account_number", "PRIVATE"),
])
def test_bad_metadata_and_unknown_fields_preserve_existing_pointers(tmp_path, field, value):
    paths, owners = packages(tmp_path, {"direction_policy_version": "stock-direction-50-v2",
        "direction_up_threshold": .5, "direction_down_threshold": .5})
    root = tmp_path / "combined"
    adopt(root, paths, owners)
    pointers = [root / "ml/gameplan-actuals-review-latest/run.json",
                root / "ml/gameplan-actuals-review-by-date/2026-09-11/run.json"]
    before = [path.read_bytes() for path in pointers]
    payload = json.loads(paths["Scout"].read_text())
    payload["rows"][0][field] = value
    paths["Scout"].write_text(json.dumps(payload), encoding="utf-8")
    message = "unsupported prediction fields" if field == "account_number" else "saved direction thresholds"
    with pytest.raises(ValueError, match=message):
        read_stats_package(paths["Scout"], expected_sha256=file_checksum(paths["Scout"]))
    with pytest.raises(ValueError, match=message):
        adopt(root, paths, owners)
    assert [path.read_bytes() for path in pointers] == before
