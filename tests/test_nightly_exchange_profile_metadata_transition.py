"""Exact local profile-transition recovery; all operating inputs are temporary fixtures."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools import nightly_exchange as module


DAY = "2026-10-28"
REVIEW = "2026-10-27"
COMPLETION = "20261028T081409Z-463ebe6f4969425fbe40280fc8af7ba0"


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if isinstance(raw, bytes) else module._bytes(raw))
    return path


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def profile():
    return {"actor": "Scout", "machine": "pc-new", "checkout": "C:/fixture",
            "symbols": ["ABCL", "DOCU"], "timezone": "America/Los_Angeles",
            "native_bindings": {"task": "retained-id"},
            "operating_roles": {
                "nightly_gameplan": "Standing local authority",
                "scout_15m_independent_services": {
                    "authority": "Existing local grant", "completion_record": "prior-completion",
                    "manifest_sha256": "1" * 64, "release_root": "C:/fixture/release-before",
                    "source_sha": "2" * 40, "recipe_bytes_preserved": True}}}


@pytest.fixture
def transition(tmp_path):
    before = profile()
    after = deepcopy(before)
    service = after["operating_roles"]["scout_15m_independent_services"]
    service.update(audit_repair={"recipe_bytes_preserved": True}, completion_record=COMPLETION,
                   manifest_sha256="3" * 64, release_root="C:/fixture/release-after", source_sha="4" * 40)
    config = {"actor": "Scout", "state_root": str(tmp_path / "state"),
              "local_profile": str(tmp_path / "current-profile.json"),
              "account_scope_sha256": "5" * 64, "private_exchange_authorized": True}
    before_bytes, after_bytes = module._bytes(before), module._bytes(after)
    frozen = {"config": config, "profile_sha256": module._sha(before_bytes),
              "workflow_config_sha256": "6" * 64, "account_config_sha256": "7" * 64,
              "coordination_active_sha256": "8" * 64, "adapter_sources": {"adapter": "9" * 64}}
    current = deepcopy(frozen)
    current["profile_sha256"] = module._sha(after_bytes)
    session = tmp_path / "state/sessions" / DAY
    binding = write(session / "binding.json", frozen)
    write(session / "failure.json", {"error": "Original immutable binding error", "observed_at": "original-time"})
    directory = session / "profile-metadata-transition"
    record = {"schema_version": module.PROFILE_METADATA_TRANSITION, "action_date": DAY,
              "actor": "Scout", "completion_id": "profile-metadata-recovery-v1",
              "reviewed_completion_record": COMPLETION, "original_binding_sha256": module._sha(binding.read_bytes()),
              "before_profile_sha256": module._sha(before_bytes),
              "after_profile_sha256": module._sha(after_bytes),
              "changed_paths": sorted(module.PROFILE_METADATA_PATHS)}
    write(directory / "profiles" / (record["before_profile_sha256"] + ".json"), before_bytes)
    write(directory / "profiles" / (record["after_profile_sha256"] + ".json"), after_bytes)
    write(Path(config["local_profile"]), after_bytes)
    evidence = {"config": config, "before": before, "after": after, "frozen": frozen,
                "current": current, "directory": directory, "record": record,
                "session": session, "root": tmp_path}
    select(evidence, record)
    return evidence


def select(evidence, record):
    raw = module._bytes(record)
    digest = module._sha(raw)
    write(evidence["directory"] / "records" / (digest + ".json"), raw)
    write(evidence["directory"] / "selection.json", {
        "schema_version": module.PROFILE_METADATA_SELECTION, "record_sha256": digest})
    evidence["record"] = record


def verify(evidence):
    return module._verify_private_exchange_binding(evidence["config"], DAY,
                                                    evidence["frozen"], evidence["current"])


def replace_after(evidence, after):
    raw = module._bytes(after)
    digest = module._sha(raw)
    write(evidence["directory"] / "profiles" / (digest + ".json"), raw)
    write(Path(evidence["config"]["local_profile"]), raw)
    evidence["current"]["profile_sha256"] = digest
    record = deepcopy(evidence["record"])
    record["after_profile_sha256"] = digest
    select(evidence, record)


def test_exact_metadata_transition_preserves_originals_and_repeats_without_writes(transition):
    original = snapshot(transition["session"])
    assert verify(transition) is None
    selected = transition["directory"] / "selected.json"
    assert selected.read_bytes() == (transition["directory"] / "selection.json").read_bytes()
    assert all((transition["session"] / path).read_bytes() == raw for path, raw in original.items())
    after_first = snapshot(transition["root"])
    verify(transition)
    assert snapshot(transition["root"]) == after_first


@pytest.mark.parametrize("name", ["actor", "machine", "checkout", "symbols", "timezone", "native_bindings"])
def test_protected_profile_fields_refuse_transition(transition, name):
    after = deepcopy(transition["after"])
    after[name] = {"unexpected": "change"}
    replace_after(transition, after)
    with pytest.raises(ValueError, match="outside allowed"):
        verify(transition)
    assert not (transition["directory"] / "selected.json").exists()


@pytest.mark.parametrize("field", ["nightly_gameplan", "other_authority"])
def test_nightly_or_new_authority_is_never_metadata(transition, field):
    after = deepcopy(transition["after"])
    after["operating_roles"][field] = "New authority"
    replace_after(transition, after)
    with pytest.raises(ValueError, match="outside allowed"):
        verify(transition)


def test_independent_service_authority_and_recipe_are_protected(transition):
    after = deepcopy(transition["after"])
    after["operating_roles"]["scout_15m_independent_services"]["authority"] = "Changed"
    replace_after(transition, after)
    with pytest.raises(ValueError, match="outside allowed"):
        verify(transition)


def test_boolean_and_integer_protected_values_cannot_compare_equal(transition):
    after = deepcopy(transition["after"])
    after["operating_roles"]["scout_15m_independent_services"]["recipe_bytes_preserved"] = 1
    replace_after(transition, after)
    with pytest.raises(ValueError, match="outside allowed"):
        verify(transition)


@pytest.mark.parametrize("field", ["config", "workflow_config_sha256", "account_config_sha256"])
def test_other_private_binding_changes_fail_before_transition(transition, field):
    transition["current"][field] = {"changed": True} if field == "config" else "0" * 64
    with pytest.raises(ValueError, match="immutable private"):
        verify(transition)
    assert not (transition["directory"] / "selected.json").exists()


def test_unreviewed_profile_drift_retains_original_error(transition):
    (transition["directory"] / "selection.json").unlink()
    with pytest.raises(ValueError, match="immutable private"):
        verify(transition)


@pytest.mark.parametrize("field,value", [
    ("action_date", "2026-10-29"), ("actor", "Atlas"),
    ("original_binding_sha256", "0" * 64), ("before_profile_sha256", "0" * 64),
    ("after_profile_sha256", "0" * 64), ("changed_paths", []),
    ("reviewed_completion_record", "unreviewed"), ("completion_id", "")])
def test_record_identity_and_declared_delta_are_bound(transition, field, value):
    record = deepcopy(transition["record"])
    record[field] = value
    select(transition, record)
    with pytest.raises(ValueError):
        verify(transition)
    assert not (transition["directory"] / "selected.json").exists()


@pytest.mark.parametrize("which", ["before_profile_sha256", "after_profile_sha256"])
def test_tampered_exact_profile_bytes_are_refused(transition, which):
    path = transition["directory"] / "profiles" / (transition["record"][which] + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="profile bytes changed"):
        verify(transition)


def test_tampered_record_bytes_are_refused(transition):
    selector = json.loads((transition["directory"] / "selection.json").read_bytes())
    path = transition["directory"] / "records" / (selector["record_sha256"] + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="record bytes changed"):
        verify(transition)


def test_current_profile_cannot_change_after_selection(transition):
    write(Path(transition["config"]["local_profile"]), transition["before"])
    with pytest.raises(ValueError, match="Current profile changed"):
        verify(transition)


def test_original_binding_cannot_be_rewritten(transition):
    path = transition["session"] / "binding.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="original binding changed"):
        verify(transition)


def test_a_second_valid_selection_cannot_replace_the_first(transition):
    verify(transition)
    selected_before = (transition["directory"] / "selected.json").read_bytes()
    record = deepcopy(transition["record"])
    record["completion_id"] = "different-reviewed-completion"
    select(transition, record)
    with pytest.raises(ValueError, match="immutable profile metadata selection"):
        verify(transition)
    assert (transition["directory"] / "selected.json").read_bytes() == selected_before


def test_unchanged_binding_needs_no_transition_evidence(transition):
    transition["current"]["profile_sha256"] = transition["frozen"]["profile_sha256"]
    (transition["directory"] / "selection.json").unlink()
    verify(transition)
    assert not (transition["directory"] / "selected.json").exists()


def test_adapter_reaches_export_after_verified_transition_without_rebinding(transition, monkeypatch):
    source = write(transition["root"] / "saved-plan.json", {})
    stats = write(transition["root"] / "saved-stats.json", {})
    output = {"package": str(source), "stats_package": str(stats),
              "files": {str(source): module._sha(source.read_bytes()), str(stats): module._sha(stats.read_bytes())}}
    monkeypatch.setattr(module, "_local_state", lambda *args: {"steps": {"local_handoff": {"output": output}}})
    monkeypatch.setattr(module, "_binding", lambda config: deepcopy(transition["current"]))
    monkeypatch.setattr(module, "_owners", lambda *args: None)
    class ReachedExport(Exception):
        pass
    def stop_at_export(*args, **kwargs):
        raise ReachedExport
    monkeypatch.setattr(module, "_publish", stop_at_export)
    original_binding = (transition["session"] / "binding.json").read_bytes()
    original_failure = (transition["session"] / "failure.json").read_bytes()
    with pytest.raises(ReachedExport):
        module._run(transition["config"], {}, DAY, REVIEW, lambda: None, False)
    assert (transition["session"] / "binding.json").read_bytes() == original_binding
    assert (transition["session"] / "failure.json").read_bytes() == original_failure
