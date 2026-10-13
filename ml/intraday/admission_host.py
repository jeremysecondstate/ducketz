"""Explicit Atlas admission host, composed from the existing canonical capture.

Construction and read-only views never create a broker, acquire a lease or open
books for writing. Only admission operations enter the canonical book context;
only reserve/send authorization refresh account evidence. This host contains no
order submission, cancellation, forecast generation or trader-start operation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from ml.account_gameplan.authority import AccountAuthority, AuthorityBinding, AuthorityError
from ml.account_gameplan.config import load_account_config, verify_cutover
from ml.account_gameplan.execution import AUTHORITY_PATH, _broker_pending
from ml.stock_trader.horizon_ledger import HorizonLedger
from .account_execution import IntradayAccountExecutor, NATIVE_PATH
from .admission_service import AdmissionError, AdmissionService, BookOperation, BookSnapshot
from .admission_views import AdmissionViews
from .scout_protocol import canonical_sha256
from .scout_journal import _safe_path


class _CaptureContext:
    """Only the inputs of existing _capture; no manual-session effect methods."""
    def __init__(self, host, operation):
        self.host, self.root, self.clock = host, host.root, host.clock
        self.config = host._checked_config()
        self.authority, self.ledger, self.lease = operation.authority, operation.ledger, operation.lease
        self.broker = host._broker()
        self.portfolio = self.snapshot_id = None

    def _assert_current(self):
        self.host._checked_config()
        self.host._require_compatible()
        self.host._capture_code_current()
        if self.broker.stable_account_fingerprint() != self.config.account_fingerprint:
            raise AdmissionError("BROKER_ACCOUNT_DIFFERS_FROM_ATLAS_BINDING")
        self.lease = self.authority.renew_lease(self.lease.token)


class AdmissionHost:
    def __init__(self, *, repository, datastore, binding_path, clock=lambda: datetime.now(timezone.utc),
                 heartbeat_age_seconds=120, response_age_seconds=30, broker_factory=None,
                 binding_loader=None, config_loader=load_account_config, cutover_verifier=verify_cutover,
                 atlas_reader=None, gameplan_reader=None):
        if binding_loader is None:
            from .admission_enrollment import load_host_binding
            binding_loader = load_host_binding
        from .atlas_writer import read_atlas_runtime_compatibility
        from .runtime_compatibility import read_gameplan_runtime_compatibility
        if any(str(value).startswith(("\\\\", "//")) or not Path(value).is_absolute() for value in (repository, datastore, binding_path)):
            raise AdmissionError("ABSOLUTE_HOST_LOCAL_PATHS_REQUIRED")
        for path in (Path(repository) / "ml/intraday/admission_host.py", Path(binding_path),
                     Path(datastore) / AUTHORITY_PATH, Path(datastore) / NATIVE_PATH,
                     Path(datastore) / "state/intraday-admission/journal.sqlite3"):
            _safe_path(path)  # Validate before resolve can hide a redirected ancestor.
        self.repository, self.root, self.binding_path = Path(repository).resolve(), Path(datastore).resolve(), Path(binding_path).resolve()
        self.clock, self.heartbeat_age = clock, heartbeat_age_seconds
        self.binding_loader = binding_loader
        self.config_loader, self.cutover_verifier = config_loader, cutover_verifier
        self.atlas_reader, self.gameplan_reader = atlas_reader or read_atlas_runtime_compatibility, gameplan_reader or read_gameplan_runtime_compatibility
        self.private_binding = self.binding_loader(self.binding_path)
        self.private_binding_sha256 = canonical_sha256(self.private_binding)
        self._credential_hashes = self._credential_fingerprints()
        self.config = None
        self.config = self._checked_config()
        self.binding = AuthorityBinding(self.config.account_fingerprint, self.config.coordinator_id, self.config.participants)
        self._broker_factory, self._broker_instance = broker_factory, None
        self.service = AdmissionService(datastore=self.root, binding=self.binding, server_identity=self.private_binding["server_identity"],
            compatibility=self._compatibility, context_factory=self._context, refresh=self._refresh, clock=clock)
        self.views = AdmissionViews(datastore=self.root, binding=self.binding, server_identity=self.private_binding["server_identity"],
            writers=self._writers, binding_check=self._view_binding_current, clock=clock, response_age_seconds=response_age_seconds,
            heartbeat_age_seconds=heartbeat_age_seconds)

    def _checked_config(self):
        for path in (self.repository / "ml/intraday/admission_host.py", self.binding_path,
                     self.root / AUTHORITY_PATH, self.root / NATIVE_PATH,
                     self.root / "state/intraday-admission/journal.sqlite3"):
            _safe_path(path)
        if canonical_sha256(self.binding_loader(self.binding_path)) != self.private_binding_sha256:
            raise AdmissionError("PRIVATE_HOST_BINDING_CHANGED_RELOAD_REQUIRED")
        if self._credential_fingerprints() != self._credential_hashes:
            raise AdmissionError("PRIVATE_HOST_CREDENTIALS_CHANGED_RELOAD_REQUIRED")
        config = self.config_loader(self.root)
        if (config is None or config.machine_id != "pc-original" or config.role != "coordinator"
                or config.activation.get("status") != "ACTIVE" or config.account_fingerprint != self.private_binding["account_fingerprint"]
                or set(config.participants["pc-new"]) != set(self.private_binding["scout_symbols"])
                or (self.config is not None and config.fingerprint != self.config.fingerprint)):
            raise AdmissionError("REVIEWED_ATLAS_ACCOUNT_AND_SCOUT_REGISTRY_REQUIRED")
        self.cutover_verifier(self.root, config)
        return config

    def _credential_fingerprints(self):
        # These bytes/digests stay inside this process, never in wire responses,
        # accounting, public source receipts or provider logs. They bind the
        # loaded TLS context to the exact enrollment files until explicit reload.
        try:
            return {name: hashlib.sha256(_safe_path(self.private_binding[name]).read_bytes()).digest()
                    for name in ("server_certificate", "server_private_key", "client_ca")}
        except (OSError, KeyError, TypeError):
            raise AdmissionError("PRIVATE_HOST_CREDENTIALS_UNAVAILABLE") from None

    def _writers(self):
        now = self.clock()
        return {"atlas": self.atlas_reader(self.root, self.repository, as_of=now, maximum_heartbeat_age_seconds=self.heartbeat_age),
                "gameplan": self.gameplan_reader(self.root, self.repository, as_of=now, maximum_heartbeat_age_seconds=self.heartbeat_age,
                                                 allow_verified_absence=True)}

    def _view_binding_current(self):
        self._checked_config()
        self._capture_code_current()

    def _compatibility(self):
        proofs = self._writers()
        atlas, gameplan = proofs["atlas"].get("ready") is True, proofs["gameplan"].get("ready") is True
        return dict(ready=atlas and gameplan, atlas_ready=atlas, gameplan_ready=gameplan)

    def _require_compatible(self):
        if self._compatibility()["ready"] is not True:
            raise AdmissionError("ALL_LOADED_WRITERS_MUST_SUPPORT_SHARED_SCOUT_BOOK")

    def _capture_code_current(self):
        from .runtime_compatibility import actual_intraday_executor_fingerprints, expected_intraday_executor_fingerprints
        # Class inspection proves the locally loaded capture implementation used
        # below. It is never substituted for a running writer's birth/heartbeat.
        if actual_intraday_executor_fingerprints(IntradayAccountExecutor) != expected_intraday_executor_fingerprints(self.repository):
            raise AdmissionError("LOADED_CAPTURE_DIFFERS_FROM_REVIEWED_SOURCE")
        original = IntradayAccountExecutor._account.__wrapped__.__globals__
        if original["AccountAuthority"] is not AccountAuthority or original["HorizonLedger"] is not HorizonLedger:
            raise AdmissionError("LOADED_CANONICAL_BOOK_CLASSES_DIFFER")

    def _broker(self):
        if self._broker_instance is None:
            if self._broker_factory is None:
                from dotenv import load_dotenv
                from app.services.schwab import SchwabSession
                load_dotenv(self.repository / ".env", override=False)
                self._broker_instance = SchwabSession()
            else:
                self._broker_instance = self._broker_factory()
        return self._broker_instance

    @contextmanager
    def _context(self):
        # AdmissionService holds the common stock-trader-hourly lock around
        # this entire context, including capture and the durable operation.
        config = self._checked_config()
        self._require_compatible()
        self._capture_code_current()
        authority = AccountAuthority(self.root / AUTHORITY_PATH, self.binding, clock=self.clock)
        ledger = HorizonLedger(self.root / NATIVE_PATH, config.account_fingerprint)
        lease = authority.acquire_lease(coordinator_id=config.machine_id, holder_id="atlas-admission:" + uuid.uuid4().hex)
        try:
            yield BookOperation(authority, ledger, lease)
        finally:
            try:
                authority.release_lease(lease.token)
            except AuthorityError:
                pass  # An expired/fenced lease is not renewed during cleanup.

    def _refresh(self, operation):
        context = _CaptureContext(self, operation)
        IntradayAccountExecutor._capture(context)
        portfolio = context.portfolio
        pending, exact = {}, {}
        for order in _broker_pending(portfolio):
            if order.side != "SELL":
                continue
            quantity = Decimal(str(order.remaining_quantity))
            if quantity != quantity.to_integral_value(rounding=ROUND_FLOOR) or quantity < 0 or not order.broker_order_id:
                raise AdmissionError("EXACT_PENDING_SELL_IDENTITY_REQUIRED")
            if not quantity:
                continue
            per_symbol = exact.setdefault(order.symbol, {})
            if order.broker_order_id in per_symbol:
                raise AdmissionError("DUPLICATE_PENDING_SELL_IDENTITY")
            per_symbol[order.broker_order_id] = int(quantity)
            pending[order.symbol] = pending.get(order.symbol, 0) + int(quantity)
        return BookSnapshot(context.snapshot_id, portfolio.observed_at, pending, exact)

    def capabilities(self, participant):
        self._checked_config()
        return self.service.capabilities(participant)

    def readiness(self, participant, body):
        return self.views.readiness(participant, body)

    def accounting(self, participant, body):
        return self.views.accounting(participant, body)

    def reserve(self, participant, body):
        return self.service.reserve(participant, body)

    def authorize_send(self, participant, body):
        return self.service.authorize_send(participant, body)

    def status(self, participant, body):
        return self.service.status(participant, body)

    def evidence(self, participant, body):
        return self.service.evidence(participant, body)

    def cancel_intent(self, participant, body):
        return self.service.cancel_intent(participant, body)

    def release_unsent(self, participant, body):
        return self.service.release_unsent(participant, body)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit Atlas admission host; default is read-only readiness")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--datastore", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--heartbeat-age-seconds", type=float, default=120)
    parser.add_argument("--response-age-seconds", type=float, default=30)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    host = AdmissionHost(repository=args.repository, datastore=args.datastore, binding_path=args.binding,
        heartbeat_age_seconds=args.heartbeat_age_seconds, response_age_seconds=args.response_age_seconds)
    if not args.serve:
        # Local diagnostic only: the same envelope here does not demonstrate
        # a remote authenticated connection or authorize a trader to start.
        result = host.readiness("pc-new", {"request_nonce": "host-check:" + uuid.uuid4().hex})
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ready"] is True else 1
    from .admission_transport import AdmissionHTTPSServer, CertificateParticipants, server_tls_context
    binding = host.private_binding
    tls = server_tls_context(certificate=binding["server_certificate"], private_key=binding["server_private_key"], client_ca=binding["client_ca"])
    host._checked_config()
    server = AdmissionHTTPSServer((binding["bind_host"], binding["bind_port"]), service=host,
        participants=CertificateParticipants({binding["client_certificate_sha256"]: "pc-new"}), tls_context=tls)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
