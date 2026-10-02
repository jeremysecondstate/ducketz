from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, get_ident
from types import ModuleType, SimpleNamespace
from typing import Callable

import pandas as pd
import pytest

import datafetching.options_runtime as options_runtime
import options.databento_live as databento_live
from datafetching.options_runtime import OptionsCycleResult
from ml.contracts import MLContractError
from ml.universe import PRODUCTION_OPTION_SYMBOLS
from options.databento_live import (
    DatabentoOpraIntegrityError,
    DatabentoOpraLiveAdapter,
)
from options.providers import OptionProviderUnavailable
from options.snapshot import normalize_databento_opra_option_snapshot


TARGET = pd.Timestamp("2026-08-05T17:15:00Z")
TEST_SYMBOL = PRODUCTION_OPTION_SYMBOLS[0]
pytestmark = pytest.mark.usefixtures("offline_databento_sdk")


def test_offline_sdk_preserves_historical_and_dbn_readers(offline_databento_sdk) -> None:
    from databento.common.dbnstore import DBNStore
    from databento.historical.client import Historical

    assert offline_databento_sdk.Historical is Historical
    assert offline_databento_sdk.DBNStore is DBNStore
    with pytest.raises(RuntimeError, match="Real Databento Live clients are disabled"):
        offline_databento_sdk.Live(key="unit-test-placeholder")


def test_offline_sdk_fixture_restores_preexisting_modules_and_live_identity(
    offline_databento_sdk,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import offline_databento_sdk as sdk_fixture

    class PreviousLive:
        def __init__(self, *args, **kwargs):
            pytest.fail("Fixture restoration must not construct a Live client")

    previous_client = ModuleType("databento.live.client")
    previous_client.Live = PreviousLive
    previous_parent = ModuleType("databento.live")
    previous_parent.client = previous_client
    monkeypatch.setitem(sys.modules, "databento.live.client", previous_client)
    monkeypatch.setitem(sys.modules, "databento.live", previous_parent)
    monkeypatch.setattr(offline_databento_sdk, "Live", PreviousLive)
    monkeypatch.setattr(offline_databento_sdk, "live", previous_parent, raising=False)
    previous_modules = {
        name: module for name, module in sys.modules.items()
        if name == "databento" or name.startswith("databento.")
    }

    nested_fixture = sdk_fixture.__wrapped__()
    try:
        assert next(nested_fixture) is offline_databento_sdk
        assert offline_databento_sdk.Live is not PreviousLive
        assert previous_parent.client is not previous_client
        sys.modules["databento.fixture_probe"] = ModuleType("databento.fixture_probe")
    finally:
        nested_fixture.close()

    assert offline_databento_sdk.Live is PreviousLive
    assert offline_databento_sdk.live is previous_parent
    assert previous_parent.client is previous_client
    assert previous_client.Live is PreviousLive
    assert {
        name: module for name, module in sys.modules.items()
        if name == "databento" or name.startswith("databento.")
    } == previous_modules


class _FakeLiveClient:
    def __init__(self, **kwargs: object) -> None:
        self.options = {
            key: value for key, value in kwargs.items() if key != "key"
        }
        self.key_was_present = bool(kwargs.get("key"))
        self.subscriptions: list[dict[str, object]] = []
        self.callback: Callable[[object], None] | None = None
        self.callback_error: Callable[[Exception], None] | None = None
        self.reconnect_callback: Callable[[object, object], None] | None = None
        self.started = False
        self.stopped = False

    def add_callback(
        self,
        callback: Callable[[object], None],
        callback_error: Callable[[Exception], None],
    ) -> None:
        self.callback = callback
        self.callback_error = callback_error

    def add_reconnect_callback(
        self,
        callback: Callable[[object, object], None],
        callback_error: Callable[[Exception], None],
    ) -> None:
        self.reconnect_callback = callback
        self.callback_error = callback_error

    def subscribe(self, **kwargs: object) -> int:
        self.subscriptions.append(dict(kwargs))
        return len(self.subscriptions)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def block_for_close(self, *, timeout: float) -> None:
        assert timeout == 5.0


def _rtype(name: str) -> object:
    return SimpleNamespace(name=name)


def _definition(
    *,
    contract: str,
    call_put: str,
    effective_at: pd.Timestamp = TARGET - pd.Timedelta(days=1),
    instrument_id: int,
) -> object:
    return SimpleNamespace(
        rtype=_rtype("INSTRUMENT_DEF"),
        raw_symbol=contract,
        underlying=TEST_SYMBOL,
        instrument_id=instrument_id,
        ts_recv=effective_at.value,
        ts_event=(effective_at - pd.Timedelta(milliseconds=1)).value,
        ts_out=(effective_at + pd.Timedelta(milliseconds=1)).value,
        activation=(effective_at - pd.Timedelta(days=1)).value,
        pretty_expiration=pd.Timestamp("2026-08-21T00:00:00Z"),
        pretty_strike_price=100.0,
        # Databento defines contract_multiplier as an unscaled int32.
        contract_multiplier=100,
        instrument_class=call_put,
        cfi=f"O{call_put}ASPS",
        security_type="OPT",
        security_update_action="A",
        publisher_id=30,
    )


def _quote(
    *,
    instrument_id: int,
    interval_end: pd.Timestamp = TARGET,
    bid: float = 1.0,
    ask: float = 1.1,
) -> object:
    return SimpleNamespace(
        rtype=_rtype("CBBO_1S"),
        instrument_id=instrument_id,
        publisher_id=30,
        ts_recv=interval_end.value,
        ts_event=(interval_end - pd.Timedelta(milliseconds=500)).value,
        ts_out=(interval_end + pd.Timedelta(milliseconds=10)).value,
        pretty_bid_px_00=bid,
        pretty_ask_px_00=ask,
        bid_sz_00=10,
        ask_sz_00=12,
    )


def _mapping(*, instrument_id: int, contract: str) -> object:
    return SimpleNamespace(
        rtype=_rtype("SYMBOL_MAPPING"),
        instrument_id=instrument_id,
        stype_out_symbol=contract,
    )


def _adapter(
    *,
    now: pd.Timestamp = TARGET + pd.Timedelta(seconds=2),
    **kwargs: object,
) -> tuple[DatabentoOpraLiveAdapter, _FakeLiveClient]:
    clients: list[_FakeLiveClient] = []

    def factory(**options: object) -> _FakeLiveClient:
        client = _FakeLiveClient(**options)
        clients.append(client)
        return client

    adapter = DatabentoOpraLiveAdapter(
        api_key="unit-test-placeholder",
        symbols=PRODUCTION_OPTION_SYMBOLS,
        clock=lambda: now.to_pydatetime(),
        client_factory=factory,
        snapshot_wait_seconds=0.0,
        **kwargs,
    )
    return adapter, clients[0]


def _complete_evidence(
    adapter: DatabentoOpraLiveAdapter,
) -> object:
    call_contract = f"{TEST_SYMBOL:<6}260821C00100000"
    put_contract = f"{TEST_SYMBOL:<6}260821P00100000"
    adapter.ingest_record(
        _definition(contract=call_contract, call_put="C", instrument_id=101)
    )
    adapter.ingest_record(
        _definition(contract=put_contract, call_put="P", instrument_id=102)
    )
    adapter.ingest_record(_quote(instrument_id=101))
    adapter.ingest_record(_quote(instrument_id=102, bid=1.2, ask=1.3))
    return adapter.fetch_snapshot(
        symbol=TEST_SYMBOL,
        target_snapshot_for=TARGET,
        requested_at=TARGET + pd.Timedelta(seconds=1),
    )


def test_live_adapter_cooperatively_yields_during_dense_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic = iter((10.0, 10.005, 10.011))
    sleeps: list[float] = []
    monkeypatch.setattr(databento_live.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(databento_live.time, "sleep", sleeps.append)
    adapter, _client = _adapter()

    adapter.ingest_record(SimpleNamespace(rtype=_rtype("SYSTEM")))
    assert sleeps == []
    adapter.ingest_record(SimpleNamespace(rtype=_rtype("SYSTEM")))
    assert sleeps == [databento_live.OPRA_CALLBACK_YIELD_SLEEP_SECONDS]


def test_live_adapter_uses_one_scoped_transport_and_strict_pretarget_cbbo() -> None:
    adapter, client = _adapter()
    evidence = _complete_evidence(adapter)

    assert client.started is True
    assert client.key_was_present is True
    assert not hasattr(adapter, "api_key")
    assert [row["schema"] for row in client.subscriptions] == [
        "definition",
        "cbbo-1s",
    ]
    assert all(row["dataset"] == "OPRA.PILLAR" for row in client.subscriptions)
    assert all(row["stype_in"] == "parent" for row in client.subscriptions)
    assert all(
        tuple(row["symbols"])
        == tuple(f"{symbol}.OPT" for symbol in PRODUCTION_OPTION_SYMBOLS)
        for row in client.subscriptions
    )
    assert set(evidence.definitions["call_put"]) == {"CALL", "PUT"}
    assert evidence.quotes["quote_timestamp"].eq(
        TARGET - pd.Timedelta(seconds=1)
    ).all()
    assert evidence.quotes["provider_interval_end_at"].eq(TARGET).all()
    assert evidence.quotes["market_event_clock_status"].eq(
        "CBBO_INTERVAL_START_CONSERVATIVE"
    ).all()
    normalized = normalize_databento_opra_option_snapshot(
        evidence.quotes,
        evidence.definitions,
        symbol=TEST_SYMBOL,
        target_snapshot_for=TARGET,
        received_at=evidence.received_at,
    )
    assert set(normalized["call_put"]) == {"CALL", "PUT"}
    assert normalized["quote_timestamp"].lt(TARGET).all()
    assert normalized["provider_received_at"].eq(TARGET).all()
    assert normalized["local_received_at"].le(normalized["available_at"]).all()
    assert normalized["exercise_style_status"].eq("POINT_IN_TIME_REFERENCE").all()
    assert normalized["settlement_status"].eq("POINT_IN_TIME_REFERENCE").all()
    assert normalized["contract_semantics_source"].eq(
        "OPRA_DEFINITION_CFI_ISO10962"
    ).all()
    adapter.close()
    assert client.stopped is True


def test_live_adapter_rejects_scope_and_never_adds_spy() -> None:
    with pytest.raises(MLContractError, match="exactly " + re.escape(", ".join(PRODUCTION_OPTION_SYMBOLS))):
        DatabentoOpraLiveAdapter(
            api_key="unit-test-placeholder",
            symbols=(*PRODUCTION_OPTION_SYMBOLS, "SPY"),
            client_factory=_FakeLiveClient,
            autostart=False,
        )


def test_live_adapter_fails_closed_on_divergent_duplicate_and_corrupt_clock() -> None:
    adapter, _client = _adapter()
    contract = f"{TEST_SYMBOL:<6}260821C00100000"
    adapter.ingest_record(
        _definition(contract=contract, call_put="C", instrument_id=101)
    )
    adapter.ingest_record(_quote(instrument_id=101))
    adapter.ingest_record(_quote(instrument_id=101, ask=1.2))
    with pytest.raises(
        DatabentoOpraIntegrityError,
        match="OPRA_QUOTE_DUPLICATE_DIVERGED",
    ):
        adapter.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )

    corrupt, _client = _adapter()
    corrupt.ingest_record(
        _definition(contract=contract, call_put="C", instrument_id=101)
    )
    bad = _quote(instrument_id=101)
    bad.ts_out = (TARGET - pd.Timedelta(seconds=1)).value
    corrupt.ingest_record(bad)
    with pytest.raises(DatabentoOpraIntegrityError, match="CLOCK_REVERSED"):
        corrupt.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )

    corrupt_definition, _client = _adapter()
    bad_definition = _definition(
        contract=contract,
        call_put="C",
        instrument_id=101,
    )
    bad_definition.ts_event = bad_definition.ts_recv + pd.Timedelta(seconds=1).value
    corrupt_definition.ingest_record(bad_definition)
    with pytest.raises(
        DatabentoOpraIntegrityError,
        match="DEFINITION_EVENT_CLOCK_REVERSED",
    ):
        corrupt_definition.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )


def test_live_adapter_rejects_future_stale_crossed_and_ineligible_evidence() -> None:
    contract = f"{TEST_SYMBOL:<6}260821C00100000"
    stale, _client = _adapter(maximum_quote_staleness_seconds=30)
    stale.ingest_record(
        _definition(contract=contract, call_put="C", instrument_id=101)
    )
    stale.ingest_record(
        _quote(
            instrument_id=101,
            interval_end=TARGET - pd.Timedelta(minutes=2),
        )
    )
    # A later invalid record may advance stream progress, but cannot become or
    # revive quote evidence.
    stale.ingest_record(_quote(instrument_id=101, bid=2.0, ask=1.0))
    with pytest.raises(OptionProviderUnavailable, match="NO_VALID_PRETARGET_BBO"):
        stale.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )

    future_definition, _client = _adapter()
    future_definition.ingest_record(
        _definition(
            contract=contract,
            call_put="C",
            effective_at=TARGET + pd.Timedelta(seconds=1),
            instrument_id=101,
        )
    )
    future_definition.ingest_record(_quote(instrument_id=101))
    with pytest.raises(OptionProviderUnavailable, match="NO_ELIGIBLE_DEFINITIONS"):
        future_definition.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )

    future_activation, _client = _adapter()
    not_yet_active = _definition(
        contract=contract,
        call_put="C",
        instrument_id=101,
    )
    not_yet_active.activation = (TARGET + pd.Timedelta(seconds=1)).value
    future_activation.ingest_record(not_yet_active)
    future_activation.ingest_record(_quote(instrument_id=101))
    with pytest.raises(OptionProviderUnavailable, match="NO_ELIGIBLE_DEFINITIONS"):
        future_activation.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )

    ineligible, _client = _adapter()
    adjusted = _definition(contract=contract, call_put="C", instrument_id=101)
    adjusted.contract_multiplier = 50
    ineligible.ingest_record(adjusted)
    ineligible.ingest_record(_quote(instrument_id=101))
    with pytest.raises(OptionProviderUnavailable, match="NO_ELIGIBLE_DEFINITIONS"):
        ineligible.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )

    incomplete, _client = _adapter()
    incomplete.ingest_record(
        _definition(contract=contract, call_put="C", instrument_id=101)
    )
    incomplete.ingest_record(_quote(instrument_id=101, bid=0.0, ask=1.0))
    with pytest.raises(OptionProviderUnavailable, match="NO_VALID_PRETARGET_BBO"):
        incomplete.fetch_snapshot(
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )


def test_live_adapter_bounds_quote_buckets_and_recovers_status_after_reconnect() -> None:
    adapter, client = _adapter(
        now=TARGET + pd.Timedelta(minutes=31),
        retained_target_buckets=2,
    )
    contract = f"{TEST_SYMBOL:<6}260821C00100000"
    adapter.ingest_record(
        _definition(contract=contract, call_put="C", instrument_id=101)
    )
    for offset in (0, 15, 30):
        adapter.ingest_record(
            _quote(
                instrument_id=101,
                interval_end=TARGET + pd.Timedelta(minutes=offset),
            )
        )
    replay = _quote(
        instrument_id=101,
        interval_end=TARGET + pd.Timedelta(minutes=30),
    )
    replay.ts_out += pd.Timedelta(milliseconds=5).value
    adapter.ingest_record(replay)
    assert adapter.buffer_status()["target_buckets"] == 2
    assert client.callback_error is not None
    client.callback_error(RuntimeError("credential-bearing-provider-message"))
    assert adapter.buffer_status()["stream_status"] == "UNAVAILABLE"
    assert client.reconnect_callback is not None
    client.reconnect_callback("old", "new")
    status = adapter.buffer_status()
    assert status["stream_status"] == "READY"
    assert status["reconnects"] == 1
    assert "credential-bearing" not in str(status)


def test_live_adapter_bounds_symbol_mapping_and_definition_keys() -> None:
    adapter, _client = _adapter(maximum_definitions=1)
    first = f"{TEST_SYMBOL:<6}260821C00100000"
    second = f"{TEST_SYMBOL:<6}260821P00100000"
    adapter.ingest_record(_mapping(instrument_id=101, contract=first))
    adapter.ingest_record(_mapping(instrument_id=102, contract=second))
    status = adapter.buffer_status()
    assert status["instrument_mappings"] == 1
    assert status["stream_status"] == "UNAVAILABLE"

    definitions, _client = _adapter(maximum_definitions=1)
    definitions.ingest_record(
        _definition(contract=first, call_put="C", instrument_id=101)
    )
    definitions.ingest_record(
        _definition(contract=second, call_put="P", instrument_id=102)
    )
    status = definitions.buffer_status()
    assert status["definition_records"] == 1
    assert status["instrument_mappings"] == 1
    assert status["stream_status"] == "UNAVAILABLE"


def test_normalizer_validates_identity_duplicates_and_clock_ordering() -> None:
    adapter, _client = _adapter()
    evidence = _complete_evidence(adapter)
    divergent = pd.concat(
        [evidence.quotes.iloc[:1], evidence.quotes.iloc[:1]],
        ignore_index=True,
    )
    divergent.loc[1, "ask"] = 9.0
    with pytest.raises(ValueError, match="Divergent duplicate"):
        normalize_databento_opra_option_snapshot(
            divergent,
            evidence.definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )
    mismatched = evidence.quotes.copy()
    mismatched["dataset"] = "NOT.OPRA"
    with pytest.raises(ValueError, match="mismatched dataset"):
        normalize_databento_opra_option_snapshot(
            mismatched,
            evidence.definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )
    future_local = evidence.quotes.copy()
    future_local["local_received_at"] = evidence.received_at + pd.Timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="no contracts"):
        normalize_databento_opra_option_snapshot(
            future_local,
            evidence.definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )

    incomplete = evidence.quotes.drop(columns="provider_received_at")
    with pytest.raises(ValueError, match="quotes are missing"):
        normalize_databento_opra_option_snapshot(
            incomplete,
            evidence.definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )

    future_market = evidence.quotes.copy()
    future_market["market_event_timestamp"] = TARGET + pd.Timedelta(milliseconds=1)
    with pytest.raises(RuntimeError, match="no contracts"):
        normalize_databento_opra_option_snapshot(
            future_market,
            evidence.definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )

    future_activation = evidence.definitions.copy()
    future_activation["definition_activation_at"] = TARGET + pd.Timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="no contracts"):
        normalize_databento_opra_option_snapshot(
            evidence.quotes,
            future_activation,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )


@pytest.mark.parametrize(
    ("column", "invalid"),
    (
        ("provider", "schwab"),
        ("dataset", "NOT.OPRA"),
        ("source_schema", "cbbo-1m"),
        ("symbol", "SPY"),
        ("target_snapshot_for", TARGET + pd.Timedelta(minutes=15)),
    ),
)
def test_normalizer_rejects_each_quote_identity_mismatch(
    column: str,
    invalid: object,
) -> None:
    adapter, _client = _adapter()
    evidence = _complete_evidence(adapter)
    mismatched = evidence.quotes.copy()
    mismatched[column] = invalid

    with pytest.raises(ValueError, match="mismatched"):
        normalize_databento_opra_option_snapshot(
            mismatched,
            evidence.definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )


@pytest.mark.parametrize("frame_name", ("quotes", "definitions"))
@pytest.mark.parametrize(
    "column",
    ("provider", "dataset", "source_schema", "symbol", "target_snapshot_for"),
)
def test_normalizer_rejects_missing_identity_values(
    frame_name: str,
    column: str,
) -> None:
    adapter, _client = _adapter()
    evidence = _complete_evidence(adapter)
    quotes = evidence.quotes.copy()
    definitions = evidence.definitions.copy()
    selected = quotes if frame_name == "quotes" else definitions
    selected[column] = pd.NA

    with pytest.raises(ValueError, match="mismatched"):
        normalize_databento_opra_option_snapshot(
            quotes,
            definitions,
            symbol=TEST_SYMBOL,
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )


def test_options_cli_constructs_and_injects_live_adapter(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Adapter:
        provider = "databento-opra"
        dataset = "OPRA.PILLAR"
        schema = "cbbo-1s"

        def __init__(self, *, api_key: str, symbols: object) -> None:
            captured["key_present"] = bool(api_key)
            captured["symbols"] = tuple(symbols)  # type: ignore[arg-type]

        def close(self) -> None:
            captured["closed"] = True

    def run(*_args: object, **kwargs: object) -> OptionsCycleResult:
        captured["adapter"] = kwargs["canonical_market_adapter"]
        return OptionsCycleResult(published=0, failed=0, skipped=6)

    monkeypatch.setattr(options_runtime, "load_repository_environment", lambda: False)
    monkeypatch.setenv("DATABENTO_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr(options_runtime, "DatabentoOpraLiveAdapter", Adapter)
    monkeypatch.setattr(options_runtime, "run_options_cycle", run)
    result = options_runtime.main(
        [
            "--symbols",
            *PRODUCTION_OPTION_SYMBOLS,
            "--datastore",
            str(tmp_path),
            "--once",
            "--provider-mode",
            "opra-canonical",
        ]
    )
    assert result == 0
    assert captured["key_present"] is True
    assert captured["symbols"] == PRODUCTION_OPTION_SYMBOLS
    assert captured["adapter"].provider == "databento-opra"  # type: ignore[union-attr]
    assert captured["closed"] is True


def test_options_cli_captures_before_slow_history_and_lets_calendar_own_target(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    order: list[str] = []
    clock_values = iter(
        (
            datetime(2026, 8, 18, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 18, 19, 20, tzinfo=timezone.utc),
        )
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            value = next(clock_values)
            return value if tz is not None else value.replace(tzinfo=None)

    class Adapter:
        provider = "databento-opra"
        dataset = "OPRA.PILLAR"
        schema = "cbbo-1s"

        def __init__(self, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            captured["closed"] = True

    def next_cycle(now: datetime, **_kwargs: object) -> datetime:
        captured["scheduled_from"] = now
        return now + timedelta(minutes=1)

    def run(*_args: object, **kwargs: object) -> OptionsCycleResult:
        order.append("cycle")
        captured["cycle_kwargs"] = kwargs
        return OptionsCycleResult(published=1, failed=0, skipped=0)

    def catchup(*_args: object, **_kwargs: object) -> None:
        order.append("history")
        raise KeyboardInterrupt

    monkeypatch.setattr(options_runtime, "datetime", Clock)
    monkeypatch.setattr(options_runtime, "load_repository_environment", lambda: False)
    monkeypatch.setenv("DATABENTO_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr(options_runtime, "DatabentoOpraLiveAdapter", Adapter)
    monkeypatch.setattr(
        options_runtime,
        "synchronize_option_history",
        catchup,
    )
    monkeypatch.setattr(options_runtime, "next_boundary", next_cycle)
    monkeypatch.setattr(
        options_runtime,
        "_wait_for_options_boundary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(options_runtime, "run_options_cycle", run)

    result = options_runtime.main(
        [
            "--symbols",
            *PRODUCTION_OPTION_SYMBOLS,
            "--datastore",
            str(tmp_path),
            "--provider-mode",
            "opra-canonical",
        ]
    )

    assert result == 0
    assert captured["scheduled_from"] == datetime(
        2026, 8, 18, 16, 0, tzinfo=timezone.utc
    )
    assert order == ["cycle", "history"]
    assert "target_snapshot_for" not in captured["cycle_kwargs"]
    assert captured["closed"] is True


def test_options_cli_requires_explicit_compatibility_mode_to_disable_opra(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    monkeypatch.setattr(
        options_runtime,
        "load_repository_environment",
        lambda: pytest.fail("compatibility mode must not load OPRA configuration"),
    )
    monkeypatch.setattr(
        options_runtime,
        "DatabentoOpraLiveAdapter",
        lambda **_kwargs: pytest.fail("compatibility mode must not construct OPRA"),
    )

    def run(*_args: object, **kwargs: object) -> OptionsCycleResult:
        captured["adapter"] = kwargs["canonical_market_adapter"]
        return OptionsCycleResult(published=0, failed=0, skipped=6)

    monkeypatch.setattr(options_runtime, "run_options_cycle", run)
    result = options_runtime.main(
        [
            "--symbols",
            *PRODUCTION_OPTION_SYMBOLS,
            "--datastore",
            str(tmp_path),
            "--once",
            "--provider-mode",
            "schwab-only-compatibility",
        ]
    )

    assert result == 0
    assert captured["adapter"] is None


def test_options_cli_fails_before_cycle_when_opra_configuration_is_missing(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(options_runtime, "load_repository_environment", lambda: False)
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    monkeypatch.setattr(
        options_runtime,
        "run_options_cycle",
        lambda *_args, **_kwargs: pytest.fail("cycle must not start"),
    )
    with pytest.raises(SystemExit):
        options_runtime.main(
            [
                "--symbols",
                *PRODUCTION_OPTION_SYMBOLS,
                "--datastore",
                str(tmp_path),
                "--once",
            ]
        )
    stderr = capsys.readouterr().err
    assert "DATABENTO_API_KEY is required" in stderr
    assert "Options Capture was not started" in stderr


def test_options_cli_sanitizes_adapter_startup_exception(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(options_runtime, "load_repository_environment", lambda: False)
    monkeypatch.setenv("DATABENTO_API_KEY", "unit-test-placeholder")

    def fail(**_kwargs: object) -> object:
        raise RuntimeError("unit-test-placeholder must never be printed")

    monkeypatch.setattr(options_runtime, "DatabentoOpraLiveAdapter", fail)
    with pytest.raises(SystemExit):
        options_runtime.main(
            [
                "--symbols",
                *PRODUCTION_OPTION_SYMBOLS,
                "--datastore",
                str(tmp_path),
                "--once",
            ]
        )
    stderr = capsys.readouterr().err
    assert "could not be constructed" in stderr
    assert "unit-test-placeholder" not in stderr


def test_options_history_uses_schema_specific_bootstrap_and_overlap_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes: list[options_runtime.SyncScope] = []
    health_refreshes: list[Path] = []
    entitlement = {
        "entitlements": {
            schema: {"entitled_end": "2026-08-15"}
            for schema in options_runtime.STANDARD_SCHEMAS
        }
    }

    monkeypatch.setattr("databento.Historical", lambda _key: object())
    monkeypatch.setattr(
        options_runtime,
        "discover_standard_entitlement",
        lambda *_args, **_kwargs: entitlement,
    )

    def synchronize(*_args: object, **kwargs: object) -> object:
        assert kwargs["refresh_health"] is False
        scopes.append(kwargs["scope"])  # type: ignore[arg-type]
        return SimpleNamespace(
            status="COMPLETE",
            completed_partitions=1,
            skipped_partitions=0,
            completed_rows=10,
            errors={},
            health_path=tmp_path / "health.json",
        )

    monkeypatch.setattr(options_runtime, "synchronize", synchronize)
    monkeypatch.setattr(
        options_runtime,
        "publish_health",
        lambda root: health_refreshes.append(Path(root)) or tmp_path / "health.json",
    )
    store = SimpleNamespace(root_dir=tmp_path)
    options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        store,
        api_key="unit-test-placeholder",
        symbols=("GOOG", "NVDA"),
        reporter=None,
    )

    assert len(scopes) == 2 * len(options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER)
    assert {scope.symbols for scope in scopes} == {
        ("GOOG.OPT",),
        ("NVDA.OPT",),
    }
    assert {
        scope.schemas[0]: scope.start for scope in scopes
    } == {
        "definition": "2026-05-07",
        "ohlcv-1d": "2019-08-17",
        "ohlcv-1h": "2021-08-16",
        "ohlcv-1m": "2026-05-07",
        "ohlcv-1s": "2026-08-05",
        "status": "2026-07-15",
        "statistics": "2026-07-15",
        "trades": "2026-07-15",
        "tcbbo": "2026-07-15",
        "cbbo-1m": "2026-07-26",
        "cbbo-1s": "2026-08-14",
    }
    assert {scope.end for scope in scopes} == {"2026-08-15"}
    assert set(options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER) == set(
        options_runtime.STANDARD_SCHEMAS
    ) - {"cmbp-1"}
    assert sorted(scope.schemas[0] for scope in scopes) == sorted(
        options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER * 2
    )
    assert health_refreshes == [tmp_path]

    scopes.clear()
    options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        store,
        api_key="unit-test-placeholder",
        symbols=("GOOG", "NVDA"),
        reporter=None,
    )
    assert {
        scope.schemas[0]: scope.start for scope in scopes
    } == {
        **{
            schema: "2026-08-12"
            for schema in options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER
            if not schema.startswith("ohlcv-") and schema != "cbbo-1s"
        },
        "cbbo-1s": "2026-08-14",
        "ohlcv-1s": "2026-08-14",
        "ohlcv-1m": "2026-08-13",
        "ohlcv-1h": "2026-08-10",
        "ohlcv-1d": "2026-08-05",
    }
    assert health_refreshes == [tmp_path, tmp_path]

    assert options_runtime.opra_history_overlap_days("ohlcv-1s") == 1
    assert options_runtime.opra_history_overlap_days("ohlcv-1m") == 2
    assert options_runtime.opra_history_overlap_days("ohlcv-1h") == 5
    assert options_runtime.opra_history_overlap_days("ohlcv-1d") == 10
    assert options_runtime.opra_history_overlap_days("cmbp-1") == 3


def test_legacy_v4_cursor_compatibility_is_limited_to_former_policy(
    tmp_path: Path,
) -> None:
    path = options_runtime._opra_symbol_history_cursor_path(
        tmp_path,
        symbol="NVDA",
        schema="ohlcv-1d",
    )
    path.parent.mkdir(parents=True)
    legacy = {
        "schema_version": options_runtime.OPRA_LEGACY_SYMBOL_HISTORY_CURSOR_VERSION,
        "symbol": "NVDA",
        "provider_symbol": "NVDA.OPT",
        "schema": "ohlcv-1d",
        "completed_through": "2026-08-15",
        "lookback_policy": {"unit": "days", "value": 5000},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    assert options_runtime._read_opra_symbol_history_cursor(
        tmp_path,
        symbol="NVDA",
        schema="ohlcv-1d",
    ) == legacy

    legacy["lookback_policy"] = {"unit": "years", "value": 13}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert (
        options_runtime._read_opra_symbol_history_cursor(
            tmp_path,
            symbol="NVDA",
            schema="ohlcv-1d",
        )
        is None
    )


def test_options_history_capacity_block_is_isolated_per_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []
    entitlement = {
        "entitlements": {
            schema: {"entitled_end": "2026-08-15"}
            for schema in options_runtime.STANDARD_SCHEMAS
        }
    }
    monkeypatch.setattr("databento.Historical", lambda _key: object())
    monkeypatch.setattr(
        options_runtime,
        "discover_standard_entitlement",
        lambda *_args, **_kwargs: entitlement,
    )

    def synchronize(*_args: object, **kwargs: object) -> object:
        scope = kwargs["scope"]
        attempted.append(scope.symbols[0])
        if scope.symbols == ("GOOG.OPT",):
            raise options_runtime.OpraCapacityError("blocked")
        return SimpleNamespace(
            status="COMPLETE",
            completed_partitions=1,
            skipped_partitions=0,
            completed_rows=10,
            errors={},
            health_path=tmp_path / "health.json",
        )

    monkeypatch.setattr(options_runtime, "synchronize", synchronize)
    store = SimpleNamespace(root_dir=tmp_path)
    options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        store,
        api_key="unit-test-placeholder",
        symbols=("GOOG", "NVDA"),
        reporter=None,
    )

    assert attempted == [
        provider_symbol
        for _schema in options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER
        for provider_symbol in ("GOOG.OPT", "NVDA.OPT")
    ]
    for schema in options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER:
        assert not options_runtime._opra_symbol_history_cursor_path(
            tmp_path,
            symbol="GOOG",
            schema=schema,
        ).exists()
        assert options_runtime._opra_symbol_history_cursor_path(
            tmp_path,
            symbol="NVDA",
            schema=schema,
        ).is_file()


@pytest.mark.parametrize("preflight_only", (False, True))
@pytest.mark.parametrize(
    "limits",
    (
        {"max_estimated_download_bytes": 15, "max_estimated_cost_usd": 1.0},
        {"max_estimated_download_bytes": 100, "max_estimated_cost_usd": 0.3},
    ),
)
def test_options_history_guarded_preflight_selects_scopes_within_run_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight_only: bool,
    limits: dict[str, int | float],
) -> None:
    entitlement = {
        "entitlements": {
            schema: {"entitled_start": "2013-08-15", "entitled_end": "2026-08-15"}
            for schema in options_runtime.STANDARD_SCHEMAS
        }
    }
    clients: list[object] = []
    endpoint_calls: list[tuple[str, str]] = []
    active_clients: set[int] = set()
    worker_threads: dict[int, int] = {}
    completion_order: list[str] = []
    published: list[str] = []
    synchronized: list[str] = []
    first_started, second_completed = Event(), Event()
    state_lock = Lock()
    max_active = 0

    class Metadata:
        TIMEOUT = 0

        def get_billable_size(self, **kwargs: object) -> int:
            endpoint_calls.append((kwargs["symbols"][0], "bytes"))
            return 10

        def get_record_count(self, **kwargs: object) -> int:
            endpoint_calls.append((kwargs["symbols"][0], "records"))
            return 2

        def get_cost(self, **kwargs: object) -> float:
            endpoint_calls.append((kwargs["symbols"][0], "cost"))
            return 0.25

    def make_client(_key: str) -> object:
        client = SimpleNamespace(metadata=Metadata(), timeseries=SimpleNamespace(TIMEOUT=0))
        clients.append(client)
        assert len(clients) <= 2
        return client

    def discover(client: object, **_kwargs: object) -> dict[str, object]:
        assert client is clients[0]
        options_runtime.configure_client(client)
        return entitlement

    real_preflight = options_runtime.storage_preflight

    def preflight(client: object, **kwargs: object) -> dict[str, object]:
        nonlocal max_active
        symbol = kwargs["scope"].symbols[0]
        with state_lock:
            assert id(client) not in active_clients
            assert worker_threads.setdefault(id(client), get_ident()) == get_ident()
            active_clients.add(id(client))
            max_active = max(max_active, len(active_clients))
        try:
            assert client.metadata.TIMEOUT == 30
            if symbol == "AAPL.OPT":
                first_started.set()
                assert second_completed.wait(5), "Second preflight did not run concurrently"
            elif symbol == "GOOG.OPT":
                assert first_started.wait(5)
            result = real_preflight(client, **kwargs)
        finally:
            with state_lock:
                active_clients.remove(id(client))
                completion_order.append(symbol)
            if symbol == "GOOG.OPT":
                second_completed.set()
        return result

    real_publish = options_runtime.publish_storage_preflight

    def publish(root: Path, value: dict[str, object]) -> dict[str, object]:
        assert not active_clients
        assert len(completion_order) == 3
        published.append(value["scope"]["symbols"][0])
        return real_publish(root, value)

    monkeypatch.setattr("databento.Historical", make_client)
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", discover)
    monkeypatch.setattr(options_runtime, "storage_preflight", preflight)
    monkeypatch.setattr(options_runtime, "publish_storage_preflight", publish)

    def synchronize(client: object, **kwargs: object) -> object:
        assert client is clients[0]
        assert not active_clients
        assert len(completion_order) == len(published) == 3
        scope = kwargs["scope"]
        synchronized.append(scope.symbols[0])
        assert kwargs["storage_preflight_receipt"][
            "estimated_download_size_bytes"
        ] == 10
        return SimpleNamespace(
            status="COMPLETE",
            completed_partitions=1,
            skipped_partitions=0,
            completed_rows=10,
            errors={},
            health_path=tmp_path / "health.json",
        )

    monkeypatch.setattr(options_runtime, "synchronize", synchronize)
    monkeypatch.setattr(
        options_runtime,
        "publish_health",
        lambda _root: tmp_path / "health.json",
    )

    summary = options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        SimpleNamespace(root_dir=tmp_path),
        api_key="unit-test-placeholder",
        symbols=("AAPL", "GOOG", "NVDA"),
        schemas=("definition",),
        reporter=None,
        preflight_only=preflight_only,
        **limits,
    )

    assert len(clients) == max_active == len(worker_threads) == 2
    assert completion_order.index("GOOG.OPT") < completion_order.index("AAPL.OPT")
    assert published == ["AAPL.OPT", "GOOG.OPT", "NVDA.OPT"]
    for symbol in published:
        assert [endpoint for scope, endpoint in endpoint_calls if scope == symbol] == [
            "bytes", "records", "cost",
        ]
    assert synchronized == ([] if preflight_only else ["AAPL.OPT"])
    assert summary.requested_scopes == 3
    assert summary.preflighted_scopes == 3
    assert summary.completed_scopes == (0 if preflight_only else 1)
    assert summary.failed_scopes == 0
    assert summary.deferred_scopes == 2
    assert summary.selected_estimated_download_bytes == 10
    assert summary.total_estimated_download_bytes == 30
    assert summary.selected_estimated_cost_usd == 0.25
    assert summary.total_estimated_cost_usd == 0.75


def test_options_history_parallel_preflight_preserves_failed_and_unknown_cost_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synchronized: list[str] = []
    published: list[str] = []
    messages: list[str] = []
    entitlement = {"entitlements": {"definition": {"entitled_end": "2026-08-15"}}}
    monkeypatch.setattr(
        "databento.Historical",
        lambda _key: SimpleNamespace(
            metadata=SimpleNamespace(TIMEOUT=0), timeseries=SimpleNamespace(TIMEOUT=0)
        ),
    )
    monkeypatch.setattr(options_runtime, "discover_standard_entitlement", lambda *a, **k: entitlement)

    def preflight(_client: object, **kwargs: object) -> dict[str, object]:
        symbol = kwargs["scope"].symbols[0]
        if symbol == "AAPL.OPT":
            raise RuntimeError("cbbo estimated download size failed after 3 attempts")
        if symbol == "GOOG.OPT":
            raise TypeError("provider returned unreadable estimated bytes")
        return {
            "symbol": symbol,
            "estimated_download_size_bytes": 10,
            "estimated_cost_usd": None if symbol == "NVDA.OPT" else 0.0,
        }

    def publish(_root: Path, value: dict[str, object]) -> dict[str, object]:
        published.append(value["symbol"])
        return value

    def synchronize(_client: object, **kwargs: object) -> object:
        symbol = kwargs["scope"].symbols[0]
        synchronized.append(symbol)
        return SimpleNamespace(
            status="COMPLETE", completed_partitions=1, skipped_partitions=0,
            completed_rows=1, errors={}, health_path=tmp_path / "health.json",
        )

    monkeypatch.setattr(options_runtime, "storage_preflight", preflight)
    monkeypatch.setattr(options_runtime, "publish_storage_preflight", publish)
    monkeypatch.setattr(options_runtime, "synchronize", synchronize)
    monkeypatch.setattr(options_runtime, "publish_health", lambda _root: tmp_path / "health.json")
    summary = options_runtime.synchronize_option_history(
        SimpleNamespace(root_dir=tmp_path), api_key="unit-test-placeholder",
        symbols=("AAPL", "GOOG", "NVDA", "TSLA"), schemas=("definition",),
        reporter=messages.append, max_estimated_download_bytes=100, max_estimated_cost_usd=0,
    )

    assert published == ["NVDA.OPT", "TSLA.OPT"]
    assert synchronized == ["TSLA.OPT"]
    assert summary.requested_scopes == 4
    assert summary.failed_scopes == 2
    assert summary.preflighted_scopes == 2
    assert summary.deferred_scopes == 1
    assert summary.completed_scopes == 1
    assert summary.total_estimated_cost_usd is None
    assert summary.selected_estimated_cost_usd == 0.0
    assert any("symbol=AAPL" in message and "failed after 3 attempts" in message for message in messages)
    assert any("symbol=GOOG" in message and "unreadable estimated bytes" in message for message in messages)


def test_options_history_preflight_only_never_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entitlement = {
        "entitlements": {
            schema: {"entitled_end": "2026-08-15"}
            for schema in options_runtime.STANDARD_SCHEMAS
        }
    }
    monkeypatch.setattr("databento.Historical", lambda _key: object())
    monkeypatch.setattr(
        options_runtime,
        "discover_standard_entitlement",
        lambda *_args, **_kwargs: entitlement,
    )
    monkeypatch.setattr(
        options_runtime,
        "storage_preflight",
        lambda *_args, **_kwargs: {
            "estimated_download_size_bytes": 10,
            "estimated_cost_usd": 0.25,
        },
    )
    monkeypatch.setattr(
        options_runtime,
        "publish_storage_preflight",
        lambda _root, value: value,
    )
    monkeypatch.setattr(
        options_runtime,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("preflight-only must not download"),
    )

    summary = options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        SimpleNamespace(root_dir=tmp_path),
        api_key="unit-test-placeholder",
        symbols=("AAPL",),
        schemas=("definition",),
        reporter=None,
        preflight_only=True,
        max_estimated_download_bytes=100,
        max_estimated_cost_usd=1.0,
    )

    assert summary.preflight_only is True
    assert summary.preflighted_scopes == 1
    assert summary.completed_scopes == 0
    assert summary.selected_estimated_download_bytes == 10


def test_options_history_caps_incremental_cursor_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entitlement = {
        "entitlements": {
            schema: {"entitled_end": "2026-09-03"}
            for schema in options_runtime.STANDARD_SCHEMAS
        }
    }
    scopes: list[options_runtime.SyncScope] = []
    options_runtime._write_opra_symbol_history_cursor(
        tmp_path,
        symbol="AAPL",
        schema="definition",
        completed_through="2026-08-22",
    )
    monkeypatch.setattr("databento.Historical", lambda _key: object())
    monkeypatch.setattr(
        options_runtime,
        "discover_standard_entitlement",
        lambda *_args, **_kwargs: entitlement,
    )

    def synchronize(*_args: object, **kwargs: object) -> object:
        scopes.append(kwargs["scope"])
        return SimpleNamespace(
            status="COMPLETE",
            completed_partitions=1,
            skipped_partitions=0,
            completed_rows=10,
            errors={},
            health_path=tmp_path / "health.json",
        )

    monkeypatch.setattr(options_runtime, "synchronize", synchronize)
    monkeypatch.setattr(
        options_runtime,
        "publish_health",
        lambda _root: tmp_path / "health.json",
    )

    options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        SimpleNamespace(root_dir=tmp_path),
        api_key="unit-test-placeholder",
        symbols=("AAPL",),
        schemas=("definition",),
        reporter=None,
        bootstrap_missing=False,
        max_incremental_catchup_days=2,
    )

    assert len(scopes) == 1
    assert scopes[0].start == "2026-08-19"
    assert scopes[0].end == "2026-08-24"
    cursor = options_runtime._read_opra_symbol_history_cursor(
        tmp_path,
        symbol="AAPL",
        schema="definition",
    )
    assert cursor is not None
    assert cursor["completed_through"] == "2026-08-24"


def test_options_loop_requires_one_time_bootstrap_for_missing_cursors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entitlement = {
        "entitlements": {
            schema: {"entitled_end": "2026-08-15"}
            for schema in options_runtime.STANDARD_SCHEMAS
        }
    }
    monkeypatch.setattr("databento.Historical", lambda _key: object())
    monkeypatch.setattr(
        options_runtime,
        "discover_standard_entitlement",
        lambda *_args, **_kwargs: entitlement,
    )
    monkeypatch.setattr(
        options_runtime,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail(
            "the recurring loop must not perform an initial history bootstrap"
        ),
    )
    monkeypatch.setattr(
        options_runtime,
        "publish_health",
        lambda *_args, **_kwargs: pytest.fail(
            "a pass with no eligible history scope must not rebuild global health"
        ),
    )

    summary = options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        SimpleNamespace(root_dir=tmp_path),
        api_key="unit-test-placeholder",
        symbols=("GOOG",),
        reporter=None,
        bootstrap_missing=False,
    )

    assert summary.requested_scopes == len(
        options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER
    )
    assert summary.completed_scopes == 0
    assert summary.bootstrap_required_scopes == len(
        options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER
    )
    assert summary.capacity_blocked_scopes == 0
    assert summary.failed_scopes == 0
