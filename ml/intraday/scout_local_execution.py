"""Manually started Scout effects against its real broker account.

This mode has no Atlas transport, enrollment, account ledger or horizon budget.
The existing Request/Grant DTOs and ScoutClient are reused solely as immutable
local attempt values and a one-send journal. A Grant here is an explicitly
recorded local capacity permit, never an authenticated Atlas authorization.
Broker snapshots supply cash already net of open orders and uncommitted shares.
Other PCs can change the account after capture; the broker remains the final
account-capacity check. Unknown sends never become capacity or another POST.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import sqlite3
import sys
from pathlib import Path
import uuid

from app.services.schwab import SchwabSession
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.stock_trader.horizon_ledger import ReservationState
from ml.stock_trader.state import capture_portfolio_state, validated_symbols

from .capacity import clip_to_capacity
from .dataset_diagnostics import capture_dataset_build, dataset_phase, recovery_phase
from .commitment_clearance import load_clearance
from .pretransport_clearance import load_clearance as load_pretransport_clearance
from .initial_gate_clearance import (load_clearance as load_initial_gate_clearance,
                                     create_clearance as create_initial_gate_clearance)
from .client_gate_clearance import (load_clearance as load_client_gate_clearance,
                                    create_clearance as create_client_gate_clearance)
from .exact_status import TERMINAL, retry_delay
from .intentions import IntentionStore
from .prediction import ContractError, digest, encoded, instant
from .store import utcnow
from .scout_session_books import ScoutBooks
from .scout_broker import ScoutBrokerAdapter
from .scout_client import ScoutClient, exception_details
from .local_forecast import forecast_disposition
from .scout_protocol import Grant, Request, canonical_sha256
from .local_forecast import _saved_forecast
from .starter_sizing import QuoteObservation, size_forecast
from .technical_sizing import AvailableInput


MODE = 'scout-local-broker-account'
PREFIX = 'scout-local:'
_NATIVE_PREPARED_DELETE = SchwabSession.cancel_prepared_order
_CANCELLATION_NOT_SENT = 'scout-local-native-cancellation-not-sent-v1'


class _CancellationForecastIneligible(ContractError):
    def __init__(self, disposition):
        super().__init__('Local forecast eligibility expired')
        self.disposition = disposition


def _record_local_evidence_freshness_failure(error, *, current, account_at, quote_at,
                                           request_issued_at, maximum_age_seconds):
    """Numeric failed-gate evidence only; never a new execution permission.

    Offsets retain the exact gate clock relative to the immutable request. The
    observed instants can then be recovered from the ages without persisting
    account values, quote prices, native IDs or arbitrary exception strings.
    """
    account_age = (current - instant(account_at)).total_seconds()
    quote_age = (current - instant(quote_at)).total_seconds()
    error.scout_local_evidence_freshness = dict(
        version='scout-local-evidence-freshness-v1', account_age_seconds=account_age,
        quote_age_seconds=quote_age, maximum_age_seconds=maximum_age_seconds,
        gate_since_request_seconds=(current - instant(request_issued_at)).total_seconds(),
        failed_inputs=(int(not 0 <= account_age <= maximum_age_seconds)
                       + 2 * int(not 0 <= quote_age <= maximum_age_seconds)))


class ScoutLocalAccountExecutor:
    def __init__(self, *, repository, profile, datastore, store, broker, local_symbols,
                 session_started_at, session_id, clock, manual_authority, stopped,
                 authority=None, books=None, maximum_evidence_age_seconds=60):
        if authority is not None:
            raise ContractError('Local execution does not accept a remote authority')
        self.repository, self.root = Path(repository), Path(datastore)
        self.profile, self.store, self.broker = profile, store, broker
        self.symbols = validated_symbols(local_symbols)
        self.session_started_at, self.session_id = instant(session_started_at), session_id
        # The live session's default clock returns ISO text; the submission
        # client and broker adapter require an aware datetime on every read.
        self.clock = lambda: instant(clock())
        self.manual_authority, self.stopped = manual_authority, stopped
        if type(maximum_evidence_age_seconds) is not int or not 1 <= maximum_evidence_age_seconds <= 300:
            raise ContractError('Explicit bounded quote/account evidence age required')
        self.maximum_evidence_age_seconds = maximum_evidence_age_seconds
        # Identity is obtained from this trader's existing broker at manual
        # startup. No Gameplan configuration or peer account binding is read.
        self.account_fingerprint, self.books = None, books
        self.books_root = Path(books.root) if books is not None else self.root / 'intraday/scout'
        self.intents, self.journal = IntentionStore(store), None
        self.evidence_paths = dict(books_root=str(self.books_root), inventory=str(self.books_root / 'inventory.json'),
            local_authorizations=str(self.books_root / 'receipts'))

    def _controls(self):
        if self.manual_authority() is not True or self.stopped() is not False:
            raise ContractError('Current manual Scout session authority required')

    def _binding_current(self):
        if (self.account_fingerprint is not None
                and self.broker.stable_account_fingerprint() != self.account_fingerprint):
            raise ContractError('Local broker account changed; manual reload required')
        profile = self.profile if isinstance(self.profile, dict) else json.loads(Path(self.profile).read_text(encoding='utf-8'))
        if (profile.get('actor') != 'Scout' or profile.get('machine') != 'pc-new'
                or set(profile.get('symbols', ())) != set(self.symbols)):
            raise ContractError('Scout operating identity or symbols changed')
        if profile.get('symbol_profile_path'):
            actual = tuple(line.strip() for line in Path(profile['symbol_profile_path']).read_text(encoding='utf-8').splitlines()
                           if line.strip() and not line.lstrip().startswith('#'))
            if len(set(actual)) != len(actual) or set(actual) != set(self.symbols):
                raise ContractError('Scout watchlist changed; manual reload required')

    @contextmanager
    def _account(self, *, starting=False):
        # Serialize only this Scout executor. Gameplan and Atlas trader state,
        # process inventories and locks are not prerequisites for Scout effects.
        with exclusive_runtime_lock(self.books_root / 'execution.lock', process_name='scout-15m-local-execution'):
            if starting:
                if self.stopped() is not False:
                    raise ContractError('Scout startup was stopped')
            else:
                self._controls()
            self._binding_current()
            yield

    def _journals(self):
        if type(self.books) is ScoutBooks:
            yield from self.books.open_sessions()
            return
        for session in self.books.sessions():
            with self.books.open_session(session) as journal:
                yield journal

    def preflight(self):
        # Called only after the operator's explicit live invocation, never by
        # offline preparation. No order/cancel or remote readiness is performed.
        with self._account(starting=True):
            current_identity = self.broker.stable_account_fingerprint()
            if self.account_fingerprint is None:
                self.books = self.books or ScoutBooks(self.books_root, account_fingerprint=current_identity)
                if self.books.account_fingerprint != current_identity:
                    raise ContractError('Scout books belong to another broker account')
                self.account_fingerprint = current_identity
            self.books.inspect()
            for journal in self._journals():
                for record in journal.records():
                    if not record['request']['request_id'].startswith(PREFIX):
                        state = journal.order_state(record['request']['request_id'])
                        if record['state'] != 'NOT_SENT' and (not state['evidence'] or state['evidence']['status'] not in TERMINAL):
                            raise ContractError('Original remote-mode commitment needs its original recovery')
            if current_identity != self.account_fingerprint:
                raise ContractError('Local broker account changed')
            if self.journal is None:
                self.journal = self.books.create_session(self.session_id)
                self.evidence_paths['journal'] = str(self.journal.path)
            proof = dict(mode=MODE, session_id=self.session_id, participant_id='pc-new',
                stream='scout-15m', account_fingerprint=self.account_fingerprint,
                symbols=list(self.symbols), observed_at=instant(self.clock()).isoformat(),
                broker_identity_verified=True, orders_or_commitments_created=False)
            saved = self.books.save_receipt(kind='local-preflight', request_id=self.session_id, payload=proof)
            return dict(mode=MODE, session_id=self.session_id, ready=True, local_proof=proof, local_receipt=saved)

    def _record(self, forecast, disposition, *, evidence=None, capacity=None, order=None,
                require_original_intention=False):
        evidence = dict(mode=MODE, **(evidence or {'reason': disposition}))
        key = digest([disposition, evidence, capacity.payload() if capacity else None, asdict(order) if order else None])
        required = {'require_original_intention': True} if require_original_intention else {}
        with dataset_phase("disposition_persist"):
            return self.intents.record(forecast, event_key=key, recorded_at=self.clock(), disposition=disposition,
                evidence=evidence, kind='execution_disposition', capacity=capacity, order=order, **required)

    def _adapter(self, journal, record, broker=None):
        return ScoutBrokerAdapter(broker or self.broker, journal,
            account_fingerprint=self.account_fingerprint, session_id=record['request']['session_id'],
            symbols=frozenset(self.symbols), authority_epoch=record['grant']['authority_epoch'],
            maximum_evidence_age_seconds=self.maximum_evidence_age_seconds, clock=self.clock)

    def _commitment_clearance(self, journal, record):
        # Each proof retains the original UNKNOWN and permanent send fence.
        # Pre-transport proof is separate from broker-history DAY expiration.
        with recovery_phase('clearance_initial_gate', journal=journal):
            clearance = load_initial_gate_clearance(self.books, journal, record, as_of=self.clock())
        if clearance:
            return clearance
        with recovery_phase('clearance_pretransport', journal=journal):
            clearance = load_pretransport_clearance(self.books, journal, record, as_of=self.clock())
        if clearance:
            return clearance
        with recovery_phase('clearance_commitment', journal=journal):
            clearance = load_clearance(self.books, journal, record, as_of=self.clock())
            if clearance:
                return clearance
            return load_client_gate_clearance(self.books, journal, record, as_of=self.clock())

    def _recover_record(self, journal, record):
        with recovery_phase('journal_record_recovery', journal=journal):
            rid = record['request']['request_id']
            if record['state'] in {'PREPARED', 'GRANTED'}:
                # A prior interrupted pre-send attempt is proven never claimed.
                journal.revoke_unsent(rid, 'local-recovery-before-send')
            elif record['state'] == 'SENDING':
                journal.outcome(rid, record['send_token'], None)
            elif record['state'] == 'ACCEPTED':
                prior = journal.order_state(rid)['evidence']
                if prior is None or prior['status'] not in TERMINAL:
                    with recovery_phase('broker_status', journal=journal):
                        evidence = self._adapter(journal, record).status(rid)
                    journal.observe_order(rid, evidence, as_of=self.clock())
            return journal.get(rid)

    def _recover_record_for_step(self, journal, old):
        """Reuse an audited pair only within this step's recovery scan.

        No snapshot spans broker IO. Keep the original supplied record for the
        adapter, then reread both values after observing its exact response.
        Legacy/custom journals retain the original recovery/read path.
        """
        rid = old['request']['request_id']
        if old['state'] == 'ACCEPTED':
            reader = getattr(journal, 'get_with_order_state', None)
            if callable(reader):
                with recovery_phase('journal_record_recovery', journal=journal):
                    record, lifecycle = reader(rid)
                    prior = lifecycle['evidence']
                    if prior is None or prior['status'] not in TERMINAL:
                        with recovery_phase('broker_status', journal=journal):
                            evidence = self._adapter(journal, old).status(rid)
                        journal.observe_order(rid, evidence, as_of=self.clock())
                        record, lifecycle = reader(rid)
                    return record, lifecycle
        return self._recover_record(journal, old), None

    def _project(self, journal, record, forecast, disposition):
        order = self._order(journal, record)
        lifecycle = journal.order_state(record['request']['request_id'])
        return self._record(forecast, disposition, order=order, require_original_intention=True,
            evidence=dict(request_id=record['request']['request_id'], local_state=record['state'],
                          order_evidence=lifecycle['evidence']))

    @staticmethod
    def _order(journal, record):
        if record['state'] == 'ACCEPTED':
            return journal.lookup_reservation(record['grant']['reservation_id'])
        if record['grant'] is None:
            return None
        req, permit = record['request'], record['grant']
        status = {'SENDING': 'SUBMITTING', 'UNKNOWN': 'UNKNOWN', 'NOT_SENT': 'REJECTED'}.get(record['state'], 'RESERVED')
        return ReservationState(permit['reservation_id'], '', req['symbol'], '15m', req['forecast_id'],
            req['side'], permit['quantity'], req['limit_price'], 0, status, None, req['request_id'], '', None)

    def recover(self):
        with self._account():
            # Recovery needs the original forecasts named by local journals,
            # not every historical prediction. Keep reuse inside this pass;
            # the next poll rereads and validates the durable original bytes.
            forecasts = {}
            results = []
            for journal in self._journals():
                for old in journal.records():
                    if not old['request']['request_id'].startswith(PREFIX):
                        continue
                    self._controls()
                    forecast_id = old['request']['forecast_id']
                    if forecast_id not in forecasts:
                        forecasts[forecast_id] = self.store.forecast_by_id(forecast_id, pc='Scout')
                    forecast = forecasts[forecast_id]
                    # Reject changed original bytes before recovery can revoke
                    # a pending attempt or request broker status for this row.
                    record, _ = self._recover_record_for_step(journal, old)
                    if forecast is None:
                        raise ContractError('Local order lacks its original immutable intention')
                    clearance = (self._commitment_clearance(journal, record)
                                 if record['state'] == 'UNKNOWN' else None)
                    if clearance:
                        # Clearances return without a projection transaction.
                        # Its pure proof reads precede this validation; ordinary
                        # projections validate once inside IntentionStore.record.
                        if self.intents.get(forecast['forecast_id']) is None:
                            raise ContractError('Local order lacks its original immutable intention')
                        # The receipt is durable. Do not append a historical
                        # intention with invented zero fills or acceptance.
                        results.append(dict(disposition=('historical_submission_unknown_no_transport_commitment'
                            if clearance['commitment'] == 'no_order_transport_entered' else
                            'historical_submission_unknown_no_open_commitment'),
                            forecast_id=forecast['forecast_id'], symbol=forecast['symbol'],
                            submission_status='UNKNOWN', submitted_quantity=None, filled_quantity=None,
                            realized_pnl=None, evidence={'commitment_clearance': clearance}))
                    else:
                        results.append(self._project(journal, record, forecast,
                            'submission_recovery_required' if record['state'] == 'UNKNOWN' else 'order_status_reconciled'))
            return results

    def _cancel(self, journal, record, forecast):
        rid, reservation_id = record['request']['request_id'], record['grant']['reservation_id']
        state = journal.order_state(rid)
        outer_clock = self.clock()
        pending = {}
        for item in state['cancellation_events']:
            detail = item['payload']
            if item['kind'] == 'cancellation_acknowledged':
                return
            if item['kind'] == 'cancellation_send_claimed':
                pending[detail['attempt_id']] = detail
            elif item['kind'] == 'cancellation_temporary_failure':
                claim = pending.get(detail.get('attempt_id'))
                expected = dict(schema_version=_CANCELLATION_NOT_SENT, stream='scout-15m',
                    reservation_id=reservation_id, broker_order_id=record['broker_order_id'],
                    symbol=record['request']['symbol'], request_sha256=canonical_sha256(record['request']),
                    native_method='SchwabSession.cancel_prepared_order', stage='native-before-delete',
                    no_delete_entered=True, disposition='fixed_target_already_elapsed')
                try:
                    proven = (claim is not None and set(detail) == set(expected) | {
                        'attempt_id', 'claim_sha256', 'forecast_id', 'cycle_id', 'target_close', 'observed_at'}
                        and all(type(detail[key]) is type(value) and detail[key] == value
                                for key, value in expected.items())
                        and detail['claim_sha256'] == canonical_sha256(claim)
                        and type(detail['forecast_id']) is str and len(detail['forecast_id']) == 64
                        and all(char in '0123456789abcdef' for char in detail['forecast_id'])
                        and detail['forecast_id'] != forecast['forecast_id']
                        and type(detail['cycle_id']) is str
                        and instant(detail['observed_at']) >= instant(detail['target_close'])
                        and instant(claim['observed_at']) <= instant(detail['observed_at']) <= outer_clock)
                    if proven:
                        original = self.store.forecast(detail['cycle_id'], detail['symbol'])
                        proven = (original is not None and original['pc'] == 'Scout'
                            and original['symbol'] == detail['symbol'] and original['forecast_id'] == detail['forecast_id']
                            and original['target_close'] == detail['target_close'])
                except (ContractError, TypeError, ValueError):
                    proven = False
                if proven:
                    del pending[detail['attempt_id']]
        if pending:
            return  # Unknown or acknowledged DELETE is never blindly repeated.
        journal.reserve_stream_cancellation(reservation_id, stream='scout-15m', reason='new-forecast',
            as_of=self.clock(), evidence_id='local-replace:' + forecast['forecast_id'])
        expected = self.broker.prepare_order_submission().identity_fingerprint
        attempt = uuid.uuid4().hex
        outer, source, claimed = self, self.broker, [None]
        latest_evidence = [None]

        def current(evidence):
            outer._controls()
            now = outer.clock()
            disposition = forecast_disposition(forecast, now=now, session_started_at=outer.session_started_at,
                local_symbols=outer.symbols)
            if disposition != 'eligible_for_account_checks':
                raise _CancellationForecastIneligible(disposition)
            if not 0 <= (now - instant(evidence.observed_at)).total_seconds() <= outer.maximum_evidence_age_seconds:
                raise ContractError('Exact order status became stale before DELETE')

        def gate(request, permit, evidence):
            outer._controls()
            outer._binding_current()
            _saved_forecast(outer.store.path, forecast, session_id=outer.session_id, session_started=outer.session_started_at)
            current(evidence)
            journal.observe_order(rid, evidence, as_of=outer.clock())
            current_state = journal.order_state(rid)
            marker = current_state['cancellation']
            if (marker is None or marker['broker_order_id'] != record['broker_order_id']
                    or marker['reservation_id'] != reservation_id or request.payload() != record['request']
                    or permit.payload() != record['grant']):
                raise ContractError('Exact local cancellation intent required')
            latest_evidence[0] = evidence
            current(evidence)
            return True

        class CancellationBroker:
            def __getattr__(self, name):
                return getattr(source, name)

            def cancel_prepared_order(self, order_id, context, *, before_delete):
                sender = source.cancel_prepared_order
                native = (isinstance(source, SchwabSession)
                    and getattr(sender, '__self__', None) is source
                    and getattr(sender, '__func__', None) is _NATIVE_PREPARED_DELETE)
                rejected = [None]
                def fenced():
                    try:
                        before_delete()
                        if claimed[0] is None:
                            claim = dict(reservation_id=reservation_id, attempt_id=attempt,
                                observed_at=instant(outer.clock()).isoformat(), stream='scout-15m')
                            journal.record_cancellation_event(attempt, 'cancellation_send_claimed', claim)
                            claimed[0] = claim
                        # The native callback must finish with a fresh clock,
                        # after all journal I/O, including its durable send fence.
                        current(latest_evidence[0])
                    except _CancellationForecastIneligible as error:
                        rejected[0] = error
                        raise
                try:
                    return sender(order_id, context, before_delete=fenced)
                except _CancellationForecastIneligible as error:
                    if (native and rejected[0] is error and claimed[0] is not None
                            and error.disposition == 'fixed_target_already_elapsed'):
                        # Only this exact native function proves DELETE was not
                        # entered. Keep the claim and append its rejection proof;
                        # a different eligible forecast may make a new attempt.
                        journal.record_cancellation_event(attempt + ':native-not-sent',
                            'cancellation_temporary_failure', dict(schema_version=_CANCELLATION_NOT_SENT,
                                reservation_id=reservation_id, broker_order_id=order_id,
                                symbol=record['request']['symbol'], request_sha256=canonical_sha256(record['request']),
                                attempt_id=attempt, claim_sha256=canonical_sha256(claimed[0]),
                                forecast_id=forecast['forecast_id'], cycle_id=forecast['cycle_id'],
                                target_close=forecast['target_close'],
                                observed_at=instant(outer.clock()).isoformat(), stream='scout-15m',
                                native_method='SchwabSession.cancel_prepared_order', stage='native-before-delete',
                                no_delete_entered=True, disposition=error.disposition))
                    raise

        try:
            result = self._adapter(journal, record, CancellationBroker()).cancel(rid,
                expected_snapshot_identity=expected, authorization_gate=gate, final_gate=gate)
        except _CancellationForecastIneligible as exc:
            return exc.disposition
        journal.observe_order(rid, result.evidence, as_of=self.clock())
        if result.delete_acknowledged:
            journal.record_cancellation_event(attempt + ':ack', 'cancellation_acknowledged', dict(
                reservation_id=reservation_id, observed_at=instant(self.clock()).isoformat(),
                stream='scout-15m', acknowledgment_is_terminal=False))

    def _capture(self):
        self._controls()
        if self.broker.stable_account_fingerprint() != self.account_fingerprint:
            raise ContractError('Local broker account changed')
        portfolio = capture_portfolio_state(self.broker, observed_at=self.clock(), parallel=True,
            literal_cash_only=True, use_actual_quote_timestamps=True, symbols=self.symbols, include_order_identities=True)
        self.broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
        self._controls()
        if not 0 <= (instant(self.clock()) - instant(portfolio.observed_at)).total_seconds() <= self.maximum_evidence_age_seconds:
            raise ContractError('Local account snapshot is stale')
        return portfolio

    def _record_execution_profile(self, forecast, profile):
        # Diagnostics never hold the account lock or wait for an application
        # writer. Missing/busy evidence storage leaves execution unchanged.
        pending_error = sys.exception()
        db = None
        try:
            db = sqlite3.connect(self.store.path.resolve().as_uri() + "?mode=rw",
                                 uri=True, timeout=0)
            with db:
                db.execute("INSERT INTO events(cycle_id,symbol,recorded_at,kind,payload) VALUES(?,?,?,?,?)",
                    (self.session_id, forecast["symbol"], instant(utcnow()).isoformat(),
                     "local_execution_profile", encoded(profile)))
        except BaseException as error:
            if not isinstance(error, Exception) and pending_error is None:
                raise
        finally:
            if db is not None:
                try:
                    db.close()
                except BaseException as error:
                    if not isinstance(error, Exception) and pending_error is None:
                        raise

    def step(self, forecast):
        profile = None
        try:
            with capture_dataset_build() as profile:
                profile["schema_version"] = "intraday-local-execution-profile-v1"
                try:
                    profile.update(forecast_id=forecast.get("forecast_id"), symbol=forecast.get("symbol"),
                        manual_session_id=self.session_id, target_close=forecast.get("target_close"))
                except Exception as error:
                    profile["observer_errors"].append(type(error).__name__[:96])
                with dataset_phase("total_step"):
                    with self._account():
                        return self._step(forecast)
        finally:
            if profile is not None:
                self._record_execution_profile(forecast, profile)

    def _step(self, forecast):
        if self.journal is None:
            raise ContractError('Local executor has not passed manual preflight')
        disposition = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at, local_symbols=self.symbols)
        if disposition != 'eligible_for_account_checks':
            return self._record(forecast, disposition)
        with dataset_phase("snapshot_validation"):
            _saved_forecast(self.store.path, forecast, session_id=self.session_id, session_started=self.session_started_at)
        with dataset_phase("chronology_check"):
            if self.intents.processed_at_or_after(forecast):
                return self._record(forecast, 'newer_or_same_reference_intention_already_processed')
        with dataset_phase("initial_capture"):
            portfolio = self._capture()
        disposition = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at,
            local_symbols=self.symbols)
        if disposition != 'eligible_for_account_checks':
            return self._record(forecast, disposition)
        # Quotes can arrive while the account capture is in progress. Their
        # availability is capture completion; keep the original account time.
        quote_available_at = instant(self.clock()).isoformat()
        with dataset_phase("intention_lookup"):
            original = self.intents.get(forecast['forecast_id'])
        if original is None:
            with dataset_phase("snapshot_load"):
                frozen = self.store.inputs(forecast['cycle_id'], forecast['symbol'])
                if frozen is None or frozen[0] != forecast['snapshot_hash']:
                    raise ContractError('Original forecast inputs unavailable')
            quote = portfolio.quotes[forecast['symbol']]
            with dataset_phase("sizing"):
                result = size_forecast(forecast, forecast_snapshot=frozen[1],
                    account_equity=AvailableInput(portfolio.account_equity, portfolio.observed_at,
                        'schwab-liquidation-equity', dict(account_fingerprint=self.account_fingerprint,
                            snapshot_identity=portfolio.broker_identity_fingerprint,
                            snapshot_sha256=portfolio.source_fingerprint, observed_at=portfolio.observed_at)),
                    quote=QuoteObservation(quote.bid, quote.ask, quote.freshness_observed_at, quote_available_at,
                        'schwab-current-quote', {'quote': asdict(quote)}), calculated_at=self.clock(),
                    maximum_quote_age_seconds=self.maximum_evidence_age_seconds)
            with dataset_phase("save_sizing"):
                original = self.intents.save_sizing(forecast, result)
            if result.status != 'sized':
                return original
        # Preserve the full intention even when an old order or zero cash means
        # this forecast cannot currently produce an effect.
        disposition = forecast_disposition(forecast, now=self.clock(), session_started_at=self.session_started_at,
            local_symbols=self.symbols)
        if disposition != 'eligible_for_account_checks':
            return self._record(forecast, disposition)
        outstanding = []
        with dataset_phase("own_journal_recovery"):
            for journal in self._journals():
                for old in journal.records():
                    if not old['request']['request_id'].startswith(PREFIX):
                        continue
                    record, paired_lifecycle = self._recover_record_for_step(journal, old)
                    if record['request']['forecast_id'] == forecast['forecast_id']:
                        return self._project(journal, record, forecast, 'recover_existing_intention')
                    if record['state'] == 'UNKNOWN':
                        # The original send stays UNKNOWN and can never be replayed.
                        # Reviewed exact pre-transport proof or complete post-expiry
                        # absence can remove only the block on distinct new requests.
                        if self._commitment_clearance(journal, record) is not None:
                            continue
                        return self._record(forecast, 'submission_recovery_required',
                            evidence={'unresolved_request_id': record['request']['request_id'], 'retry_after_seconds': 3})
                    if record['state'] == 'ACCEPTED':
                        lifecycle = (paired_lifecycle if paired_lifecycle is not None else
                                     journal.order_state(record['request']['request_id']))
                        state = lifecycle['evidence']
                        if state['status'] in TERMINAL:
                            continue
                        if record['request']['symbol'] == forecast['symbol']:
                            disposition = forecast_disposition(forecast, now=self.clock(),
                                session_started_at=self.session_started_at, local_symbols=self.symbols)
                            if disposition != 'eligible_for_account_checks':
                                return self._record(forecast, disposition)
                            cancellation_disposition = self._cancel(journal, record, forecast)
                            if cancellation_disposition is not None:
                                return self._record(forecast, cancellation_disposition)
                            return self._record(forecast, 'previous_intention_reconciliation',
                                evidence={'request_id': record['request']['request_id'], 'retry_after_seconds': 3})
                        outstanding.append((record, state))
        # Recovery/cancellation may have taken time and fills may have changed
        # capacity; obtain fresh account evidence after clearance.
        with dataset_phase("second_capture"):
            portfolio = self._capture()
        visible = {str(order['order_id']): order for order in portfolio.broker_working_orders or ()}
        for record, state in outstanding:
            pending = visible.get(record['broker_order_id'])
            if (pending is None or pending['symbol'] != record['request']['symbol']
                    or pending['instruction'] != record['request']['side']
                    or Decimal(str(pending['remaining_quantity'])) < state['remaining_quantity']):
                return self._record(forecast, 'broker_capacity_reconciliation_required',
                    evidence={'request_id': record['request']['request_id'], 'retry_after_seconds': 3,
                              'proven_unsent_retry_eligible': True})
        with dataset_phase("capacity"):
            quote = portfolio.quotes[forecast['symbol']]
            price = Decimal(str(quote.ask if original['side'] == 'BUY' else quote.bid)).quantize(Decimal('.01'),
                rounding=ROUND_CEILING if original['side'] == 'BUY' else ROUND_FLOOR)
            capacity = clip_to_capacity(symbol=forecast['symbol'], side=original['side'],
                desired_quantity=original['desired_quantity'], limit_price=format(price.normalize(), 'f'),
                available_cash=portfolio.available_cash, available_shares=int(Decimal(str(portfolio.available_sell_shares(forecast['symbol']))).to_integral_value(rounding=ROUND_FLOOR)),
                capacity_evidence_id=portfolio.source_fingerprint, observed_at=portfolio.observed_at, calculated_at=self.clock())
        # Account evidence, sizing and own-order recovery can cross the fixed
        # target. Classify that boundary before creating a new attempt, using
        # the same instant for classification and its immutable DTO issuance.
        current = instant(self.clock())
        disposition = forecast_disposition(forecast, now=current, session_started_at=self.session_started_at,
            local_symbols=self.symbols)
        if disposition != 'eligible_for_account_checks':
            return self._record(forecast, disposition, capacity=capacity)
        if capacity.adjusted_quantity == 0:
            return self._record(forecast, capacity.disposition, capacity=capacity)
        now = current.isoformat()
        request = Request(PREFIX + forecast['forecast_id'], self.account_fingerprint, self.session_id,
            forecast['symbol'], original['side'], original['desired_quantity'], capacity.limit_price,
            forecast['forecast_id'], forecast['forecast_id'], forecast['snapshot_hash'], original['sizing_id'],
            forecast['reference_at'], now, forecast['target_close'], forecast['target_close'])
        permit = Grant('local-permit:' + forecast['forecast_id'], request.request_id, request.sha256,
            self.account_fingerprint, self.session_id, 1, capacity.adjusted_quantity,
            'local-order:' + forecast['forecast_id'], now, request.valid_until)
        with dataset_phase("permit_prepare"):
            self.journal.prepare(request)
            self.journal.grant(permit)
        authorization = dict(mode=MODE, request=request.payload(), permit=permit.payload(), capacity=capacity.payload(),
            snapshot_identity=portfolio.broker_identity_fingerprint, snapshot_sha256=portfolio.source_fingerprint,
            observed_at=portfolio.observed_at, manual_session_id=self.session_id)
        with dataset_phase("permit_persist"):
            saved = self.books.save_receipt(kind='local-account-permit', request_id=request.request_id, payload=authorization)

        def local_authorization(req, granted):
            self._controls()
            self._binding_current()
            value = json.loads(Path(saved['path']).read_text(encoding='utf-8'))
            if (canonical_sha256(value) != saved['sha256'] or value['kind'] != 'local-account-permit'
                    or value['payload'] != authorization or req != request or granted != permit
                    or self.intents.get(forecast['forecast_id']) != original):
                raise ContractError('Exact saved local capacity permission changed')
            checked = clip_to_capacity(**{key: capacity.payload()[key] for key in
                ('symbol', 'side', 'desired_quantity', 'limit_price', 'available_cash', 'available_shares',
                 'capacity_evidence_id', 'observed_at', 'calculated_at')})
            if checked != capacity or granted.quantity != capacity.adjusted_quantity:
                raise ContractError('Local permitted quantity differs from account capacity')
            with dataset_phase("authorization_validation"):
                _saved_forecast(self.store.path, forecast, session_id=self.session_id, session_started=self.session_started_at)
            # Integrity validation can wait briefly for SQLite recovery. Check
            # current freshness and eligibility after it, before any send gate.
            current = instant(self.clock())
            if any(not 0 <= (current - instant(at)).total_seconds() <= self.maximum_evidence_age_seconds
                   for at in (portfolio.observed_at, quote.freshness_observed_at)):
                error = ContractError('Current local quote/account evidence expired')
                try:
                    _record_local_evidence_freshness_failure(error, current=current,
                        account_at=portfolio.observed_at, quote_at=quote.freshness_observed_at,
                        request_issued_at=request.issued_at,
                        maximum_age_seconds=self.maximum_evidence_age_seconds)
                except BaseException:
                    pass  # Optional diagnostics must preserve the original gate rejection.
                raise error
            if forecast_disposition(forecast, now=current, session_started_at=self.session_started_at,
                    local_symbols=self.symbols) != 'eligible_for_account_checks':
                raise ContractError('Local forecast eligibility expired')
            self._controls()
            return True

        client = ScoutClient(self.journal, None, account_fingerprint=self.account_fingerprint,
            session_id=self.session_id, symbols=frozenset(self.symbols), authority_epoch=1, clock=self.clock)
        adapter = None
        def send(req, granted):
            nonlocal adapter
            adapter = self._adapter(self.journal, self.journal.get(req.request_id))
            return adapter.send(req, granted,
                expected_snapshot_identity=portfolio.broker_identity_fingerprint,
                authorization_gate=local_authorization, final_gate=local_authorization)
        try:
            with dataset_phase("submission"):
                client.submit(request.request_id, final_gate=local_authorization, send=send)
            return self._record(forecast, 'submitted', capacity=capacity,
                order=self.journal.lookup_reservation(permit.reservation_id), evidence={'request_id': request.request_id})
        except Exception as exc:
            record = self.journal.get(request.request_id)
            clearance, clearance_failure = None, None
            if (adapter is not None and record['state'] == 'UNKNOWN'
                    and (adapter.initial_gate_failure is not None or adapter.native_before_post_failure is not None)):
                try:
                    clearance = create_initial_gate_clearance(self.books, self.journal, record,
                        adapter=adapter, error=exc, as_of=self.clock())
                except Exception as persistence_error:
                    # Preserve the actual submission failure if proof storage
                    # fails. An absent/invalid proof keeps the original block.
                    clearance_failure = exception_details(persistence_error)
            if (adapter is None and record['state'] == 'UNKNOWN'
                    and type(exc) is sqlite3.OperationalError
                    and type(getattr(exc, 'sqlite_errorcode', None)) is int
                    and exc.sqlite_errorcode >= 0
                    and exc.sqlite_errorcode & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    and client.final_gate_failure is not None):
                try:
                    clearance = create_client_gate_clearance(self.books, self.journal, record,
                        executor=self, client=client, gate=local_authorization, send=send,
                        error=exc, as_of=self.clock())
                except Exception as persistence_error:
                    clearance_failure = exception_details(persistence_error)
            return self._record(forecast, 'submission_recovery_required', capacity=capacity,
                order=self._order(self.journal, record),
                evidence={'request_id': request.request_id, 'local_state': record['state'],
                          'error_type': type(exc).__name__, 'retry_after_seconds': retry_delay(exc, self.clock()),
                          **({'pretransport_clearance': clearance} if clearance is not None else {}),
                          **({'clearance_persistence_failure': clearance_failure} if clearance_failure is not None else {}),
                          'submission_failure': getattr(exc, 'scout_submission_failure',
                              dict(stage='outside_claimed_submit', original=exception_details(exc)))})

# Retain the installed local closure implementations. Arbitrary client callbacks
# and wrappers cannot qualify for a no-transport receipt using exception text.
_LOCAL_AUTHORIZATION_CODE = next(code for code in ScoutLocalAccountExecutor._step.__code__.co_consts
                                 if getattr(code, 'co_name', None) == 'local_authorization')
_LOCAL_SEND_CODE = next(code for code in ScoutLocalAccountExecutor._step.__code__.co_consts
                        if getattr(code, 'co_name', None) == 'send')
