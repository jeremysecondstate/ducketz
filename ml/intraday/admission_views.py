"""Authenticated, noncommitting views of Atlas's canonical admission books.

Both views serialize with writers. Accounting uses only mode=ro/query_only
SQLite connections, never ledger constructors, leases, captures or providers.
Only the original Scout request and its directly attributed native lots leave
this boundary. Gross fill values are exact; no tax-lot or cost-basis policy is
invented for observed opening inventory or fungible historical allocations.
"""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.authority import AccountAuthority, VERSION as AUTHORITY_VERSION
from ml.stock_trader.horizon_ledger import LEDGER_VERSION
from .admission_service import AdmissionError, PROTOCOL_VERSION, PARTICIPANT, STREAM, _unpacked
from .scout_protocol import Grant, Request, MAX_INTEGER, canonical_sha256
from .scout_journal import _safe_path
from .admission_contract import READINESS_VERSION, ACCOUNTING_VERSION, RESPONSE_MAX_AGE_SECONDS, readiness_request, accounting_request


def _require(condition, code):
    if not condition:
        raise AdmissionError(code)


def _instant(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "AWARE_VIEW_CLOCK_REQUIRED")
    return parsed.astimezone(timezone.utc)


@contextmanager
def _readonly(path):
    path = _safe_path(path)
    _require(path.is_file(), "CANONICAL_ACCOUNTING_BOOK_UNAVAILABLE")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


MAX_AMOUNT_LENGTH = 128
MAX_AMOUNT_SCALE = MAX_AMOUNT_LENGTH - 2


def _ordinary_amount(digits, exponent):
    """Render bounded integer coefficient/exponent without Decimal rounding."""
    digits = digits.lstrip("0")
    if not digits:
        return "0"
    trimmed = digits.rstrip("0")
    exponent += len(digits) - len(trimmed)
    digits = trimmed
    scale = max(0, -exponent)
    integer_places = max(1, len(digits) + exponent)
    _require(scale <= MAX_AMOUNT_SCALE
        and integer_places + (scale + 1 if scale else 0) <= MAX_AMOUNT_LENGTH,
        "CANONICAL_ACCOUNTING_AMOUNT_OUT_OF_BOUNDS")
    if exponent >= 0:
        return digits + "0" * exponent
    point = len(digits) + exponent
    return (digits[:point] + "." + digits[point:] if point > 0
            else "0." + "0" * -point + digits)


def _amount(value):
    # Native books can retain Decimal exponent notation. Bound its raw input
    # before parsing, then its coefficient/scale before expanding any exponent.
    raw = str(value)
    _require(len(raw) <= MAX_AMOUNT_LENGTH, "CANONICAL_ACCOUNTING_AMOUNT_OUT_OF_BOUNDS")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise AdmissionError("INVALID_CANONICAL_ACCOUNTING_AMOUNT") from None
    _require(number.is_finite() and number >= 0, "INVALID_CANONICAL_ACCOUNTING_AMOUNT")
    parts = number.as_tuple()
    return _ordinary_amount("".join(str(digit) for digit in parts.digits), parts.exponent)


def _gross_amount(fills):
    """Exact bounded integer arithmetic; the ambient Decimal context is unused."""
    units, quantity = 0, 0
    for fill in fills:
        count = fill["quantity"]
        _require(type(count) is int and 0 < count <= MAX_INTEGER,
                 "INVALID_CANONICAL_ACCOUNTING_QUANTITY")
        quantity += count
        _require(quantity <= MAX_INTEGER, "INVALID_CANONICAL_ACCOUNTING_QUANTITY")
        amount = _amount(fill["price"])
        whole, _, fraction = amount.partition(".")
        units += int(whole + fraction) * 10 ** (MAX_AMOUNT_SCALE - len(fraction)) * count
    # Each accepted amount has at most 128 ordinary characters, total quantity
    # is signed-64-bit bounded, and this accumulator has at most 273 digits.
    return _ordinary_amount(str(units), -MAX_AMOUNT_SCALE)


class AdmissionViews:
    def __init__(self, *, datastore, binding, server_identity, writers, binding_check,
                 clock=lambda: datetime.now(timezone.utc), response_age_seconds=30,
                 heartbeat_age_seconds=120):
        for relative in ("state/intraday-admission/journal.sqlite3", "state/account-gameplan/authority.sqlite3",
                         "state/independent-stock-trader/holdings.sqlite3"):
            _safe_path(Path(datastore) / relative)
        self.root = Path(datastore).resolve()
        self.binding = AccountAuthority._binding(binding)
        self.server_identity, self.writers, self.binding_check, self.clock = server_identity, writers, binding_check, clock
        _require(type(response_age_seconds) in (int, float) and 0 < response_age_seconds <= RESPONSE_MAX_AGE_SECONDS
                 and type(heartbeat_age_seconds) in (int, float) and 0 < heartbeat_age_seconds <= 300,
                 "BOUNDED_VIEW_FRESHNESS_REQUIRED")
        self.response_age, self.heartbeat_age = response_age_seconds, heartbeat_age_seconds

    def _request(self, participant, body, extra=()):
        _require(participant == PARTICIPANT, "AUTHENTICATED_SCOUT_PARTICIPANT_REQUIRED")
        accounting_request(body) if extra else readiness_request(body)

    def _base(self, version, nonce, now):
        return dict(version=version, protocol_version=PROTOCOL_VERSION, server_identity=self.server_identity,
            participant_id=PARTICIPANT, stream=STREAM, account_fingerprint=self.binding["account_fingerprint"],
            request_nonce=nonce, observed_at=now.isoformat(), expires_at=(now + timedelta(seconds=self.response_age)).isoformat(),
            authenticated=True, protocol_compatible=True, read_only=True, orders_or_commitments_created=0)

    def readiness(self, participant, body):
        self._request(participant, body)
        with exclusive_runtime_lock(self.root / "locks/stock-trader-hourly.lock", process_name="stock-trader-hourly"):
            self.binding_check()
            started = _instant(self.clock())
            proofs = self.writers()
            now = _instant(self.clock())
            _require(now >= started, "VIEW_CLOCK_MOVED_BACKWARDS")
            _require(type(proofs) is dict and set(proofs) == {"atlas", "gameplan"}, "EXACT_WRITER_PROOFS_REQUIRED")
            writers, reasons = {}, []
            expires = now + timedelta(seconds=self.response_age)
            for name in ("atlas", "gameplan"):
                proof = proofs[name]
                _require(type(proof) is dict and type(proof.get("ready")) is bool, "INVALID_WRITER_PROOF")
                ready = proof["ready"]
                state = proof.get("compatibility_state")
                if state is None:
                    state = {"verified_gameplan_absent": "absent", "loaded_gameplan_verified": "compatible"}.get(proof.get("mode"), "blocked")
                codes = proof.get("reason_codes")
                _require(type(state) is str and state in {"compatible", "absent", "blocked"}
                    and type(codes) is list and len(codes) <= 32
                    and all(type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_:.-]{0,127}", code) for code in codes)
                    and ((ready and state in {"compatible", "absent"} and not codes)
                         or (not ready and state == "blocked" and bool(codes))), "INVALID_WRITER_PROOF")
                observed = proof.get("observed_at", proof.get("heartbeat_at"))
                if ready:
                    _require(observed is not None and 0 <= (now - _instant(observed)).total_seconds() < self.heartbeat_age,
                             "WRITER_PROOF_STALE_OR_FUTURE")
                    expires = min(expires, _instant(observed) + timedelta(seconds=self.heartbeat_age))
                    if proof.get("expires_at") is not None:
                        _require(_instant(proof["expires_at"]) > now, "WRITER_PROOF_STALE_OR_FUTURE")
                        expires = min(expires, _instant(proof["expires_at"]))
                reasons.extend(name.upper() + ":" + code for code in codes)
                try:
                    digest = canonical_sha256(proof)
                except (TypeError, ValueError):
                    raise AdmissionError("INVALID_WRITER_PROOF") from None
                writers[name] = dict(ready=ready, compatibility_state=state,
                    proof_sha256=digest, reason_codes=list(codes))
            response = self._base(READINESS_VERSION, body["request_nonce"], now)
            return dict(response, expires_at=expires.isoformat(), ready=all(p["ready"] for p in writers.values()),
                        reason_codes=reasons, writers=writers)

    def accounting(self, participant, body):
        self._request(participant, body, ("request_id", "request_sha256", "session_id"))
        with exclusive_runtime_lock(self.root / "locks/stock-trader-hourly.lock", process_name="stock-trader-hourly"):
            self.binding_check()
            with ExitStack() as stack:
                journal = stack.enter_context(_readonly(self.root / "state/intraday-admission/journal.sqlite3"))
                authority = stack.enter_context(_readonly(self.root / "state/account-gameplan/authority.sqlite3"))
                native = stack.enter_context(_readonly(self.root / "state/independent-stock-trader/holdings.sqlite3"))
                expected_binding = dict(protocol_version=PROTOCOL_VERSION, server_identity=self.server_identity, **self.binding)
                saved = journal.execute("SELECT payload FROM binding WHERE id=1").fetchone()
                _require(saved is not None and json.loads(saved[0]) == expected_binding, "ACCOUNTING_SERVICE_BINDING_MISMATCH")
                saved = authority.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
                value = None if saved is None else json.loads(saved[0])
                _require(value is not None and value.get("version") == AUTHORITY_VERSION
                         and all(value.get(k) == v for k, v in self.binding.items()), "ACCOUNTING_AUTHORITY_BINDING_MISMATCH")
                saved = native.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
                _require(saved is not None and saved[0] == self.binding["account_fingerprint"], "ACCOUNTING_NATIVE_BINDING_MISMATCH")
                saved = native.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
                _require(saved is not None and saved[0] == LEDGER_VERSION, "ACCOUNTING_NATIVE_VERSION_MISMATCH")
                row = journal.execute("SELECT * FROM requests WHERE id=?", (body["request_id"],)).fetchone()
                _require(row is not None, "UNKNOWN_ACCOUNTING_REQUEST")
                request = Request.from_payload(json.loads(row["payload"]))
                _require(request.sha256 == row["sha"] == body["request_sha256"] and request.session_id == body["session_id"]
                    and request.account_fingerprint == self.binding["account_fingerprint"]
                    and request.symbol in self.binding["participants"][PARTICIPANT], "ACCOUNTING_ORIGINAL_REQUEST_SCOPE_MISMATCH")
                for column in ("plan", "response"):
                    if row[column] is not None:
                        _unpacked(row[column])
                projection = self._projection(authority, native, request)
                response = None if row["response"] is None else _unpacked(row["response"])
                if response is not None and "grant" in response:
                    grant = Grant.from_payload(response["grant"]).validate(request)
                    _require(grant.reservation_id == projection["account_reservation"]["reservation_id"]
                        and grant.quantity == projection["account_reservation"]["quantity"], "ACCOUNTING_JOURNAL_GRANT_DIFFERS_FROM_BOOKS")
                now = _instant(self.clock())
                result = dict(self._base(ACCOUNTING_VERSION, body["request_nonce"], now), request_id=request.request_id,
                    request_sha256=request.sha256, session_id=request.session_id, status="AVAILABLE", projection=projection,
                    snapshot_sha256=canonical_sha256(projection))
                from .admission_transport import encode_json
                encode_json(result)  # A complete bounded snapshot or an error, never truncation.
                return result

    def _projection(self, authority, native, request):
        key = STREAM + ":" + request.sha256
        account = authority.execute("SELECT * FROM reservations WHERE key=?", (key,)).fetchone()
        _require(account is not None, "ACCOUNTING_REQUEST_NOT_ADMITTED")
        ar = json.loads(account["request"])
        _require(ar.get("participant_id") == PARTICIPANT and ar.get("stream") == STREAM
            and ar.get("remote_request_sha256") == request.sha256 and ar.get("account_fingerprint") == request.account_fingerprint
            and ar.get("symbol") == request.symbol and ar.get("side") == request.side and ar.get("forecast_id") == request.forecast_id
            and type(ar.get("quantity")) is int and 0 < ar["quantity"] <= request.quantity
            and Decimal(ar["limit_price"]) == Decimal(request.limit_price),
            "ACCOUNTING_RESERVATION_SCOPE_MISMATCH")
        order = native.execute("SELECT * FROM reservations WHERE id=?", (ar["native_reservation_id"],)).fetchone()
        _require(order is not None and order["allocation"] == ar["native_allocation_id"]
            and order["idempotency_key"] == key and order["batch"] == request.session_id
            and order["side"] == request.side and order["quantity"] == ar["quantity"] and order["filled"] == account["filled"]
            and order["broker_order"] == account["broker_id"], "ACCOUNTING_CANONICAL_PAIR_MISMATCH")
        expected_status = {"SUBMITTING": "UNKNOWN", "NOT_SUBMITTED": "REJECTED"}.get(account["status"], account["status"])
        _require(order["status"] == expected_status, "ACCOUNTING_CANONICAL_PAIR_MISMATCH")
        nr = json.loads(order["request"])
        _require(nr.get("stream") == STREAM and nr.get("forecast") == request.forecast_id
            and nr.get("symbol") == request.symbol and nr.get("side") == request.side
            and nr.get("quantity") == order["quantity"] and nr.get("batch") == request.session_id
            and Decimal(nr["price"]) == Decimal(order["price"]) == Decimal(request.limit_price), "ACCOUNTING_NATIVE_REQUEST_MISMATCH")
        marker = native.execute("SELECT stream FROM intraday_streams WHERE kind='reservation' AND id=?", (order["id"],)).fetchone()
        _require(marker is not None and marker[0] == STREAM, "ACCOUNTING_NATIVE_STREAM_MISMATCH")
        fills = [dict(fill_id=r["id"], quantity=r["quantity"], price=_amount(r["price"]), executed_at=r["executed_at"],
                      native_reservation_id=order["id"], account_reservation_id=account["id"])
                 for r in native.execute("SELECT * FROM fills WHERE reservation=? ORDER BY executed_at,id", (order["id"],))]
        af = {r["fill_id"]: json.loads(r["payload"]) for r in authority.execute("SELECT * FROM fills WHERE reservation=?", (account["id"],))}
        _require(set(af) == {f["fill_id"] for f in fills} and sum(f["quantity"] for f in fills) == account["filled"], "ACCOUNTING_EXACT_FILLS_DIFFER")
        for fill in fills:
            prior = af[fill["fill_id"]]
            _require(prior["fill_id"] == fill["fill_id"] and prior["quantity"] == fill["quantity"]
                and type(fill["quantity"]) is int and fill["quantity"] > 0 and Decimal(fill["price"]) > 0
                and Decimal(prior["price"]) == Decimal(fill["price"]) and _instant(prior["executed_at"]) == _instant(fill["executed_at"]),
                "ACCOUNTING_EXACT_FILLS_DIFFER")
        slices = [dict(allocation_id=r["allocation"], quantity=r["quantity"], ordinal=r["ordinal"])
                  for r in native.execute("SELECT * FROM intraday_sell_slices WHERE reservation=? ORDER BY ordinal", (order["id"],))]
        attributed = [dict(fill_id=r["fill"], allocation_id=r["allocation"], quantity=r["quantity"], slice_ordinal=r["slice_ordinal"])
            for r in native.execute("SELECT a.* FROM intraday_fill_attribution a JOIN fills f ON f.id=a.fill WHERE f.reservation=? ORDER BY a.fill,a.slice_ordinal", (order["id"],))]
        if request.side == "SELL":
            _require(sum(s["quantity"] for s in slices) == order["quantity"] and sum(a["quantity"] for a in attributed) == order["filled"],
                     "ACCOUNTING_SELL_ATTRIBUTION_DOES_NOT_BALANCE")
            by_slice = {s["ordinal"]: s for s in slices}
            for row in attributed:
                _require(row["slice_ordinal"] in by_slice and row["quantity"] > 0
                    and row["allocation_id"] == by_slice[row["slice_ordinal"]]["allocation_id"], "ACCOUNTING_SELL_ATTRIBUTION_DOES_NOT_BALANCE")
            _require(all(sum(a["quantity"] for a in attributed if a["fill_id"] == f["fill_id"]) == f["quantity"] for f in fills)
                and all(sum(a["quantity"] for a in attributed if a["slice_ordinal"] == s["ordinal"]) <= s["quantity"] for s in slices),
                "ACCOUNTING_SELL_ATTRIBUTION_DOES_NOT_BALANCE")
        else:
            _require(not slices and not attributed, "ACCOUNTING_BUY_HAS_SELL_ATTRIBUTION")
        allocations = []
        for identity in sorted({order["allocation"], *(s["allocation_id"] for s in slices)}):
            allocation = native.execute("SELECT * FROM allocations WHERE id=?", (identity,)).fetchone()
            _require(allocation is not None and allocation["account"] == request.account_fingerprint
                     and allocation["symbol"] == request.symbol, "ACCOUNTING_LOT_SCOPE_MISMATCH")
            if identity == order["allocation"]:
                marker = native.execute("SELECT stream FROM intraday_streams WHERE kind='allocation' AND id=?", (identity,)).fetchone()
                _require(allocation["horizon"] == "15m" and marker is not None and marker[0] == STREAM,
                         "ACCOUNTING_PRIMARY_ALLOCATION_SCOPE_MISMATCH")
            purchases = [dict(fill_id=r["id"], native_reservation_id=r["reservation"], quantity=r["quantity"],
                              price=_amount(r["price"]), executed_at=r["executed_at"])
                for r in native.execute("SELECT f.* FROM fills f JOIN reservations r ON r.id=f.reservation WHERE r.allocation=? AND r.side='BUY' ORDER BY f.executed_at,f.id", (identity,))]
            assignments = [] if not _table(native, "inventory_assignments") else [dict(r) for r in native.execute(
                "SELECT id,quantity,snapshot_id,observed_at FROM inventory_assignments WHERE allocation=? ORDER BY id", (identity,))]
            allocations.append(dict(allocation_id=identity, symbol=allocation["symbol"], horizon=allocation["horizon"],
                forecast_id=allocation["forecast"], target_start=allocation["start"], target_end=allocation["end"],
                status=allocation["status"], purchase_fills=purchases, observed_inventory=assignments))
        gross = _gross_amount(fills)
        def hashes(db, table, kind=None):
            query = "SELECT payload FROM " + table + (" WHERE kind=?" if kind else "")
            values = (kind,) if kind else ()
            result = []
            for (raw,) in db.execute(query, values):
                value = json.loads(raw)
                if value.get("reservation_id") == (account["id"] if table == "order_evidence" else order["id"]):
                    result.append(canonical_sha256(value))
            return sorted(result)
        return dict(request=dict(symbol=request.symbol, side=request.side, quantity=request.quantity,
                    limit_price=request.limit_price, forecast_id=request.forecast_id),
            account_reservation=dict(reservation_id=account["id"], status=account["status"], quantity=ar["quantity"],
                filled_quantity=account["filled"], broker_order_id=account["broker_id"], created_at=account["created"], last_evidence_at=account["last_evidence"]),
            native_reservation=dict(reservation_id=order["id"], allocation_id=order["allocation"], status=order["status"],
                quantity=order["quantity"], filled_quantity=order["filled"], broker_order_id=order["broker_order"], last_evidence_at=order["last_evidence_at"]),
            allocations=allocations, fills=fills, sell_slices=slices, fill_attribution=attributed,
            totals=dict(filled_quantity=account["filled"], gross_cost=gross if request.side == "BUY" else "0",
                gross_proceeds=gross if request.side == "SELL" else "0", cost_basis=None, realized_pnl=None,
                basis_status="unknown", fees=None),
            provenance=dict(journal_request_sha256=request.sha256, authority_request_sha256=canonical_sha256(ar),
                native_request_sha256=canonical_sha256(nr), account_order_evidence_sha256=hashes(authority, "order_evidence"),
                native_order_evidence_sha256=hashes(native, "evidence", "order")))
