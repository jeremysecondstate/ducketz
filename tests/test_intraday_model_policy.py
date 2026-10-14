from dataclasses import asdict
import json

import pytest

from ml.intraday.model_policy import install_selection, load_active_models, load_active_policy
from ml.intraday.models import ModelSpec
from ml.intraday.prediction import ContractError, digest


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


@pytest.mark.parametrize("scope", [
    {"synthetic_policy": "exclude", "lookback_days": None},
    {"synthetic_policy": "include", "lookback_days": 7},
])
def test_policy_binds_exact_reviewed_dataset_scope_and_only_scope_fields(tmp_path, scope):
    report = proposal()
    result = install_selection(tmp_path, pc="Scout", symbol="ABCL", report=report,
        feature_names=["a"], installed_at="2026-10-14T08:00Z",
        dataset_manifest={**scope, "training_rows": 47, "private_source_path": "not-for-policy"})
    policy = load_active_policy(tmp_path, pc="Scout", symbol="ABCL", feature_names=["a"])
    assert policy["models"] == load_active_models(tmp_path, pc="Scout", symbol="ABCL")
    assert isinstance(policy["models"], tuple)
    assert policy["dataset_scope"] == scope
    assert policy["feature_names"] == ["a"]
    assert policy["sha256"] == result["policy_sha256"]
    raw = (tmp_path / "active/Scout-ABCL.json").read_bytes()
    assert b"private_source_path" not in raw and b"training_rows" not in raw
    archive = tmp_path / "policies" / f"{policy['sha256']}.json"
    assert archive.read_bytes() == raw
    saved = json.loads(raw)
    checksum = saved.pop("sha256")
    assert checksum == digest(saved) and saved["report_sha256"] == digest(report)


def test_legacy_policy_scope_is_derived_after_integrity_without_rewriting_bytes(tmp_path):
    assert load_active_policy(tmp_path, pc="Scout", symbol="ABCL") is None
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z")
    path = tmp_path / "active/Scout-ABCL.json"
    value = json.loads(path.read_bytes())
    del value["dataset_scope"]
    value.pop("sha256")
    checksum = digest(value)
    value["sha256"] = checksum
    original = json.dumps(value, indent=4).encode("utf-8") + b"\n\n"
    path.write_bytes(original)
    archive = tmp_path / "policies" / f"{checksum}.json"
    archive.write_bytes(original)
    policy = load_active_policy(tmp_path, pc="Scout", symbol="ABCL")
    assert policy["dataset_scope"] == {"synthetic_policy": "exclude", "lookback_days": None}
    assert policy["sha256"] == checksum
    assert policy["models"][0].name == "qualified-logistic"
    assert path.read_bytes() == archive.read_bytes() == original
    # Adding scope without rehashing does not extend a legacy policy.
    value["dataset_scope"] = {"synthetic_policy": "include", "lookback_days": 1}
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        load_active_policy(tmp_path, pc="Scout", symbol="ABCL")


@pytest.mark.parametrize("manifest", [None, {}, {"completed_session_id": "2026-10-13"}])
def test_historical_decision_without_scope_installs_known_legacy_population(tmp_path, manifest):
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z", dataset_manifest=manifest)
    policy = load_active_policy(tmp_path, pc="Scout", symbol="ABCL")
    assert policy["dataset_scope"] == {"synthetic_policy": "exclude", "lookback_days": None}


INVALID_SCOPES = [
    {"synthetic_policy": "exclude"},
    {"lookback_days": None},
    {"synthetic_policy": None, "lookback_days": None},
    {"synthetic_policy": [], "lookback_days": None},
    {"synthetic_policy": "default", "lookback_days": None},
    {"synthetic_policy": "exclude", "lookback_days": 0},
    {"synthetic_policy": "exclude", "lookback_days": -1},
    {"synthetic_policy": "exclude", "lookback_days": True},
    {"synthetic_policy": "exclude", "lookback_days": 1.5},
    {"synthetic_policy": "exclude", "lookback_days": "7"},
]


@pytest.mark.parametrize("manifest", INVALID_SCOPES + [[], "exclude"])
def test_invalid_or_partial_reviewed_scope_preserves_incumbent_and_archive(tmp_path, manifest):
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    with pytest.raises(ContractError):
        install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
            feature_names=["a"], installed_at="2026-10-14T08:30Z", dataset_manifest=manifest)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.json")} == before


@pytest.mark.parametrize("scope", INVALID_SCOPES + [None, {}, []])
def test_present_invalid_policy_scope_cannot_masquerade_as_legacy(tmp_path, scope):
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z")
    path = tmp_path / "active/Scout-ABCL.json"
    value = json.loads(path.read_bytes())
    value["dataset_scope"] = scope
    value.pop("sha256")
    value["sha256"] = digest(value)
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    with pytest.raises(ContractError):
        load_active_policy(tmp_path, pc="Scout", symbol="ABCL")
    assert path.read_bytes() == original


def test_changed_scoped_policy_fails_integrity_even_when_scope_remains_valid(tmp_path):
    install_selection(tmp_path, pc="Scout", symbol="ABCL", report=proposal(),
        feature_names=["a"], installed_at="2026-10-14T08:00Z",
        dataset_manifest={"synthetic_policy": "exclude", "lookback_days": 7})
    path = tmp_path / "active/Scout-ABCL.json"
    value = json.loads(path.read_bytes())
    value["dataset_scope"]["synthetic_policy"] = "include"
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        load_active_policy(tmp_path, pc="Scout", symbol="ABCL")
