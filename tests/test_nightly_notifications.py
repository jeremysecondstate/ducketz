"""Offline notification timing, exact ownership and native-confirmation regressions."""
from pathlib import Path
import json

import pytest
from tools import nightly_notifications as notices

NOW = "2026-10-12T03:40:00-07:00"


@pytest.fixture
def config(tmp_path):
    return {"actor": "Scout", "state_root": str(tmp_path)}


def ready(now=NOW, **changes):
    return {"action_date": "2026-10-12", "observed_at": now, "confirmed": False,
            "evidence": "fresh-local-and-exact-peer-readiness", **changes}


def get_claim(config, **changes):
    return notices.claim(config, ready(), kind="readiness-risk", owner="display",
                         thread_id="native-run-one", now=NOW, **changes)


def proof(tmp_path, claim, visible=True, **changes):
    value = {"source": "native_thread_readback", "event_id": claim["event"]["event_id"],
             "owner": claim["claim"]["owner"], "thread_id": claim["claim"]["thread_id"],
             "run_status": "completed", "notice_visible": visible, "observed_at": NOW, "completed_at": NOW,
             "readback_reference": "saved-native-read_thread-result", **changes}
    p = tmp_path / "proof.json"
    p.write_text(json.dumps(value))
    return p


@pytest.mark.parametrize("now,kinds", [
    ("2026-10-12T02:58:00-07:00", []),
    ("2026-10-12T03:00:00-07:00", ["readiness-risk"]),
    ("2026-10-12T03:34:00-07:00", ["readiness-risk"]),
    (NOW, ["readiness-risk", "missed-confirmation"]),
    ("2026-10-10T03:40:00-07:00", []),
    ("2026-10-11T03:40:00-07:00", []),
])
def test_calendar_due_thresholds_and_early_native_jitter(config, now, kinds):
    result = notices.inspect(config, ready(now), now=now)
    assert result["action_date"] == "2026-10-12"
    assert [v["event"]["kind"] for v in result["events"]] == kinds
    assert not list(Path(config["state_root"]).rglob("event.json"))


def test_holiday_retains_calendar_successor_without_false_alarm(config):
    now = "2026-12-25T03:40:00-08:00"
    result = notices.inspect(config, ready(now, action_date="2026-12-28"), now=now)
    assert result["action_date"] == "2026-12-28"
    assert result["events"] == []


def test_verified_confirmation_never_creates_warning(config):
    assert notices.inspect(config, ready(confirmed=True), now=NOW)["events"] == []
    assert notices.claim(config, ready(confirmed=True), kind="readiness-risk", owner="a", thread_id="t", now=NOW)["status"] == "NOT_DUE"


@pytest.mark.parametrize("changes", [
    {"observed_at": "2026-10-12T03:00:00-07:00"},
    {"observed_at": "2026-10-12T04:00:00-07:00"},
    {"observed_at": "2026-10-12T03:40:00"},
    {"action_date": "2026-10-09"}, {"confirmed": "true"}, {"evidence": ""},
])
def test_stale_wrong_date_and_unverified_readiness_refused(config, changes):
    with pytest.raises(ValueError):
        notices.inspect(config, ready(**changes), now=NOW)


def test_duplicate_native_wake_reuses_exact_claim(config):
    first = get_claim(config)
    assert get_claim(config) == first
    assert first["status"] == "PENDING_NATIVE_CONFIRMATION"
    assert first["claimed"] is True
    assert not list(Path(config["state_root"]).rglob("outcomes/*.json"))


def test_other_owner_or_new_native_thread_cannot_take_over(config):
    original = get_claim(config)
    for owner, thread in [("synthesis", "new-run"), ("display", "new-run")]:
        blocked = notices.claim(config, ready(), kind="readiness-risk", owner=owner, thread_id=thread, now=NOW)
        assert blocked["claimed"] is False
        assert blocked["claim"] == original["claim"]


def test_no_time_expiry_takeover(config):
    original = get_claim(config)
    later = "2026-10-12T15:40:00-07:00"
    blocked = notices.claim(config, ready(later), kind="readiness-risk", owner="other", thread_id="other", now=later)
    assert blocked["claimed"] is False
    assert blocked["claim"] == original["claim"]


def test_actual_native_confirmation_is_durable_idempotent_and_suppresses_duplicate(config, tmp_path):
    first = get_claim(config)
    p = proof(tmp_path, first)
    kwargs = dict(event_id=first["event"]["event_id"], token=first["claim"]["token"], proof_path=p, now=NOW)
    done = notices.confirm(config, **kwargs)
    assert done["status"] == "DELIVERED"
    assert notices.confirm(config, **kwargs) == done
    assert get_claim(config)["status"] == "DELIVERED"


def test_confirmed_absent_notice_can_be_reclaimed_with_same_event_identity(config, tmp_path):
    first = get_claim(config)
    p = proof(tmp_path, first, visible=False, run_status="failed")
    notices.confirm(config, event_id=first["event"]["event_id"], token=first["claim"]["token"], proof_path=p, now=NOW)
    second = notices.claim(config, ready(), kind="readiness-risk", owner="synthesis", thread_id="next-native-run", now=NOW)
    assert second["event"] == first["event"]
    assert second["claim"]["token"] != first["claim"]["token"]
    assert len(list(Path(config["state_root"]).rglob("claims/*.json"))) == 2


@pytest.mark.parametrize("changes", [
    {"thread_id": "other"}, {"owner": "other"}, {"event_id": "a" * 64},
    {"run_status": "running"}, {"notice_visible": "yes"}, {"source": "assumed"},
    {"readback_reference": ""}, {"observed_at": "2026-10-12T03:00:00-07:00"},
])
def test_unknown_or_wrong_native_outcome_never_claims_delivery(config, tmp_path, changes):
    first = get_claim(config)
    p = proof(tmp_path, first, **changes)
    with pytest.raises(ValueError):
        notices.confirm(config, event_id=first["event"]["event_id"], token=first["claim"]["token"], proof_path=p, now=NOW)
    assert get_claim(config)["status"] == "PENDING_NATIVE_CONFIRMATION"


def test_interruption_after_event_save_before_claim_is_resumable(config, monkeypatch):
    original = notices._immutable
    def interrupted(path, value):
        if path.parent.name == "claims":
            raise OSError("fixture crash before claim publication")
        return original(path, value)
    monkeypatch.setattr(notices, "_immutable", interrupted)
    with pytest.raises(OSError):
        get_claim(config)
    monkeypatch.setattr(notices, "_immutable", original)
    assert get_claim(config)["status"] == "PENDING_NATIVE_CONFIRMATION"


def test_immutable_outcome_cannot_be_rewritten(config, tmp_path):
    first = get_claim(config)
    kwargs = dict(event_id=first["event"]["event_id"], token=first["claim"]["token"], now=NOW)
    notices.confirm(config, proof_path=proof(tmp_path, first), **kwargs)
    with pytest.raises(ValueError, match="Immutable"):
        notices.confirm(config, proof_path=proof(tmp_path, first, visible=False), **kwargs)


def test_cli_requires_readiness_or_explicit_proof(config, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(SystemExit):
        notices.main(["--config", str(path), "--inspect"])


def test_missing_observation_time_is_not_assumed_current(config):
    value = ready()
    value.pop("observed_at")
    with pytest.raises(ValueError, match="time is required"):
        notices.inspect(config, value, now=NOW)


def test_pending_claim_is_reconciled_but_not_reemitted_after_readiness_recovers(config):
    get_claim(config)
    value = notices.claim(config, ready(confirmed=True), kind="readiness-risk", owner="display", thread_id="native-run-one", now=NOW)
    assert value["status"] == "PENDING_NATIVE_CONFIRMATION"
    assert value["eligible"] is False


def test_held_ledger_lock_prevents_competing_native_owner(config):
    from filelock import FileLock, Timeout
    root = notices._root(config)
    with FileLock(str(root / "ledger.lock"), timeout=0):
        with pytest.raises(Timeout):
            get_claim(config)
    assert not list(root.rglob("claims/*.json"))


def test_archived_interrupted_native_run_requires_actual_terminal_time(config, tmp_path):
    first = get_claim(config)
    kwargs = dict(event_id=first["event"]["event_id"], token=first["claim"]["token"], now=NOW)
    path = proof(tmp_path, first, visible=False, run_status="interrupted", completed_at=None)
    with pytest.raises(ValueError, match="completion time"):
        notices.confirm(config, proof_path=path, **kwargs)
    done = notices.confirm(config, proof_path=proof(tmp_path, first, visible=False, run_status="interrupted"), **kwargs)
    assert done["status"] == "CONFIRMED_UNDELIVERED"


def test_oversized_evidence_is_rejected_before_open(tmp_path, monkeypatch):
    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * 65537)
    def forbidden(*args, **kwargs):
        pytest.fail("Oversized evidence must be rejected before opening")
    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(ValueError, match="bounded size"):
        notices._read(path)
