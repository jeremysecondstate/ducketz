from dataclasses import asdict
import json

import pytest

from ml.intraday.model_policy import install_selection, load_active_models
from ml.intraday.models import ModelSpec
from ml.intraday.prediction import ContractError


def proposal():
    return dict(status="promoted", holdout_session="2026-10-13", selected_specs=[
        asdict(ModelSpec("qualified-logistic", family="logistic", probability_calibration="sigmoid"))])


def test_policy_only_installs_successful_calibrated_proposal_and_readback(tmp_path):
    assert load_active_models(tmp_path, pc="Scout", symbol="ABCL") is None
    assert install_selection(tmp_path, pc="Scout", symbol="ABCL", report={"status": "retained"},
        feature_names=["a"], installed_at="2026-10-14T08:00Z")["status"] == "retained"
    result = install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z")
    assert result["status"] == "installed"
    specs = load_active_models(tmp_path, pc="Scout", symbol="ABCL", feature_names=["a"])
    assert specs[0].family == "logistic"
    assert len(list((tmp_path / "policies").glob("*.json"))) == 1
    with pytest.raises(ContractError):
        load_active_models(tmp_path, pc="Scout", symbol="ABCL", feature_names=["changed"])
    assert load_active_models(tmp_path, pc="Atlas", symbol="ABCL") is None


def test_changed_policy_fails_closed_and_rejection_preserves_active(tmp_path):
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z")
    path = tmp_path / "active/Scout-ABCL.json"
    original = path.read_bytes()
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report={"status": "retained"},
        feature_names=["a"], installed_at="2026-10-14T08:30Z")
    assert path.read_bytes() == original
    value = json.loads(original)
    value["models"][0]["parameters"] = {"C": 999}
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        load_active_models(tmp_path, pc="Scout", symbol="ABCL")


def test_uncalibrated_winner_cannot_activate(tmp_path):
    value = proposal()
    value["selected_specs"][0]["probability_calibration"] = "none"
    with pytest.raises(ContractError):
        install_selection(tmp_path, pc="Scout", symbol="ABCL", report=value,
            feature_names=["a"], installed_at="2026-10-14T08:00Z")
