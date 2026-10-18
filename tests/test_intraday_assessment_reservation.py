"""Reserved outcomes cannot silently enter continuing saved-data fitting."""
import json
from dataclasses import replace

import numpy as np
import pytest

from ml.intraday.assessment_reservation import (
    SCHEMA, assert_training_unreserved, load_reservation, protect_dataset,
)
from ml.intraday.prediction import ContractError, digest
from tests.test_intraday_live_model_recipe import selected, dataset_for


def reserve(root, boundary):
    value = dict(schema=SCHEMA, pc="Scout", symbols=["ABCL", "DBX"], experiment_id="test-frozen",
                 protected_from=boundary, policy="exclude_from_automatic_development_until_explicit_new_experiment")
    value["sha256"] = digest(value)
    path = root / "intraday/scout/assessment-reservation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


def test_targets_are_purged_before_fit_without_removing_future_inference(tmp_path):
    _, _, _, _, runtime = selected(tmp_path)
    before = dataset_for(runtime)
    boundary = before.target_close_at[180].isoformat()
    reservation = reserve(tmp_path, boundary)
    after = dataset_for(runtime)
    assert len(after.y) == 181 < len(before.y)
    np.testing.assert_array_equal(after.X, before.X[:181])
    np.testing.assert_array_equal(after.inference_X, before.inference_X)
    assert after.manifest["assessment_reservation"]["sha256"] == reservation["sha256"]
    assert after.manifest["label_available_at_max"] == max(after.label_available_at).isoformat()
    assert_training_unreserved(after.target_close_at, reservation)
    with pytest.raises(ContractError, match="Reserved prospective"):
        assert_training_unreserved(before.target_close_at, reservation)


def test_private_scope_absence_corruption_and_no_automatic_expiry(tmp_path):
    assert load_reservation(tmp_path, pc="Scout") is None
    value = reserve(tmp_path, "2026-05-15T00:00:00Z")
    assert load_reservation(tmp_path, pc="Scout", symbol="ABCL") == value
    assert load_reservation(tmp_path, pc="Atlas") is None
    assert load_reservation(tmp_path, pc="Scout", symbol="OTHER") is None
    with pytest.raises(ContractError):
        assert_training_unreserved(["2030-01-01T00:00Z"], value)
    path = tmp_path / "intraday/scout/assessment-reservation.json"
    path.write_text(path.read_text().replace("2026-05-15", "2030-05-15"))
    with pytest.raises(ContractError, match="reservation"):
        load_reservation(tmp_path, pc="Scout")


def test_empty_reservation_population_stays_empty(tmp_path):
    _, _, _, _, runtime = selected(tmp_path)
    before = dataset_for(runtime)
    value = reserve(tmp_path, "2000-01-01T00:00Z")
    after = protect_dataset(before, value)
    assert len(after.y) == 0
    np.testing.assert_array_equal(after.inference_X, before.inference_X)


def test_supervision_cannot_inspect_or_select_reserved_session(tmp_path, monkeypatch):
    from ml.intraday import supervision
    from tests.test_intraday_supervision import helpers
    reserve(tmp_path, "2026-10-13T11:00:00Z")
    kwargs = helpers(monkeypatch)
    kwargs["builder"] = lambda *a, **kw: pytest.fail("built reserved model population")
    monkeypatch.setattr(supervision, "review_session", lambda *a, **kw: pytest.fail("inspected reserved performance"))
    result = supervision.supervise(datastore=tmp_path,
        evaluator=lambda *a, **kw: pytest.fail("selected on reserved observations"), **kwargs)
    assert all(item["status"] == "reserved_for_prospective_assessment" for item in result["symbols"].values())
    assert not list((tmp_path / "intraday/supervision/decisions").glob("**/*.json"))
