"""Same-day plan revisions retain real outstanding commitments and source history."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AccountSnapshot, AuthorityBinding, AuthorityError,
    ReservationRequest,
)


ACCOUNT = "a" * 64
SOURCES = {"Atlas": "b" * 64, "Scout": "c" * 64}
BINDING = AuthorityBinding(ACCOUNT, "Atlas", {"Atlas": ("AAPL",), "Scout": ("MSFT",)})


@pytest.fixture
def env(tmp_path):
    now = [datetime(2026, 10, 19, 22, tzinfo=timezone.utc)]
    authority = AccountAuthority(tmp_path / "authority.sqlite3", BINDING, clock=lambda: now[0])
    lease = authority.acquire_lease(coordinator_id="Atlas", holder_id="offline-fixture")
    original = AccountSnapshot(
        "original", ACCOUNT, "generation-original", "2026-10-19", SOURCES,
        now[0].isoformat(), "10000", "100", "95", "0", "1000",
        {"AAPL": "0", "MSFT": "0"}, {"AAPL": "1000", "MSFT": "1000"},
        {"AAPL": 100, "MSFT": 100}, evidence_fingerprint="d" * 64,
        identity_before=ACCOUNT, identity_after=ACCOUNT, complete=True,
    )
    authority.publish_snapshot(lease.token, original)
    return authority, lease.token, now, original


def request(snapshot, identity, quantity=3):
    return ReservationRequest(
        identity, snapshot.snapshot_id, snapshot.generation, snapshot.action_date,
        snapshot.source_fingerprints, "Atlas", "AAPL", "1h", identity + "-forecast",
        "BUY", quantity, "10", "10", identity + "-native", identity + "-allocation",
    )


def revision(env):
    authority, token, now, original = env
    now[0] += timedelta(seconds=1)
    revised = replace(original, snapshot_id="revision", generation="generation-revised",
                      source_fingerprints={"Atlas": "e" * 64, "Scout": "f" * 64},
                      observed_at=now[0].isoformat())
    authority.publish_snapshot(token, revised)
    return revised


def test_same_day_revision_executes_against_current_snapshot_without_freeing_original_commitments(env):
    authority, token, now, original = env
    first = authority.reserve(token, request(original, "original-order", quantity=6))
    revised = revision(env)
    second = authority.reserve(token, request(revised, "revised-order"))
    submitted = authority.submit(token, second.reservation_id, lambda gate: (gate(), "2002")[1])
    assert submitted.broker_order_id == "2002"
    assert authority.reservation(first.reservation_id) == first
    assert authority.reserve(token, request(original, "original-order", quantity=6)) == first
    with pytest.raises(AuthorityError, match="BUDGET_EXHAUSTED"):
        authority.reserve(token, request(revised, "overdraw", quantity=1))
    with pytest.raises(AuthorityError, match="LATEST_ACCOUNT_SNAPSHOT"):
        authority.reserve(token, request(original, "stale-source"))
    with pytest.raises(AuthorityError, match="SOURCE_BINDING_MISMATCH"):
        authority.reserve(token, replace(request(revised, "mixed"), source_fingerprints=SOURCES))


def test_revised_generation_cannot_rewrite_original_generation_source_bytes(env):
    authority, token, now, original = env
    revised = revision(env)
    now[0] += timedelta(seconds=1)
    with pytest.raises(AuthorityError, match="GENERATION_SOURCE_BINDING_IS_IMMUTABLE"):
        authority.publish_snapshot(token, replace(revised, snapshot_id="rewrite", generation=original.generation,
                                                 observed_at=now[0].isoformat()))
    assert authority.reserve(token, request(revised, "still-current")).status == "RESERVED"


def test_revision_does_not_release_unknown_submission_or_its_identity(env):
    authority, token, now, original = env
    first = authority.reserve(token, request(original, "unknown"))
    def uncertain(gate):
        gate()
        raise TimeoutError("fixture ambiguous response")
    with pytest.raises(TimeoutError):
        authority.submit(token, first.reservation_id, uncertain)
    revised = revision(env)
    assert authority.reservation(first.reservation_id).status == "UNKNOWN"
    with pytest.raises(AuthorityError, match="UNCERTAIN_SUBMISSION"):
        authority.reserve(token, request(revised, "after-unknown"))


def test_legacy_plan_and_all_snapshot_bytes_survive_additive_history_upgrade(env):
    authority, token, now, original = env
    with sqlite3.connect(authority.path) as db:
        legacy = db.execute("SELECT * FROM plans").fetchall()
        snapshots = db.execute("SELECT * FROM snapshots").fetchall()
        # Recreate the exact pre-repair schema, retaining its original rows.
        db.execute("DROP TABLE IF EXISTS plan_revisions")
    reopened = AccountAuthority(authority.path, BINDING, clock=lambda: now[0])
    revised = revision((reopened, token, now, original))
    AccountAuthority(authority.path, BINDING, clock=lambda: now[0])
    with sqlite3.connect(authority.path) as db:
        assert db.execute("SELECT * FROM plans").fetchall() == legacy
        assert db.execute("SELECT * FROM snapshots WHERE id=?", (original.snapshot_id,)).fetchall() == snapshots
        rows = db.execute("SELECT generation, action_date, sources FROM plan_revisions ORDER BY generation").fetchall()
    assert len(rows) == 2
    assert {generation for generation, _, _ in rows} == {original.generation, revised.generation}
    assert all(action_date == original.action_date for _, action_date, _ in rows)
    assert {json.dumps(json.loads(sources), sort_keys=True) for _, _, sources in rows} == {
        json.dumps(SOURCES, sort_keys=True), json.dumps(revised.source_fingerprints, sort_keys=True)}
