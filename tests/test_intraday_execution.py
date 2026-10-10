from dataclasses import replace
from datetime import datetime

import pytest

from ml.intraday.book import Capacity, Fill, OfflineBook, OrderEvidence, Request
from ml.intraday.execution import OfflineExecutor, OfflineVenue, Quote, QuoteUnavailable, TemporaryFailure
from ml.intraday.prediction import ContractError

NOW = "2026-10-08T17:16:00+00:00"


class MockVenue(OfflineVenue):
    def __init__(self):
        self.posts = []
        self.quotes = []
        self.statuses = []
        self.submission = None
        self.cancelled = []

    @staticmethod
    def value(sequence):
        result = sequence.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def quote(self, symbol):
        return self.value(self.quotes) if self.quotes else Quote(symbol, "99", "100", "99.5", NOW, NOW)

    def submit(self, **kwargs):
        self.posts.append(kwargs)
        if isinstance(self.submission, BaseException):
            raise self.submission
        return self.submission

    def status(self, request_id):
        return self.value(self.statuses)

    def cancel(self, request_id):
        self.cancelled.append(request_id)


def setup(tmp_path, venue=None, *, stopped=lambda: False):
    ledger = OfflineBook(tmp_path / "offline-intraday.sqlite3", account_id="offline-account",
                         atlas_symbols=("AAPL",), gameplan_symbols=("AAPL", "ABCL"))
    ledger.capacity(Capacity("initial", NOW, "1000", "1000", {"AAPL": "1000", "ABCL": "1000"}))
    sleeps = []
    venue = venue or MockVenue()
    runner = OfflineExecutor(ledger, venue, clock=lambda: datetime.fromisoformat(NOW), sleep=sleeps.append,
                             maximum_quote_age_seconds=5, stopped=stopped)
    return ledger, venue, runner, sleeps


def request(rid="request", qty=3):
    return Request(rid, "prediction-" + rid, "AAPL", "BUY", qty, "100", "atlas-15m")


def status(rid="request", *, state="WORKING", qty=3, fills=(), eid="status"):
    return OrderEvidence(eid, rid, "broker-" + rid, NOW, state, qty, fills)


def test_unknown_submit_recovers_identity_with_indefinite_status_retries(tmp_path):
    ledger, venue, runner, sleeps = setup(tmp_path)
    venue.submission = TimeoutError("response lost after acceptance")
    venue.statuses = [TemporaryFailure(retry_after=8), None] + [TemporaryFailure()] * 11 + [TimeoutError(), status()]
    result = runner.execute(request(), admission_id="fixture-manual")
    assert result["status"] == "WORKING"
    assert len(venue.posts) == 1
    assert sleeps[0] == 8 and len(sleeps) == 14
    venue.statuses = [status(eid="retrieved-again")]
    assert runner.execute(request(), admission_id="retry")["status"] == "WORKING"
    assert len(venue.posts) == 1
    with pytest.raises(ContractError, match="changed"):
        runner.execute(replace(request(), desired=4), admission_id="changed-retry")
    assert ledger.order("request")["permitted"] == 3


def test_quote_failure_retries_successful_unchanged_quote_is_usable(tmp_path):
    ledger, venue, runner, sleeps = setup(tmp_path)
    unchanged = Quote("AAPL", "99", "100", "99.5", NOW, NOW)
    venue.quotes = [QuoteUnavailable(), unchanged]
    venue.submission = status()
    result = runner.execute(request(), admission_id="quote-fixture")
    assert result["status"] == "WORKING" and sleeps == [3.0]
    assert venue.posts[0]["limit"] == "100"
    assert venue.posts[0]["quote"]["midpoint"] == "99.5"
    assert venue.posts[0]["quote"]["spread"] == "1"
    assert ledger.order("request")["request"]["sizing"]["submission_quote"] == unchanged.payload()


def test_wide_spread_is_recorded_without_veto_and_sell_uses_bid(tmp_path):
    ledger, venue, runner, _ = setup(tmp_path)
    ledger.import_allocation(allocation_id="short", symbol="AAPL", horizon="15m", held=3, completed_purchase_id="old-buy")
    sale = replace(request(), side="SELL", allocation_caps=(("short", 3),))
    venue.quotes = [Quote("AAPL", "50", "100", "75", NOW, NOW)]
    venue.submission = status()
    assert runner.execute(sale, admission_id="wide-spread")["status"] == "WORKING"
    assert venue.posts[0]["limit"] == "50" and venue.posts[0]["quote"]["spread"] == "50"


def test_crash_after_send_gate_restarts_by_status_only(tmp_path):
    ledger, venue, runner, _ = setup(tmp_path)
    venue.submission = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        runner.execute(request(), admission_id="crash-fixture")
    assert ledger.order("request")["status"] == "SUBMITTING"
    reopened = OfflineBook(ledger.path, account_id="offline-account", atlas_symbols=("AAPL",), gameplan_symbols=("AAPL", "ABCL"))
    new_runner = OfflineExecutor(reopened, venue, clock=lambda: datetime.fromisoformat(NOW), sleep=lambda _: None,
                                 maximum_quote_age_seconds=5)
    venue.statuses = [status(state="FILLED", fills=(Fill("fill1", 3, "99", NOW),))]
    assert new_runner.execute(request(), admission_id="restart")["filled"] == 3
    assert len(venue.posts) == 1


def test_cancellation_race_records_intervening_fills_before_replacement(tmp_path):
    ledger, venue, runner, _ = setup(tmp_path)
    venue.submission = status(state="PARTIAL", fills=(Fill("f1", 1, "100", NOW),))
    runner.execute(request(), admission_id="initial")
    venue.statuses = [status(state="CANCELLED", fills=(Fill("f1", 1, "100", NOW), Fill("f2", 1, "99", NOW)), eid="cancelled")]
    cancelled = runner.cancel("request")
    assert cancelled["remaining"] == 1
    replacement = replace(request("remainder", qty=1), forecast_id="prediction-request", parent_request="request")
    venue.submission = status("remainder", qty=1, eid="remainder-accepted")
    runner.execute(replacement, admission_id="explicit-fixture-replacement-policy")
    assert [p["quantity"] for p in venue.posts] == [3, 1]
    assert ledger.order("request")["filled"] == 2


def test_invalid_requests_are_not_retried_and_unknown_capacity_survives_stop(tmp_path):
    ledger, venue, runner, sleeps = setup(tmp_path)
    venue.quotes = [Quote("AAPL", "101", "100", "100", NOW, NOW)]
    with pytest.raises(ContractError):
        runner.execute(request(), admission_id="invalid-quote")
    assert not venue.posts and not sleeps and not ledger.snapshot()["orders"]
    venue.submission = ValueError("mock adapter code problem")
    with pytest.raises(ValueError):
        runner.execute(request(), admission_id="invalid-code")
    assert ledger.order("request")["status"] == "UNKNOWN"
    runner.stopped = lambda: True
    assert runner.recover("request")["status"] == "UNKNOWN"
    assert ledger.reserve(request("independent", qty=10), admission_id="other")["permitted"] == 7


def test_no_six_orders_per_wake_ceiling(tmp_path):
    ledger, venue, runner, _ = setup(tmp_path)
    for i in range(8):
        rid = f"request-{i}"
        venue.submission = status(rid, qty=1, eid=rid)
        assert runner.execute(request(rid, qty=1), admission_id=rid)["status"] == "WORKING"
    assert len(venue.posts) == 8


def test_status_for_another_request_does_not_release_original(tmp_path):
    ledger, venue, runner, _ = setup(tmp_path)
    venue.submission = status("another-request", state="NOT_ACCEPTED")
    with pytest.raises(ContractError, match="another persistent"):
        runner.execute(request(), admission_id="wrong-status")
    assert ledger.order("request")["status"] == "SUBMITTING"


def test_end_to_end_issue_size_quote_reservation_fill_and_outcome(tmp_path):
    from datetime import date, timedelta
    from ml.intraday.prediction import Candle, PredictionHistory, TARGET_VERSION
    from ml.intraday.prototype import AtlasPrototype, AtlasScope
    ledger, venue, runner, _ = setup(tmp_path)
    history = PredictionHistory(tmp_path / "issues.sqlite3")
    prototype = AtlasPrototype(scope=AtlasScope(("AAPL",), ("AAPL", "ABCL")), history=history)
    start = datetime.fromisoformat("2026-10-08T11:30:00+00:00")
    candles = []
    for i in range(23):
        opened = start + timedelta(minutes=15 * i)
        candles.append(Candle("AAPL", opened.isoformat(), (opened + timedelta(minutes=15)).isoformat(),
                              "100", "101", "99", "100", "1000", "offline-fixture"))
    result = prototype.issue_cycle(candles, issued_at=NOW, eligible_session=date(2026, 10, 8),
                                   model_version="mock-model", model_target_version=TARGET_VERSION,
                                   infer_by_symbol={"AAPL": lambda _: "0.7"})
    prediction = result["predictions"]["AAPL"]
    sizing = prototype.explain_size(prediction, candles, side="BUY", expected_volume=("1000", NOW),
                                    holdings=(0, NOW), recent_executed=(0, NOW), hourly_reference=10,
                                    reference_version="fixture-hourly-reference", score_mapping=lambda _: "0.36",
                                    score_version="fixture-score-not-selected-for-live")
    instruction = prototype.instruction(prediction, sizing, request_id="integrated")
    venue.submission = status("integrated", state="FILLED", qty=4, fills=(Fill("f1", 4, "100", NOW),), eid="integrated-fill")
    outcome = runner.execute(instruction, admission_id="offline-manual")
    assert sizing.desired_quantity == 4 and outcome["filled"] == 4
    target = Candle("AAPL", prediction.target_open, prediction.target_close, "100", "101", "99", "100", "1000", "offline-fixture")
    final = history.observe(prediction.prediction_id, as_of=prediction.target_close, candle=target, observation_id="actual")
    assert final["status"] == "completed" and final["target_not_down"] is True
    assert history.get(prediction.prediction_id) == prediction
    assert ledger.snapshot()["fills"]["f1"]["allocation_effects"] == {"purchase:integrated": 4}
