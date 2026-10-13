"""Temporary canonical books only; read-only views never capture or reconcile."""
from dataclasses import asdict
from datetime import timedelta
from decimal import localcontext
import hashlib
import json
import re
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ml.account_gameplan.authority import Fill, OrderEvidence
from ml.intraday.admission_service import AdmissionError
from ml.intraday.scout_journal import JournalError
from ml.intraday.admission_views import AdmissionViews, _amount, _gross_amount
from ml.intraday.admission_contract import validate_view_envelope, READINESS_VERSION, ACCOUNTING_VERSION
from ml.intraday.scout_protocol import MAX_INTEGER, canonical_sha256
from tests.test_intraday_admission_service import Fixture, request, bound
from tests.test_intraday_account_authority import ACCOUNT, BINDING


def writers(clock):
    return {name: dict(ready=True, compatibility_state="absent", reason_codes=[], loaded_code_proven=False,
        observed_at=clock().isoformat(), private_path="never-export-this", worker_pid=123)
        for name in ("atlas", "gameplan")}


def views(env):
    return AdmissionViews(datastore=env.root, binding=BINDING, server_identity="reviewed-atlas-server",
        writers=lambda: writers(env.clock), binding_check=lambda: None, clock=env.clock)


def body(value, nonce="view-one"):
    return dict(request_nonce=nonce, **bound(value))


def fingerprints(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*.sqlite3")}


def test_readiness_is_scoped_fresh_and_creates_no_books(tmp_path):
    from tests.test_intraday_account_authority import Clock
    clock = Clock()
    value = AdmissionViews(datastore=tmp_path, binding=BINDING, server_identity="reviewed-atlas-server",
        writers=lambda: writers(clock), binding_check=lambda: None, clock=clock)
    result = value.readiness("pc-new", {"request_nonce": "probe-one"})
    assert result["ready"] is True and result["orders_or_commitments_created"] == 0
    assert list(tmp_path.rglob("*.sqlite3")) == []
    assert "never-export-this" not in json.dumps(result) and "worker_pid" not in json.dumps(result)
    validate_view_envelope(result, version=READINESS_VERSION, request_nonce="probe-one",
        server_identity="reviewed-atlas-server", account_fingerprint=ACCOUNT, now=clock())
    with pytest.raises(AdmissionError):
        value.readiness("pc-original", {"request_nonce": "probe-one"})


def test_incompatible_writer_never_becomes_ready_and_expiry_respects_oldest_proof(tmp_path):
    env = Fixture(tmp_path)
    value = views(env)
    proofs = writers(env.clock)
    proofs["atlas"].update(ready=False, compatibility_state="blocked", reason_codes=["OLD_LOADED_WRITER"])
    value.writers = lambda: proofs
    result = value.readiness("pc-new", {"request_nonce": "blocked"})
    assert result["ready"] is False and result["reason_codes"] == ["ATLAS:OLD_LOADED_WRITER"]
    proofs["atlas"].update(ready=True, compatibility_state="compatible", reason_codes=[],
        observed_at=(env.clock() - timedelta(seconds=115)).isoformat())
    result = value.readiness("pc-new", {"request_nonce": "fresh"})
    assert result["expires_at"] == (env.clock() + timedelta(seconds=5)).isoformat()
    # The manual session heartbeat can expire before a newer code proof does.
    proofs["atlas"].update(observed_at=env.clock().isoformat(), expires_at=(env.clock() + timedelta(seconds=2)).isoformat())
    result = value.readiness("pc-new", {"request_nonce": "manual-age-bound"})
    assert result["expires_at"] == (env.clock() + timedelta(seconds=2)).isoformat()


def test_readiness_time_follows_advancing_writer_observations_and_rejects_expired_proof(tmp_path):
    env = Fixture(tmp_path)
    value = views(env)
    def advancing():
        env.clock.tick()
        return env.clock()
    value.clock = advancing
    def advancing_writers():
        advancing()
        return writers(env.clock)
    value.writers = advancing_writers
    response = value.readiness("pc-new", {"request_nonce": "advancing-clock"})
    assert response["ready"] is True and response["observed_at"] == env.clock().isoformat()
    def expiring_writers():
        result = writers(env.clock)
        result["atlas"]["expires_at"] = (env.clock() + timedelta(milliseconds=500)).isoformat()
        return result
    value.writers = expiring_writers
    with pytest.raises(AdmissionError, match="WRITER_PROOF_STALE_OR_FUTURE"):
        value.readiness("pc-new", {"request_nonce": "expired-mid-check"})


def test_readiness_codes_and_hashes_are_bounded_and_ordered_for_every_state(tmp_path):
    env = Fixture(tmp_path)
    value = views(env)
    proofs = writers(env.clock)
    proofs["atlas"].update(ready=False, compatibility_state="blocked", reason_codes=["OLD_WRITER", "PROOF_MISSING"])
    proofs["gameplan"].update(ready=False, compatibility_state="blocked", reason_codes=["UNVERIFIED"])
    value.writers = lambda: {name: proofs[name] for name in ("gameplan", "atlas")}
    response = value.readiness("pc-new", {"request_nonce": "bounded"})
    assert response["ready"] is False and response["protocol_compatible"] is True
    assert response["reason_codes"] == ["ATLAS:OLD_WRITER", "ATLAS:PROOF_MISSING", "GAMEPLAN:UNVERIFIED"]
    for proof in response["writers"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", proof["proof_sha256"])
    proofs["atlas"].update(ready=True, compatibility_state="compatible", reason_codes=[])
    proofs["gameplan"].update(ready=True, compatibility_state="absent", reason_codes=[])
    response = value.readiness("pc-new", {"request_nonce": "all-states"})
    assert response["ready"] is True and response["reason_codes"] == []
    assert all(re.fullmatch(r"[0-9a-f]{64}", p["proof_sha256"]) for p in response["writers"].values())


@pytest.mark.parametrize("change", [
    {"reason_codes": ["C:/private/path"]}, {"reason_codes": ["X" * 129]},
    {"reason_codes": ["CODE"] * 33}, {"reason_codes": "CODE"}, {"reason_codes": [4]},
    {"reason_codes": []}, {"ready": True}, {"compatibility_state": "unknown"}, {"ready": 1},
])
def test_malformed_writer_proofs_fail_without_echoing_private_codes(tmp_path, change):
    env = Fixture(tmp_path)
    value = views(env)
    proofs = writers(env.clock)
    proofs["atlas"].update(ready=False, compatibility_state="blocked", reason_codes=["BLOCKED"])
    proofs["atlas"].update(change)
    value.writers = lambda: proofs
    with pytest.raises(AdmissionError, match="^INVALID_WRITER_PROOF$"):
        value.readiness("pc-new", {"request_nonce": "malformed"})


def test_accounting_exact_original_pair_and_fills_preserves_every_database_byte(tmp_path):
    env = Fixture(tmp_path)
    req, grant, claim = env.accepted()
    env.clock.tick()
    evidence = OrderEvidence("exact-terminal", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
        "CANCELLED", grant.quantity, 2, 0, (Fill("fill-one", 2, "10", env.clock().isoformat()),), complete=True)
    env.service.evidence("pc-new", bound(req, event_id="terminal-event", kind="ORDER_STATUS", claim_id=claim["claim_id"],
        order_evidence=json.loads(json.dumps(asdict(evidence)))))
    before = fingerprints(tmp_path)
    result = views(env).accounting("pc-new", body(req))
    projection = result["projection"]
    assert projection["native_reservation"]["reservation_id"] != grant.reservation_id
    assert projection["account_reservation"]["reservation_id"] == grant.reservation_id
    assert projection["fills"][0]["fill_id"] == "fill-one"
    assert projection["totals"] == dict(filled_quantity=2, gross_cost="20", gross_proceeds="0", cost_basis=None,
        realized_pnl=None, basis_status="unknown", fees=None)
    assert result["snapshot_sha256"] == canonical_sha256(projection)
    assert fingerprints(tmp_path) == before
    validate_view_envelope(result, version=ACCOUNTING_VERSION, request_nonce="view-one",
        server_identity="reviewed-atlas-server", account_fingerprint=ACCOUNT, now=env.clock())


def test_accounting_rechecks_redirected_database_sidecar_before_sqlite_open(tmp_path, monkeypatch):
    env = Fixture(tmp_path)
    req, _ = env.grant()
    value = views(env)
    redirected = Path(str(env.service.path) + "-wal")
    original_lstat = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == redirected:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0x400)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("opened redirected SQLite path"))
    with pytest.raises(JournalError, match="symlinks"):
        value.accounting("pc-new", body(req))


@pytest.mark.parametrize("change", [{"request_id": "different"}, {"request_sha256": "b" * 64}, {"session_id": "different"}])
def test_accounting_rejects_cross_request_or_session(tmp_path, change):
    env = Fixture(tmp_path)
    req, _ = env.grant()
    before = fingerprints(tmp_path)
    with pytest.raises(AdmissionError):
        views(env).accounting("pc-new", {**body(req), **change})
    assert fingerprints(tmp_path) == before


@pytest.mark.parametrize("damage", ["broker", "quantity", "side", "state", "stream", "allocation_stream", "allocation_horizon", "fill"])
def test_accounting_partial_or_corrupt_pair_never_reports_complete_projection(tmp_path, damage):
    env = Fixture(tmp_path)
    req, grant, _ = env.accepted()
    with sqlite3.connect(env.ledger.path) as db:
        if damage == "broker": db.execute("UPDATE reservations SET broker_order='999'")
        if damage == "quantity": db.execute("UPDATE reservations SET quantity=quantity+1")
        if damage == "side": db.execute("UPDATE reservations SET side='SELL'")
        if damage == "state": db.execute("UPDATE reservations SET status='RESERVED'")
        if damage == "stream": db.execute("UPDATE intraday_streams SET stream='atlas-15m' WHERE kind='reservation'")
        if damage == "allocation_stream": db.execute("UPDATE intraday_streams SET stream='atlas-15m' WHERE kind='allocation'")
        if damage == "allocation_horizon": db.execute("UPDATE allocations SET horizon='1d'")
        if damage == "fill":
            rid = db.execute("SELECT id FROM reservations").fetchone()[0]
            db.execute("INSERT INTO fills VALUES(?,?,?,?,?)", ("unshared-fill", rid, 1, "10", env.clock().isoformat()))
    before = fingerprints(tmp_path)
    with pytest.raises(AdmissionError):
        views(env).accounting("pc-new", body(req))
    assert fingerprints(tmp_path) == before


def test_existing_stock_sale_retains_unknown_basis_and_exact_native_slices(tmp_path):
    env = Fixture(tmp_path)
    req, grant, claim = env.accepted(request(side="SELL", quantity=3))
    env.clock.tick()
    evidence = OrderEvidence("sale-terminal", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
        "CANCELLED", 3, 1, 0, (Fill("sold-one", 1, "10", env.clock().isoformat()),), complete=True)
    env.service.evidence("pc-new", bound(req, event_id="sale-terminal-event", kind="ORDER_STATUS", claim_id=claim["claim_id"],
        order_evidence=json.loads(json.dumps(asdict(evidence)))))
    result = views(env).accounting("pc-new", body(req))["projection"]
    assert result["totals"]["gross_proceeds"] == "10" and result["totals"]["cost_basis"] is None
    assert result["allocations"][0]["observed_inventory"] and result["allocations"][0]["purchase_fills"] == []
    assert result["fill_attribution"][0]["quantity"] == 1 and result["sell_slices"][0]["quantity"] == 3


def test_clipped_request_and_tiny_exact_fill_use_original_scope_and_ordinary_money(tmp_path):
    env = Fixture(tmp_path)
    req, grant, claim = env.accepted()
    assert req.quantity == 20 and grant.quantity == 10
    env.clock.tick()
    evidence = OrderEvidence("tiny-terminal", grant.reservation_id, ACCOUNT, "201", env.clock().isoformat(),
        "CANCELLED", grant.quantity, 1, 0, (Fill("tiny-fill", 1, "0.00000001", env.clock().isoformat()),), complete=True)
    env.service.evidence("pc-new", bound(req, event_id="tiny-terminal-event", kind="ORDER_STATUS", claim_id=claim["claim_id"],
        order_evidence=json.loads(json.dumps(asdict(evidence)))))
    before = fingerprints(tmp_path)
    projection = views(env).accounting("pc-new", body(req))["projection"]
    assert projection["request"]["quantity"] == 20
    assert projection["account_reservation"]["quantity"] == projection["native_reservation"]["quantity"] == 10
    assert projection["fills"][0]["price"] == projection["totals"]["gross_cost"] == "0.00000001"
    assert projection["allocations"][0]["status"] == "ACTIVE"
    assert fingerprints(tmp_path) == before


@pytest.mark.parametrize("raw,expected", [
    ("1E-8", "0.00000001"), ("1.2300", "1.23"), ("0.0000", "0"),
    ("1E-126", "0." + "0" * 125 + "1"), ("1E127", "1" + "0" * 127),
    ("1" * 126 + ".1", "1" * 126 + ".1"),
])
def test_canonical_money_bounds_before_expansion_without_rounding(raw, expected):
    with localcontext() as context:
        context.prec = 2
        result = _amount(raw)
    assert result == expected and len(result) <= 128
    assert re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?", result)


@pytest.mark.parametrize("raw", [
    "1E-127", "1E128", "1E999999999", "1E-999999999",
    "9" * 129, "0." + "1" * 127, "NaN", "Infinity", "-1", "not-a-number",
])
def test_unsupported_money_is_rejected_before_exponent_expansion(raw):
    with pytest.raises(AdmissionError, match="^(?:INVALID_CANONICAL_ACCOUNTING_AMOUNT|CANONICAL_ACCOUNTING_AMOUNT_OUT_OF_BOUNDS)$"):
        _amount(raw)


def test_exact_gross_does_not_use_ambient_decimal_precision_or_exponent_output():
    fills = [dict(price="1234567890123456789012345.12345678", quantity=3),
             dict(price="0.00000001", quantity=1)]
    with localcontext() as context:
        context.prec = 2
        assert _gross_amount(fills) == "3703703670370370367037035.37037035"
        assert _gross_amount([dict(price="1E-8", quantity=1)]) == "0.00000001"
        assert _gross_amount([dict(price="0." + "0" * 125 + "1", quantity=MAX_INTEGER)]) == (
            "0." + "0" * (126 - len(str(MAX_INTEGER))) + str(MAX_INTEGER))
        assert _gross_amount([]) == "0"


@pytest.mark.parametrize("fills,code", [
    ([dict(price="9" * 128, quantity=1), dict(price="1", quantity=1)], "CANONICAL_ACCOUNTING_AMOUNT_OUT_OF_BOUNDS"),
    ([dict(price="1", quantity=MAX_INTEGER), dict(price="1", quantity=1)], "INVALID_CANONICAL_ACCOUNTING_QUANTITY"),
    ([dict(price="1", quantity=True)], "INVALID_CANONICAL_ACCOUNTING_QUANTITY"),
    ([dict(price="1", quantity=MAX_INTEGER + 1)], "INVALID_CANONICAL_ACCOUNTING_QUANTITY"),
])
def test_gross_bounds_fail_without_truncating_or_rounding(fills, code):
    with pytest.raises(AdmissionError, match="^" + code + "$"):
        _gross_amount(fills)
