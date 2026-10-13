"""Scout's manually owned effects, using Atlas's one shared reservation pool.

The local inventory retains every original manual session. Recovery never sends
an old intention again, and exact terminal broker evidence is delivered to the
shared authority before a replacement can request capacity. No Atlas database
or coordinator identity is created on Scout.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
import json
import sqlite3
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.stock_trader.horizon_ledger import FillEvidence, OrderEvidence, ReservationState
from ml.stock_trader.session import stock_execution_window
from ml.stock_trader.state import capture_portfolio_state

from .cancellation import CancellationService, TERMINAL, retry_delay
from .capacity import CapacityDecision, clip_to_capacity
from .execution_policy import submission_gate
from .intentions import IntentionStore
from .prediction import ContractError, PACIFIC, TARGET_VERSION, digest, instant
from .scout_books import ScoutBooks
from .scout_broker import ScoutBrokerAdapter
from .scout_client import ScoutClient
from .scout_journal import _safe_path
from .scout_protocol import Grant, Request, canonical_sha256
from .scout_runtime_gate import local_gate
from .scout_prestart import inspect_local_account_writers
from .starter_sizing import QuoteObservation, size_forecast
from .technical_sizing import AvailableInput


def forecast_disposition(forecast, *, now, session_started_at, local_symbols):
    original = dict(forecast)
    key = original.pop('forecast_id', None)
    if key != digest(original) or original['target_version'] != TARGET_VERSION:
        raise ContractError('Immutable exact-target forecast required')
    if forecast['pc'] != 'Scout' or forecast['symbol'] not in local_symbols:
        return 'outside_local_scout_scope'
    if forecast['mode'] != 'live' or forecast['availability_basis'] != 'observed':
        return 'not_a_current_live_forecast'
    if instant(forecast['issued_at']) < instant(session_started_at):
        return 'forecast_precedes_manual_start'
    if instant(forecast['issued_at']) > instant(now):
        raise ContractError('Forecast issue lies in the future')
    if forecast['ensemble_status'] != 'full' or forecast['successful_count'] != forecast['configured_count']:
        return 'incomplete_ensemble'
    gate = submission_gate(as_of=now, eligible_session=instant(forecast['reference_at']).astimezone(PACIFIC).date())
    if gate.state != 'before_submission_cutoff':
        return gate.state
    if instant(now) >= instant(forecast['target_close']):
        return 'fixed_target_already_elapsed'
    return 'eligible_for_account_checks'


def native_evidence(payload):
    return OrderEvidence(**{**payload, 'fills': tuple(FillEvidence(**f) for f in payload['fills'])})


class ScoutAccountExecutor:
    def __init__(self, *, repository, profile, datastore, store, broker, local_symbols,
                 session_started_at, session_id, clock, manual_authority, stopped,
                 authority=None, books=None, maximum_evidence_age_seconds=60):
        self.repository, self.root = Path(repository), Path(datastore)
        self.profile, self.store, self.broker = profile, store, broker
        self.symbols = tuple(local_symbols)
        self.session_started_at, self.session_id = instant(session_started_at), session_id
        self.clock, self.manual_authority, self.stopped = clock, manual_authority, stopped
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ContractError('Exact nonempty Scout symbols required')
        if type(maximum_evidence_age_seconds) is not int or not 1 <= maximum_evidence_age_seconds <= 300:
            raise ContractError('Existing native quote-evidence age range required')
        self.maximum_evidence_age_seconds = maximum_evidence_age_seconds
        self.binding_path = self.root / 'intraday/scout/authority-binding.json'
        self._production_binding = authority is None
        if authority is None:
            from .scout_authority import ScoutAuthorityClient, load_binding
            binding = load_binding(self.binding_path)
            books = books or ScoutBooks(self.root / 'intraday/scout', account_fingerprint=binding.account_fingerprint)
            authority = ScoutAuthorityClient(binding, books, clock=clock)
        self.authority = authority
        self.binding = authority.binding
        if set(self.binding.symbols) != set(self.symbols):
            raise ContractError('Authenticated participant symbol partition differs from Scout')
        self.books = books or ScoutBooks(self.root / 'intraday/scout', account_fingerprint=self.binding.account_fingerprint)
        if self.books.account_fingerprint != self.binding.account_fingerprint:
            raise ContractError('Private Scout books belong to another account')
        self.intents = IntentionStore(store)
        self.journal = None
        self._ready = None
        self._ready_at = None
        self._clearance = None
        self._settled = {}
        self.evidence_paths = dict(books_root=str(self.books.root), inventory=str(self.books.path),
            authority_receipts=str(self.books.root / 'receipts'), authority_binding=str(self.binding_path))

    def _controls(self):
        if self.manual_authority() is not True or self.stopped() is not False:
            raise ContractError('Current manual Scout session authority required')

    @contextmanager
    def _account(self, *, require_manual=True):
        # Exclude supported local account writers from the absence proof through
        # the broker effect. Atlas serialization still owns the shared pool.
        with exclusive_runtime_lock(self.root / 'locks/stock-trader-hourly.lock', process_name='stock-trader-hourly'):
            if require_manual:
                self._controls()
            elif self.stopped() is not False:
                raise ContractError('Scout startup was stopped')
            proof = inspect_local_account_writers(self.root,self.repository,as_of=self.clock())
            if proof.get('ready') is not True or proof.get('mode') != 'verified_gameplan_absent':
                raise ContractError('Scout local account-writer absence is not verified')
            yield

    def _binding_current(self):
        if self._production_binding:
            from .scout_authority import load_binding
            if load_binding(self.binding_path) != self.binding:
                raise ContractError('Scout authority binding changed; manual reload required')
            profile = json.loads(Path(self.profile).read_text(encoding='utf-8'))
            if (profile.get('actor') != 'Scout' or profile.get('machine') != 'pc-new'
                    or set(profile.get('symbols',())) != set(self.symbols)):
                raise ContractError('Scout operating identity or symbol membership changed')
            watch = Path(profile['symbol_profile_path'])
            symbols = tuple(line.strip() for line in watch.read_text(encoding='utf-8').splitlines()
                            if line.strip() and not line.lstrip().startswith('#'))
            if len(symbols) != len(set(symbols)) or set(symbols) != set(self.symbols):
                raise ContractError('Scout local watchlist changed; manual reload required')
        return True

    def _compatibility(self):
        # This call is an authenticated noncommitting service check. Capability
        # discovery alone cannot assert that the actual writers are compatible.
        self._binding_current()
        proof = self.authority.readiness()
        if (not isinstance(proof, dict) or proof.get('ready') is not True
                or proof.get('authenticated') is not True or proof.get('protocol_compatible') is not True
                or proof.get('participant_id') != 'pc-new' or proof.get('stream') != 'scout-15m'
                or proof.get('account_fingerprint') != self.binding.account_fingerprint):
            raise ContractError('Authenticated compatible account writers are not ready')
        self._ready, self._ready_at = proof, instant(self.clock())
        return proof

    def preflight(self):
        # STARTING has no order authority. Only step/recover require RUNNING;
        # this check performs identity/readiness and local book registration.
        with self._account(require_manual=False):
            return self._preflight()

    def _preflight(self):
        self.books.inspect()
        proof = self._compatibility()
        # Audit every original journal before registering this new session.
        for session in self.books.sessions():
            with self.books.open_session(session):
                pass
        if self.journal is None:
            self.journal = self.books.create_session(self.session_id)
            self.evidence_paths['journal'] = str(self.journal.path)
        return {**proof, 'session_id': self.session_id}

    def _record(self, forecast, disposition, *, evidence=None, kind='execution_disposition', capacity=None, order=None):
        evidence = evidence or {'reason': disposition}
        event_key = digest([kind, disposition, evidence, capacity.payload() if capacity else None,
                            asdict(order) if order else None])
        return self.intents.record(forecast, event_key=event_key, recorded_at=self.clock(),
            disposition=disposition, evidence=evidence, kind=kind, capacity=capacity, order=order)

    def _journals(self):
        # The manual-session host lock excludes a second executor; these are
        # short-lived handles, not another authority or a copied database.
        for session in self.books.sessions():
            with self.books.open_session(session) as journal:
                yield journal

    def _adapter(self, journal, record):
        grant = Grant.from_payload(record['grant'])
        return ScoutBrokerAdapter(self.broker, journal,
            account_fingerprint=self.binding.account_fingerprint, session_id=record['request']['session_id'],
            symbols=frozenset(self.symbols), authority_epoch=grant.authority_epoch,
            maximum_evidence_age_seconds=self.maximum_evidence_age_seconds, clock=self.clock)

    def _accepted_event(self, journal, record):
        for event in journal.pending_events():
            if event['event']['request_id'] == record['request']['request_id'] and event['event']['kind'] == 'ACCEPTED':
                self.authority.accepted(record, event_id=event['event_id'])
                journal.acknowledge(event['event_id'], event['event_sha256'])

    @staticmethod
    def _order(journal, record, receipt=None):
        if record['grant'] is None:
            return None
        if record['state'] == 'ACCEPTED':
            return journal.lookup_reservation(record['grant']['reservation_id'])
        request, grant = record['request'], record['grant']
        # An ambiguous local attempt has an unknown submitted quantity. Empty
        # allocation identities explicitly avoid inventing native lot ownership.
        status = {'SENDING':'SUBMITTING','UNKNOWN':'UNKNOWN','NOT_SENT':'REJECTED'}.get(record['state'],'RESERVED')
        if (receipt is not None and receipt.response.get('status') in {'NOT_SUBMITTED','EXPIRED','DECLINED'}
                and receipt.response.get('claim') is None and not receipt.response.get('broker_order_id')
                and not receipt.response.get('filled_quantity')):
            status = 'REJECTED'  # Authority-proven unsubmitted reservation; preserve the local send fence.
        return ReservationState(grant['reservation_id'],'',request['symbol'],'15m',request['forecast_id'],
            request['side'],grant['quantity'],request['limit_price'],0,status,None,request['request_id'],'',None)

    def _apply_order(self, journal, record, evidence):
        journal.observe_order(record['request']['request_id'], evidence, as_of=self.clock())
        # Lost reply leaves the same exact evidence in the local lifecycle. The
        # next recovery publishes it before asking for a newer broker snapshot.
        receipt = self.authority.order_status(record, evidence)
        return receipt

    def _recover_record(self, journal, record):
        key = (journal.journal_id,record['request']['request_id'])
        def fingerprint(current):
            return canonical_sha256({'record':current,'order_state':journal.order_state(current['request']['request_id'])})
        cached = self._settled.get(key)
        if cached is not None and cached[0] == fingerprint(record):
            return cached[1]
        receipt = self._recover_uncached(journal,record)
        current = journal.get(record['request']['request_id'])
        if self._released(journal,current,receipt):
            # Only exact shared terminal/release evidence is cached. Every new
            # manual process rebuilds this cache, and every hit audits unchanged
            # local source evidence. Unresolved sends/DELETE acknowledgments
            # cannot enter it. Stable receipts also deduplicate UI projections.
            self._settled[key] = (fingerprint(current),receipt)
        return receipt

    def _recover_uncached(self, journal, record):
        request = Request.from_payload(record['request'])
        if record['state'] == 'PREPARED' or record['state'] == 'NOT_SENT' and record['grant'] is None:
            # The exact original request repairs a reply lost after Atlas paired
            # its books. Status alone cannot repair an interrupted admission.
            decision = self.authority.reserve(request)
            if decision.grant is not None:
                record = journal.grant(decision.grant)
            elif decision.response['status'] in {'DECLINED','EXPIRED','NOT_SUBMITTED'}:
                if decision.response.get('reason') == 'expired_before_admission':
                    from .scout_authority import validate_negative_admission_receipt
                    validate_negative_admission_receipt(record,decision,self.binding,now=self.clock())
                if record['state'] != 'NOT_SENT':
                    journal.revoke_unsent(request.request_id,'authority-negative-admission')
                return decision
            else:
                raise ContractError('Original admission remains unresolved; no replacement is authorized')
        if record['state'] in {'GRANTED', 'NOT_SENT'}:
            # Recovery never adopts an old grant for POST. Revoke the local
            # attempt first, then obtain the authority's atomic no-claim proof.
            if record['state'] != 'NOT_SENT':
                record = journal.revoke_unsent(request.request_id, 'recovery-before-send')
            return self.authority.release_unsent(record)
        if record['state'] == 'SENDING':
            # Only the single serialized executor calls recovery, after the
            # original sender has returned or after its process has ended. This
            # durable transition also makes the broker's final SENDING gate fail.
            record = journal.outcome(request.request_id, record['send_token'], None)
        status = self.authority.status(record)
        body = status.response
        if body['status'] in {'NOT_SUBMITTED','EXPIRED'} and body.get('claim') is None:
            return status
        if body.get('broker_order_id') and record['broker_order_id'] is None:
            # The wrapper binds the service's exact order and original claim.
            record = journal.outcome(request.request_id, record['send_token'], body['broker_order_id'])
        if record['state'] != 'ACCEPTED':
            # A locally consumed attempt is never rearmed by status, including
            # when the authorization reply was lost before or after commitment.
            if body.get('claim') is None:
                return self.authority.release_unsent(record, status_receipt=status)
            for event in journal.pending_events():
                if event['event']['request_id'] == request.request_id and event['event']['kind'] == 'UNKNOWN':
                    self.authority.unknown(record,event_id=event['event_id'])
                    journal.acknowledge(event['event_id'],event['event_sha256'])
            return status
        self._accepted_event(journal, record)
        old = journal.order_state(request.request_id)['evidence']
        if old is not None:
            receipt = self.authority.order_status(record, native_evidence(old))
            if old['status'] in TERMINAL:
                return receipt
        evidence = self._adapter(journal, record).status(request.request_id)
        return self._apply_order(journal, record, evidence)

    def _released(self, journal, record, receipt):
        body = receipt.response
        if record['broker_order_id'] is None and body.get('status') in {'NOT_SUBMITTED','EXPIRED','DECLINED'}:
            return body.get('claim') is None and not body.get('filled_quantity')
        state = journal.order_state(record['request']['request_id'])
        evidence = state['evidence']
        return (evidence is not None and evidence['status'] in TERMINAL
            and body.get('status') == evidence['status']
            and body.get('broker_order_id') == record['broker_order_id']
            and body.get('filled_quantity') == evidence['cumulative_filled_quantity'])

    def recover(self):
        with self._account():
            return self._recover()

    def _recover(self):
        self._controls()
        self._compatibility()
        forecasts = {f['forecast_id']: f for f in self.store.forecasts(pc='Scout')}
        results = []
        for journal in self._journals():
            for record in journal.records():
                self._controls()
                forecast = forecasts.get(record['request']['forecast_id'])
                if (record['state'] in {'PREPARED','GRANTED'}
                        and record['request']['session_id'] == self.session_id and forecast is not None
                        and self._latest(forecast)
                        and forecast_disposition(forecast,now=self.clock(),session_started_at=self.session_started_at,
                            local_symbols=self.symbols) == 'eligible_for_account_checks'):
                    # The durable execution queue still owns an unconsumed
                    # current attempt. Its next step retries exact admission.
                    continue
                receipt = self._recover_record(journal, record)
                current = journal.get(record['request']['request_id'])
                if forecast is None or self.intents.get(forecast['forecast_id']) is None:
                    raise ContractError('Private order lacks its original immutable intention')
                order = self._order(journal,current,receipt)
                negative = self._released(journal,current,receipt) and current['broker_order_id'] is None
                results.append(self._record(forecast, 'not_submitted' if negative else
                    'order_status_reconciled' if order and order.broker_order_id else 'submission_recovery_required',
                    order=order, evidence={'request_id':record['request']['request_id'],
                        'local_state':current['state'], 'authority_status':receipt.response['status'],
                        'authority_receipt':receipt.sha256}))
        return results

    def _latest(self, forecast):
        return not any(f['symbol'] == forecast['symbol'] and f['mode'] == 'live'
            and f['ensemble_status'] == 'full' and instant(f['issued_at']) >= self.session_started_at
            and (instant(f['reference_at']), instant(f['issued_at'])) >
                (instant(forecast['reference_at']), instant(forecast['issued_at']))
            for f in self.store.forecasts(pc='Scout'))

    def _cancel_previous(self, journal, record, forecast):
        rid = record['request']['request_id']
        reservation_id = record['grant']['reservation_id']
        authorization = [None]
        known_before_delete = [False]
        delete_entered = [False]
        delete_preparing = [False]
        source_broker = self.broker
        class CancellationBrokerView:
            def __getattr__(self, name):
                return getattr(source_broker,name)

            def prepare_order_submission(self):
                delete_preparing[0] = True
                return source_broker.prepare_order_submission()

            def cancel_prepared_order(self, order_id, context, *, before_delete):
                def complete_gate():
                    try:
                        before_delete()
                    except BaseException:
                        known_before_delete[0] = True
                        raise
                event('cancellation_send_claimed',dict(reservation_id=reservation_id,
                    attempt_id=uuid.uuid4().hex,observed_at=instant(self_outer.clock()).isoformat(),stream='scout-15m'))
                delete_entered[0] = True
                return source_broker.cancel_prepared_order(order_id,context,before_delete=complete_gate)
        self_outer = self
        def check_cancel(context, reservation):
            self._controls()
            if not self._latest(forecast):
                raise ContractError('Cancellation trigger superseded by a newer forecast')
            marker = journal.order_state(rid)['cancellation']
            if marker is None:
                raise ContractError('Durable exact cancellation intent required')
            if authorization[0] is None:
                authorization[0] = self.authority.cancel_intent(record,
                    reason=marker['reason'], event_id=marker['evidence_id'])
            self._binding_current()
            self.broker.verify_read_snapshot(context.identity_fingerprint)
            self._controls()
            if (reservation.last_evidence_at is None or not 0 <=
                    (instant(self.clock())-instant(reservation.last_evidence_at)).total_seconds()
                    <= self.maximum_evidence_age_seconds):
                raise ContractError('Exact cancellation status needs refresh before DELETE')
            if authorization[0].response.get('broker_order_id') != reservation.broker_order_id:
                raise ContractError('Cancellation intent differs from exact accepted order')
        def before_cancel(context, reservation):
            try:
                check_cancel(context,reservation)
            except BaseException:
                # This callback executes strictly before the existing broker's
                # DELETE. A thrown gate has positive before-effect provenance;
                # transport failures outside this callback do not get this flag.
                known_before_delete[0] = True
                raise
        def event(kind, payload):
            if kind in {'cancellation_temporary_failure','cancellation_reconciliation_required'}:
                payload = {**payload,'known_before_delete':payload.get('stage') == 'delete_exact_order'
                    and (known_before_delete[0] or not delete_entered[0])}
            self.store.event(forecast['cycle_id'], kind, payload, symbol=forecast['symbol'])
            if kind in {'cancellation_send_claimed','cancellation_acknowledged','cancellation_temporary_failure','cancellation_reconciliation_required'}:
                if journal.order_state(rid)['cancellation'] is not None:
                    journal.record_cancellation_event(digest([kind,payload]), kind, payload)
        service = CancellationService(broker=CancellationBrokerView(), ledger=journal,
            account_fingerprint=self.binding.account_fingerprint, before_cancel=before_cancel,
            apply_evidence=lambda evidence:self._apply_order(journal,record,evidence), record_event=event,
            clock=self.clock, stopped=self.stopped, stream='scout-15m',
            observe=lambda reservation:self._adapter(journal,record).status(rid))
        retry = False
        for item in journal.order_state(rid)['cancellation_events']:
            if item['kind'] in {'cancellation_send_claimed','cancellation_acknowledged'}:
                retry = False
            elif item['kind'] == 'cancellation_temporary_failure' and item['payload'].get('stage') == 'delete_exact_order':
                retry = True
            elif item['kind'] == 'cancellation_reconciliation_required' and item['payload'].get('stage') == 'delete_exact_order':
                retry = item['payload'].get('known_before_delete') is True
        try:
            return service.step(reservation_id, reason='new-forecast',
                evidence_id='scout-replace:' + digest([forecast['forecast_id'],reservation_id]),
                trigger_forecast_id=forecast['forecast_id'], retry_send=retry)
        except BaseException as exc:
            # A user interrupt in a gate is positive before-effect evidence;
            # an interrupt during transport remains ambiguous and cannot retry.
            if not isinstance(exc,Exception) and (known_before_delete[0] or delete_preparing[0] and not delete_entered[0]):
                event('cancellation_reconciliation_required',dict(reservation_id=reservation_id,
                    stage='delete_exact_order',error_type=type(exc).__name__,
                    observed_at=instant(self.clock()).isoformat(),stream='scout-15m'))
            raise

    def _previous(self, forecast):
        self._clearance = None
        for journal in self._journals():
            for record in journal.records():
                if record['request']['symbol'] != forecast['symbol'] or record['request']['forecast_id'] == forecast['forecast_id']:
                    continue
                receipt = self._recover_record(journal, record)
                record = journal.get(record['request']['request_id'])
                if self._released(journal, record, receipt):
                    continue
                if record['state'] == 'ACCEPTED':
                    outcome = self._cancel_previous(journal,record,forecast)
                    return self._record(forecast,'previous_intention_reconciliation',evidence=asdict(outcome))
                return self._record(forecast,'previous_intention_reconciliation',
                    evidence={'request_id':record['request']['request_id'], 'local_state':record['state'],
                        'authority_status':receipt.response['status'], 'retry_after_seconds':3})
        # The single serialized executor owns local order mutations. Every
        # historical commitment was checked through authenticated, saved Atlas
        # evidence above; this invocation may now request a fresh reservation.
        self._clearance = (forecast['forecast_id'],forecast['symbol'],self.session_id)
        return None

    def _sizing_current(self, forecast_id, original):
        with closing(sqlite3.connect(_safe_path(self.store.path).as_uri()+'?mode=ro',uri=True,timeout=0)) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?',
                             (forecast_id,)).fetchone()
            if IntentionStore._value(row) != original:
                raise ContractError('Exact original immutable sizing is no longer saved')
        return True

    def _capture(self):
        self._controls()
        if self.broker.stable_account_fingerprint() != self.binding.account_fingerprint:
            raise ContractError('Scout broker account differs from shared capacity binding')
        portfolio = capture_portfolio_state(self.broker, observed_at=self.clock(), parallel=True,
            literal_cash_only=True,use_actual_quote_timestamps=True,symbols=self.symbols,include_order_identities=True)
        self._controls()
        self.broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
        at = instant(self.clock())
        if not 0 <= (at-instant(portfolio.observed_at)).total_seconds() <= self.maximum_evidence_age_seconds:
            raise ContractError('Scout account snapshot is stale')
        portfolio = replace(portfolio, observed_at=at.isoformat(), source_fingerprint=digest({
            'capture':portfolio.source_fingerprint,'validated_at':at.isoformat()}))
        return portfolio, digest(['scout-account',portfolio.source_fingerprint,at.isoformat()])

    def step(self, forecast):
        with self._account():
            return self._step(forecast)

    def _step(self, forecast):
        self._controls()
        if self.journal is None:
            raise ContractError('Scout executor has not passed preflight')
        initial = forecast_disposition(forecast,now=self.clock(),session_started_at=self.session_started_at,local_symbols=self.symbols)
        if initial in {'outside_local_scout_scope','not_a_current_live_forecast','forecast_precedes_manual_start','incomplete_ensemble','wrong_session_date'}:
            return self._record(forecast,initial)
        self._compatibility()
        if not self._latest(forecast) or self.intents.processed_at_or_after(forecast):
            return self._record(forecast,'newer_or_same_reference_intention_already_processed')
        unclaimed = None
        for journal in self._journals():
            for record in journal.records():
                if record['request']['forecast_id'] == forecast['forecast_id']:
                    if (record['state'] in {'PREPARED','GRANTED'}
                            and record['request']['session_id'] == self.session_id
                            and initial == 'eligible_for_account_checks'):
                        if unclaimed is not None:
                            raise ContractError('Multiple private requests claim one forecast')
                        unclaimed = record
                        continue
                    receipt = self._recover_record(journal,record)
                    current = journal.get(record['request']['request_id'])
                    order = self._order(journal,current,receipt)
                    return self._record(forecast,'recover_existing_intention',order=order,
                        evidence={'request_id':record['request']['request_id'],'authority_status':receipt.response['status']})
        portfolio,snapshot_id = self._capture()
        original = self.intents.get(forecast['forecast_id'])
        unavailable = None
        if original is None:
            frozen = self.store.inputs(forecast['cycle_id'],forecast['symbol'])
            if frozen is None or frozen[0] != forecast['snapshot_hash']:
                raise ContractError('Original forecasting inputs unavailable')
            quote = portfolio.quotes[forecast['symbol']]
            result = size_forecast(forecast,forecast_snapshot=frozen[1],
                account_equity=AvailableInput(portfolio.account_equity,portfolio.observed_at,
                    'schwab-liquidation-equity',{'snapshot_id':snapshot_id}),
                quote=QuoteObservation(quote.bid,quote.ask,quote.freshness_observed_at,
                    portfolio.observed_at,'schwab-current-quote',{'snapshot_id':snapshot_id,'quote':asdict(quote)}),
                calculated_at=self.clock(),maximum_quote_age_seconds=self.maximum_evidence_age_seconds)
            original = self.intents.save_sizing(forecast,result)
            if result.status != 'sized':
                unavailable = original
        prior = self._previous(forecast)
        if prior is not None:
            return prior
        if unavailable is not None:
            return unavailable
        disposition = forecast_disposition(forecast,now=self.clock(),session_started_at=self.session_started_at,local_symbols=self.symbols)
        if disposition != 'eligible_for_account_checks':
            return self._record(forecast,disposition)
        if original['desired_quantity'] == 0:
            return self._record(forecast,'sizing_produced_zero')
        window = stock_execution_window(self.clock())
        if not window.executable:
            return self._record(forecast,'market_session_closed',evidence={'reason':window.reason})
        quote = portfolio.quotes[forecast['symbol']]
        price = Decimal(str(quote.ask if original['side']=='BUY' else quote.bid)).quantize(Decimal('.01'),
            rounding=ROUND_CEILING if original['side']=='BUY' else ROUND_FLOOR)
        canonical_price = format(price.normalize(),'f')
        if unclaimed is not None:
            request = Request.from_payload(unclaimed['request'])
            if (request.limit_price != canonical_price or request.side != original['side']
                    or request.quantity != original['desired_quantity'] or request.sizing_sha256 != original['sizing_id']):
                receipt = self._recover_record(self.journal,unclaimed)
                return self._record(forecast,'unclaimed_quote_changed',
                    evidence={'request_id':request.request_id,'authority_status':receipt.response['status']})
        else:
            request = Request('scout-15m:'+forecast['forecast_id'],self.binding.account_fingerprint,self.session_id,
                forecast['symbol'],original['side'],original['desired_quantity'],canonical_price,forecast['forecast_id'],
                forecast['forecast_id'],forecast['snapshot_hash'],original['sizing_id'],forecast['reference_at'],
                instant(self.clock()).isoformat(),forecast['target_close'],forecast['target_close'])
        self.journal.prepare(request)
        decision = self.authority.reserve(request)
        capacity = None
        if 'capacity' in decision.response:
            capacity = CapacityDecision(**decision.response['capacity'])
            checked = clip_to_capacity(**{key:decision.response['capacity'][key] for key in
                ('symbol','side','desired_quantity','limit_price','available_cash','available_shares',
                 'capacity_evidence_id','observed_at','calculated_at')})
            if capacity != checked:
                raise ContractError('Shared capacity decision failed local validation')
        if decision.grant is None:
            if decision.response.get('reason') == 'expired_before_admission':
                from .scout_authority import validate_negative_admission_receipt
                validate_negative_admission_receipt(self.journal.get(request.request_id),decision,self.binding,now=self.clock())
            self.journal.revoke_unsent(request.request_id,'authority-declined')
            return self._record(forecast,decision.response['status'].lower(),capacity=capacity,
                evidence={'request_id':request.request_id,'authority_receipt':decision.sha256})
        grant = decision.grant
        self.journal.grant(grant)
        return self._submit(forecast,request,grant,portfolio,quote,window,original,capacity)

    def _submit(self, forecast, request, grant, portfolio, quote, window, original, capacity):
        from .scout_authority import validate_send_receipt
        authorized = [None]
        def proof(req, granted):
            self._binding_current()
            self._sizing_current(forecast['forecast_id'],original)
            current_price = Decimal(str(quote.ask if req.side=='BUY' else quote.bid)).quantize(Decimal('.01'),
                rounding=ROUND_CEILING if req.side=='BUY' else ROUND_FLOOR)
            if (req != request or granted != grant or self._ready is None or self._ready_at is None
                    or not 0 <= (instant(self.clock())-self._ready_at).total_seconds() <= self.maximum_evidence_age_seconds
                    or req.sizing_sha256 != original['sizing_id'] or req.quantity != original['desired_quantity']
                    or req.side != original['side'] or Decimal(req.limit_price) != current_price
                    or self._clearance != (forecast['forecast_id'],forecast['symbol'],self.session_id)):
                return False
            if authorized[0] is not None:
                return validate_send_receipt(self.journal.get(req.request_id),authorized[0],self.binding,now=self.clock())
            return True  # Preclaim eligibility only; broker's separate authorization gate is mandatory.
        def final(req, granted):
            return local_gate(req,granted,forecast=forecast,forecast_path=self.store.path,
                session_id=self.session_id,session_started=self.session_started_at,local_symbols=self.symbols,
                account_fingerprint=self.binding.account_fingerprint,quote_observed_at=quote.freshness_observed_at,
                max_quote_age_seconds=self.maximum_evidence_age_seconds,expected_time_in_force=window.time_in_force,
                manual_authority=self.manual_authority,stopped=self.stopped,authorization_current=proof,clock=self.clock)
        def authorize(req, granted):
            if authorized[0] is None:
                authorized[0] = self.authority.authorize_send(self.journal.get(req.request_id))
            return validate_send_receipt(self.journal.get(req.request_id),authorized[0],self.binding,now=self.clock())
        client = ScoutClient(self.journal,self.authority,account_fingerprint=self.binding.account_fingerprint,
            session_id=self.session_id,symbols=frozenset(self.symbols),authority_epoch=grant.authority_epoch,clock=self.clock)
        try:
            record = client.submit(request.request_id,final_gate=final,
                send=lambda req, granted:self._adapter(self.journal,self.journal.get(req.request_id)).send(req,granted,
                    expected_snapshot_identity=portfolio.broker_identity_fingerprint,authorization_gate=authorize,final_gate=final))
            self._accepted_event(self.journal,record)
            return self._record(forecast,'submitted',capacity=capacity,
                order=self.journal.lookup_reservation(grant.reservation_id),
                evidence={'request_id':request.request_id,'reservation_id':grant.reservation_id})
        except Exception as exc:
            record = self.journal.get(request.request_id)
            # No cleanup path cancels/releases capacity based on this exception.
            # Recovery asks Atlas and uses exact accepted-ID evidence separately.
            order = self._order(self.journal,record)
            unclaimed = record['state'] == 'GRANTED'
            return self._record(forecast,'not_submitted' if unclaimed else 'submission_recovery_required',order=order,
                evidence={'request_id':request.request_id,'local_state':record['state'],
                    'error_type':type(exc).__name__,'retry_after_seconds':retry_delay(exc,self.clock()),
                    'proven_unsent_retry_eligible':unclaimed})
