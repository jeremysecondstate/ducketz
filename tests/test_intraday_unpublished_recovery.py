"""Unpublished primary intervals use only the selected native recovery policy."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ml.intraday.candle_collection import CandleCollector
from ml.intraday.coverage import RECOVERY_POPULATION_CONTRACT
from ml.intraday.data import AcquisitionJournal
from ml.intraday.gap_recovery import GapRecovery
from ml.intraday.recovered_population import verified_recovery_candle
from ml.intraday.store import ShadowStore


START = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)
NOW = END + timedelta(seconds=30)
PUBLISHED_END = datetime(2026, 10, 17, tzinfo=timezone.utc)
SESSION = "manual-atlas-unpublished-fixture"


class UnpublishedPrimary:
    dataset = "EQUS.MINI"

    def __init__(self):
        self.metadata_calls = 0

    def native_specs(self):
        return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]

    def dataset_range(self):
        self.metadata_calls += 1
        return {}

    def available_range_for_schema(self, schema, *, dataset_range):
        assert schema == "ohlcv-1m"
        return SimpleNamespace(start=START-timedelta(days=30), end=PUBLISHED_END)

    def fetch_native_bars_range(self, *args, **kwargs):
        pytest.fail("Unpublished primary coverage cannot supply a price response")


def setup_collector(tmp_path, monkeypatch, *, alternate="schwab", contract=RECOVERY_POPULATION_CONTRACT):
    from ml.intraday import gap_recovery, provider_recovery

    calls = []
    stop = [False]
    values = dict(open=100., high=102., low=99., close=101., volume=20.)

    class Schwab:
        def get_price_history(self, symbol, **kwargs):
            calls.append("schwab")
            assert symbol == "TEST" and kwargs["frequency"] == 1
            if alternate == "missing":
                raise ValueError("Fixture native response unavailable")
            return {"symbol": symbol, "candles": [dict(values,
                datetime=int(START.timestamp()*1000))] if alternate == "schwab" else []}

    def fmp_request(provider, endpoint, params):
        calls.append("fmp")
        assert endpoint == "historical-chart/5min" and params["symbol"] == "TEST"
        stamp = START.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
        return [dict(values, date=stamp)] if alternate == "fmp" else []

    def recovery(root, **kwargs):
        return GapRecovery(root, **kwargs, schwab=Schwab(), fmp=SimpleNamespace(api_key=""))

    monkeypatch.setattr(provider_recovery, "fmp_request", fmp_request)
    monkeypatch.setattr(gap_recovery, "GapRecovery", recovery)
    store = ShadowStore(tmp_path/"intraday/shadow.sqlite3")
    journal = AcquisitionJournal(tmp_path/"intraday/acquisition.sqlite3")
    provider = UnpublishedPrimary()
    collector = CandleCollector(store, histories={}, provider=provider, journal=journal,
        symbols=("TEST",), session_id=SESSION, started_at=START,
        clock=lambda: NOW, stopped=lambda: stop[0], population_contract=contract)
    return collector, calls, stop


def collection_state(collector):
    with collector.store.connect() as db:
        return db.execute("SELECT status,last_error FROM intraday_candle_collection WHERE session_id=?",
                          (SESSION,)).fetchone()


def recovery_events(collector):
    with collector.store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE cycle_id=? AND kind='cross_provider_gap_recovery'", (SESSION,))]


@pytest.mark.parametrize("alternate,seconds", [("schwab", 60), ("fmp", 300)])
def test_unpublished_primary_recovers_actual_native_alternate(tmp_path, monkeypatch, alternate, seconds):
    collector, calls, _ = setup_collector(tmp_path, monkeypatch, alternate=alternate)
    assert collector.pass_once()["saved"] == 1
    assert collection_state(collector) == ("saved", None)
    assert calls == ["schwab", "fmp"]
    candle, = collector.store.observed_candles("TEST")
    assert candle["observation_kind"] == "alternate_provider_observed"
    assert candle["native_source_id"].startswith(alternate+":")
    assert candle["native_interval_seconds"] == seconds
    assert candle["available_at"] == NOW.isoformat()
    assert candle["observed_constituents"] == 1 and candle["volume"] == 20
    assert not candle["synthetic"] and not candle["no_trade"]
    assert verified_recovery_candle(candle)
    evidence, = recovery_events(collector)
    assert not evidence["assessment"]["derived_allowed"]
    assert all(item["provider"] != "databento" for item in evidence["assessment"]["evidence"])
    with sqlite3.connect(collector.journal.path) as db:
        primary = json.loads(db.execute("SELECT payload FROM attempts").fetchone()[0])
    assert primary["status"] == "unavailable_coverage"
    assert primary["coverage_close"] == PUBLISHED_END.isoformat() and not primary["rows"]
    assert list((tmp_path/"intraday/gap-recovery/responses").glob("*.json"))
    assert list((tmp_path/"intraday/gap-recovery/candles").glob("*.json"))


@pytest.mark.parametrize("alternate", ["empty", "missing"])
def test_unpublished_with_empty_or_missing_alternates_stays_pending(tmp_path, monkeypatch, alternate):
    collector, calls, _ = setup_collector(tmp_path, monkeypatch, alternate=alternate)
    assert collector.pass_once()["saved"] == 0
    assert calls == ["schwab", "fmp"]
    status, error = collection_state(collector)
    assert status == "pending"
    assert json.loads(error)["error_type"] == "AcquisitionPending"
    assert "unavailable_coverage" in json.loads(error)["message"]
    assert not collector.store.observed_candles("TEST")
    event, = recovery_events(collector)
    assert not event["assessment"]["observed_candidates"]
    assert not event["assessment"]["derived_allowed"]
    assert "databento" not in event["assessment"]["agreeing_providers"]


def test_legacy_population_does_not_acquire_alternate_sources(tmp_path, monkeypatch):
    collector, calls, _ = setup_collector(tmp_path, monkeypatch, contract=None)
    assert collector.pass_once()["saved"] == 0
    assert collection_state(collector)[0] == "pending"
    assert not calls and not recovery_events(collector)


def test_manual_stop_prevents_primary_and_alternate_requests(tmp_path, monkeypatch):
    collector, calls, stop = setup_collector(tmp_path, monkeypatch)
    stop[0] = True
    assert collector.pass_once()["saved"] == 0
    assert collector.provider.metadata_calls == 0 and not calls
    assert collection_state(collector) is None
    assert stop[0] is True
