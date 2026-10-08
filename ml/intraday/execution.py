"""Offline venue contract and persistent recovery; no Schwab/provider imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import time

from .book import OfflineBook, OrderEvidence, Request, TERMINAL
from .prediction import ContractError, instant, number


class TemporaryFailure(Exception):
    def __init__(self, message="temporary offline failure", *, retry_after=0):
        super().__init__(message)
        self.retry_after = float(number(retry_after))


class QuoteUnavailable(TemporaryFailure):
    """No usable quote was returned; a successful unchanged quote is different."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: str
    ask: str
    current: str
    observed_at: str
    received_at: str

    def payload(self):
        bid, ask = number(self.bid, positive=True), number(self.ask, positive=True)
        number(self.current, positive=True)
        if bid > ask or instant(self.observed_at) > instant(self.received_at):
            raise ContractError("Invalid quote")
        return {**asdict(self), "midpoint": str((bid + ask) / 2), "spread": str(ask - bid)}


class OfflineVenue:
    """Implement only with mock fixtures for this prototype's authorized tests.

    status() looks up the persistent request even if no broker ID was returned.
    None means unresolved, including a nonauthoritative 'not found'. A definitive
    nonacceptance requires complete NOT_ACCEPTED evidence, not a timeout.
    """
    def quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    def submit(self, *, request_id, symbol, side, quantity, limit, quote) -> OrderEvidence | None:
        raise NotImplementedError

    def status(self, request_id: str) -> OrderEvidence | None:
        raise NotImplementedError

    def cancel(self, request_id: str):
        raise NotImplementedError


class OfflineExecutor:
    def __init__(self, book: OfflineBook, venue: OfflineVenue, *, clock, sleep=time.sleep,
                 maximum_quote_age_seconds, stopped=lambda: False):
        if not isinstance(venue, OfflineVenue):
            raise ContractError("Only an explicit offline venue can be supplied")
        self.book, self.venue = book, venue
        self.clock, self.sleep, self.stopped = clock, sleep, stopped
        self.maximum_age = float(number(maximum_quote_age_seconds, positive=True))

    def _retry(self, operation, *, request_id=None, state_name):
        while not self.stopped():
            try:
                return operation()
            except (TemporaryFailure, TimeoutError) as exc:
                with self.book.transaction(state_name, {"request_id": request_id, "reason": type(exc).__name__}) as _:
                    pass
                self.sleep(max(3.0, getattr(exc, "retry_after", 0)))
        return None

    def fresh_quote(self, symbol):
        def get():
            quote = self.venue.quote(symbol)
            quote.payload()
            now = instant(self.clock())
            if quote.symbol != symbol:
                raise ContractError("Quote symbol differs from instruction")
            if not instant(quote.observed_at) <= instant(quote.received_at) <= now:
                raise ContractError("Quote times cannot lie in the future")
            if (now - instant(quote.observed_at)).total_seconds() > self.maximum_age:
                raise QuoteUnavailable("Quote needs refresh")
            return quote
        return self._retry(get, state_name="refreshing_price")

    def _apply(self, evidence, request_id):
        if evidence.request_id != request_id:
            raise ContractError("Status response belongs to another persistent request")
        return self.book.apply(evidence)

    def execute(self, request: Request, *, admission_id):
        # Existing commitments recover before quote refresh or another POST.
        existing = self.book.snapshot()["orders"].get(request.request_id)
        if existing:
            from .prediction import encoded
            original = existing["request"]["sizing"].get("original_intent", existing["request"])
            if encoded(original) != encoded(asdict(request)):
                raise ContractError("Retry changed persistent instruction")
            if existing["status"] != "RESERVED":
                return existing if existing["status"] in TERMINAL else self.recover(request.request_id)
            # A persisted pre-send reservation uses its exact original quote/intent.
            # If that quote is no longer contemporaneous, leave it reserved until
            # an explicit repricing policy is provided rather than mutating intent.
            raise ContractError("Unsent persisted reservation needs explicit refresh/repricing policy")
        quote = self.fresh_quote(request.symbol)
        if quote is None:
            return None
        quoted = replace(request, limit=quote.ask if request.side == "BUY" else quote.bid,
                         sizing={**request.sizing, "submission_quote": quote.payload(),
                                 "original_intent": asdict(request),
                                 "execution_price_proposal": "ask-buy-bid-sell-offline-fixture"})
        reserved = self.book.reserve(quoted, admission_id=admission_id)
        # No spread-width veto, FOK/AON flag, or six-orders-per-wake ceiling.
        if (instant(self.clock()) - instant(quote.observed_at)).total_seconds() > self.maximum_age:
            raise ContractError("Quote aged after reservation; explicit repricing needed")
        self.book.begin_submit(request.request_id)
        try:
            evidence = self.venue.submit(request_id=request.request_id, symbol=request.symbol, side=request.side,
                                         quantity=reserved["permitted"], limit=quoted.limit, quote=quote.payload())
        except (TemporaryFailure, TimeoutError):
            self.book.unknown(request.request_id)
            return self.recover(request.request_id)
        except Exception:
            # An adapter/code failure does not prove the POST never happened.
            self.book.unknown(request.request_id)
            raise
        if evidence is None:
            self.book.unknown(request.request_id)
            return self.recover(request.request_id)
        return self._apply(evidence, request.request_id)

    def recover(self, request_id):
        while not self.stopped():
            current = self.book.order(request_id)
            if current["status"] in TERMINAL:
                return current
            if current["status"] == "RESERVED":
                raise ContractError("Submission has not started")
            evidence = self._retry(lambda: self.venue.status(request_id), request_id=request_id,
                                   state_name="retrying_status")
            if evidence is not None:
                return self._apply(evidence, request_id)
            if not self.stopped():
                with self.book.transaction("retrying_status", {"request_id": request_id, "result": "unresolved"}) as _:
                    pass
                self.sleep(3.0)
        return self.book.order(request_id)

    def cancel(self, request_id):
        self.book.cancel_requested(request_id)
        try:
            self.venue.cancel(request_id)
        except (TemporaryFailure, TimeoutError):
            pass
        # Cancel acknowledgment alone releases nothing. Status may show an
        # intervening fill; apply it before caller plans any replacement.
        return self.recover(request_id)
