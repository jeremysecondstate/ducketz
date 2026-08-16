from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pandas as pd
import pytest

import datafetching.options_runtime as options_runtime
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
        underlying="AAPL",
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
    call_contract = "AAPL  260821C00100000"
    put_contract = "AAPL  260821P00100000"
    adapter.ingest_record(
        _definition(contract=call_contract, call_put="C", instrument_id=101)
    )
    adapter.ingest_record(
        _definition(contract=put_contract, call_put="P", instrument_id=102)
    )
    adapter.ingest_record(_quote(instrument_id=101))
    adapter.ingest_record(_quote(instrument_id=102, bid=1.2, ask=1.3))
    return adapter.fetch_snapshot(
        symbol="AAPL",
        target_snapshot_for=TARGET,
        requested_at=TARGET + pd.Timedelta(seconds=1),
    )


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
        symbol="AAPL",
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
    with pytest.raises(MLContractError, match="exactly AAPL"):
        DatabentoOpraLiveAdapter(
            api_key="unit-test-placeholder",
            symbols=(*PRODUCTION_OPTION_SYMBOLS, "SPY"),
            client_factory=_FakeLiveClient,
            autostart=False,
        )


def test_live_adapter_fails_closed_on_divergent_duplicate_and_corrupt_clock() -> None:
    adapter, _client = _adapter()
    contract = "AAPL  260821C00100000"
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
            symbol="AAPL",
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
            symbol="AAPL",
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
            symbol="AAPL",
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )


def test_live_adapter_rejects_future_stale_crossed_and_ineligible_evidence() -> None:
    contract = "AAPL  260821C00100000"
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
            symbol="AAPL",
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
            symbol="AAPL",
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
            symbol="AAPL",
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
            symbol="AAPL",
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
            symbol="AAPL",
            target_snapshot_for=TARGET,
            requested_at=TARGET + pd.Timedelta(seconds=1),
        )


def test_live_adapter_bounds_quote_buckets_and_recovers_status_after_reconnect() -> None:
    adapter, client = _adapter(
        now=TARGET + pd.Timedelta(minutes=31),
        retained_target_buckets=2,
    )
    contract = "AAPL  260821C00100000"
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
    first = "AAPL  260821C00100000"
    second = "AAPL  260821P00100000"
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
            symbol="AAPL",
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )
    mismatched = evidence.quotes.copy()
    mismatched["dataset"] = "NOT.OPRA"
    with pytest.raises(ValueError, match="mismatched dataset"):
        normalize_databento_opra_option_snapshot(
            mismatched,
            evidence.definitions,
            symbol="AAPL",
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )
    future_local = evidence.quotes.copy()
    future_local["local_received_at"] = evidence.received_at + pd.Timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="no contracts"):
        normalize_databento_opra_option_snapshot(
            future_local,
            evidence.definitions,
            symbol="AAPL",
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )

    incomplete = evidence.quotes.drop(columns="provider_received_at")
    with pytest.raises(ValueError, match="quotes are missing"):
        normalize_databento_opra_option_snapshot(
            incomplete,
            evidence.definitions,
            symbol="AAPL",
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )

    future_market = evidence.quotes.copy()
    future_market["market_event_timestamp"] = TARGET + pd.Timedelta(milliseconds=1)
    with pytest.raises(RuntimeError, match="no contracts"):
        normalize_databento_opra_option_snapshot(
            future_market,
            evidence.definitions,
            symbol="AAPL",
            target_snapshot_for=TARGET,
            received_at=evidence.received_at,
        )

    future_activation = evidence.definitions.copy()
    future_activation["definition_activation_at"] = TARGET + pd.Timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="no contracts"):
        normalize_databento_opra_option_snapshot(
            evidence.quotes,
            future_activation,
            symbol="AAPL",
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
            symbol="AAPL",
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
            symbol="AAPL",
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
    store = SimpleNamespace(root_dir=tmp_path)
    options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        store,
        api_key="unit-test-placeholder",
        symbols=("GOOG", "NVDA"),
        reporter=None,
    )

    assert len(scopes) == 2 * len(options_runtime.STANDARD_SCHEMAS)
    assert {scope.symbols for scope in scopes} == {
        ("GOOG.OPT",),
        ("NVDA.OPT",),
    }
    assert {
        scope.schemas[0]: scope.start for scope in scopes
    } == {
        "definition": "2013-08-15",
        "ohlcv-1d": "2019-08-17",
        "ohlcv-1h": "2021-08-16",
        "ohlcv-1m": "2026-05-07",
        "ohlcv-1s": "2026-08-10",
        "status": "2026-07-15",
        "statistics": "2026-07-15",
        "trades": "2026-07-15",
        "tcbbo": "2026-07-15",
        "cbbo-1m": "2026-05-07",
        "cbbo-1s": "2026-08-10",
        "cmbp-1": "2026-07-15",
    }
    assert {scope.end for scope in scopes} == {"2026-08-15"}
    assert set(options_runtime.OPRA_SYMBOL_HISTORY_SCHEMA_ORDER) == set(
        options_runtime.STANDARD_SCHEMAS
    )
    assert sorted(scope.schemas[0] for scope in scopes) == sorted(
        options_runtime.STANDARD_SCHEMAS * 2
    )

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
            for schema in options_runtime.STANDARD_SCHEMAS
            if not schema.startswith("ohlcv-")
        },
        "ohlcv-1s": "2026-08-14",
        "ohlcv-1m": "2026-08-13",
        "ohlcv-1h": "2026-08-10",
        "ohlcv-1d": "2026-08-05",
    }

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
    for schema in options_runtime.STANDARD_SCHEMAS:
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

    summary = options_runtime.synchronize_option_history(  # type: ignore[arg-type]
        SimpleNamespace(root_dir=tmp_path),
        api_key="unit-test-placeholder",
        symbols=("GOOG",),
        reporter=None,
        bootstrap_missing=False,
    )

    assert summary.requested_scopes == len(options_runtime.STANDARD_SCHEMAS)
    assert summary.completed_scopes == 0
    assert summary.bootstrap_required_scopes == len(
        options_runtime.STANDARD_SCHEMAS
    )
    assert summary.capacity_blocked_scopes == 0
    assert summary.failed_scopes == 0
