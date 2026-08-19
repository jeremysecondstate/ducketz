from __future__ import annotations

import math
import numbers
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

import pandas as pd

from ml.universe import canonical_production_option_symbols
from options.providers import (
    OptionProviderUnavailable,
    ProviderOptionEvidence,
)


OPRA_PROVIDER = "databento-opra"
OPRA_DATASET = "OPRA.PILLAR"
OPRA_LIVE_SCHEMA = "cbbo-1s"
OPRA_DEFINITION_SCHEMA = "definition"
OPRA_CONSOLIDATED_PUBLISHER_ID = 30
OPRA_PARENT_SUFFIX = ".OPT"
OPRA_PRICE_SCALE = 1_000_000_000
OPRA_CALLBACK_YIELD_INTERVAL_SECONDS = 0.01


class DatabentoOpraIntegrityError(RuntimeError):
    """Buffered OPRA evidence is internally inconsistent and cannot fall back."""


class DatabentoOpraLiveAdapter:
    """One bounded, reconnecting OPRA live transport owned by Options Capture.

    The live connection is shared by every production symbol and target.  Target
    reads are local buffer selections; ``fetch_snapshot`` never creates another
    Databento request.  Definitions and sampled consolidated BBOs remain separate
    point-in-time records until a target is requested.
    """

    provider = OPRA_PROVIDER
    dataset = OPRA_DATASET
    schema = OPRA_LIVE_SCHEMA

    def __init__(
        self,
        *,
        api_key: str,
        symbols: Sequence[str],
        clock: Callable[[], datetime] | None = None,
        client_factory: Callable[..., object] | None = None,
        quote_replay_minutes: int = 30,
        maximum_quote_staleness_seconds: int = 20 * 60,
        retained_target_buckets: int = 8,
        maximum_definitions: int = 250_000,
        maximum_contracts_per_bucket: int = 100_000,
        snapshot_wait_seconds: float = 5.0,
        require_target_watermark: bool = True,
        autostart: bool = True,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("DATABENTO_API_KEY is required for canonical OPRA mode")
        self._symbols = canonical_production_option_symbols(
            symbols,
            label="Databento OPRA live subscription scope",
        )
        if quote_replay_minutes < 1 or quote_replay_minutes > 24 * 60:
            raise ValueError("quote_replay_minutes must be between 1 and 1440")
        if maximum_quote_staleness_seconds < 0:
            raise ValueError("maximum_quote_staleness_seconds cannot be negative")
        if retained_target_buckets < 2:
            raise ValueError("retained_target_buckets must be at least two")
        if maximum_definitions < 1 or maximum_contracts_per_bucket < 1:
            raise ValueError("OPRA live buffer capacities must be positive")
        if snapshot_wait_seconds < 0.0 or snapshot_wait_seconds > 60.0:
            raise ValueError("snapshot_wait_seconds must be between zero and 60")

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._quote_replay_minutes = int(quote_replay_minutes)
        self._maximum_quote_staleness_seconds = int(
            maximum_quote_staleness_seconds
        )
        self._retained_target_buckets = int(retained_target_buckets)
        self._maximum_definitions = int(maximum_definitions)
        self._maximum_contracts_per_bucket = int(maximum_contracts_per_bucket)
        self._snapshot_wait_seconds = float(snapshot_wait_seconds)
        self._require_target_watermark = bool(require_target_watermark)
        self._parents = tuple(
            f"{symbol}{OPRA_PARENT_SUFFIX}" for symbol in self._symbols
        )

        self._condition = threading.Condition(threading.RLock())
        self._definitions: dict[str, dict[pd.Timestamp, dict[str, object]]] = {}
        self._definition_count = 0
        self._instrument_symbols: dict[int, str] = {}
        self._quote_buckets: OrderedDict[
            pd.Timestamp, dict[str, dict[str, object]]
        ] = OrderedDict()
        self._watermarks: dict[str, pd.Timestamp] = {}
        self._stream_unavailable_reason: str | None = None
        self._integrity_error: str | None = None
        self._callback_failures = 0
        self._reconnects = 0
        self._last_callback_yield_at = time.monotonic()
        self._started = False
        self._closed = False
        self._client: object | None = None

        if client_factory is None:
            import databento as db

            client_factory = db.Live
            compression: object = db.Compression.ZSTD
        else:
            compression = "zstd"
        try:
            # The key is handed directly to the SDK and is deliberately not
            # copied into adapter fields, included in exceptions, or serialized.
            self._client = client_factory(
                key=api_key,
                ts_out=True,
                reconnect_policy="reconnect",
                slow_reader_behavior="warn",
                compression=compression,
            )
        except Exception:
            raise OptionProviderUnavailable("OPRA_LIVE_CLIENT_CONSTRUCTION_FAILED") from None
        if autostart:
            self.start()

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def parent_symbols(self) -> tuple[str, ...]:
        return self._parents

    def start(self) -> None:
        """Start the two subscriptions once; any startup failure is explicit."""

        with self._condition:
            if self._closed:
                raise RuntimeError("A closed OPRA adapter cannot be restarted")
            if self._started:
                return
            client = self._client
        if client is None:
            raise OptionProviderUnavailable("OPRA_LIVE_CLIENT_UNAVAILABLE")
        try:
            client.add_callback(self.ingest_record, self._record_callback_failure)
            client.add_reconnect_callback(
                self._record_reconnect,
                self._record_callback_failure,
            )
            # Live intraday replay is bounded by the service (at most 24 hours).
            # Definition replay uses all available intraday records for only the
            # six production parents so contracts listed before process start are
            # not silently omitted.  Quote recovery remains a much smaller window.
            client.subscribe(
                dataset=self.dataset,
                schema=OPRA_DEFINITION_SCHEMA,
                symbols=self._parents,
                stype_in="parent",
                start=0,
            )
            quote_start = _utc(self._clock()) - pd.Timedelta(
                minutes=self._quote_replay_minutes
            )
            client.subscribe(
                dataset=self.dataset,
                schema=self.schema,
                symbols=self._parents,
                stype_in="parent",
                start=quote_start,
            )
            client.start()
        except Exception:
            self._terminate_client()
            raise OptionProviderUnavailable("OPRA_LIVE_SUBSCRIPTION_START_FAILED") from None
        with self._condition:
            self._started = True
            self._condition.notify_all()

    def close(self) -> None:
        """Stop the owned live session without exposing SDK exception content."""

        with self._condition:
            if self._closed:
                return
            self._closed = True
            client = self._client
            started = self._started
            self._condition.notify_all()
        if client is None:
            return
        try:
            if started:
                client.stop()
                block = getattr(client, "block_for_close", None)
                if callable(block):
                    block(timeout=5.0)
        except Exception:
            self._terminate_client()

    def __enter__(self) -> DatabentoOpraLiveAdapter:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def buffer_status(self) -> Mapping[str, object]:
        """Return credential-free bounded-buffer diagnostics."""

        with self._condition:
            return {
                "provider": self.provider,
                "dataset": self.dataset,
                "schema": self.schema,
                "symbols": self._symbols,
                "parent_symbols": self._parents,
                "started": self._started,
                "closed": self._closed,
                "definition_records": self._definition_count,
                "instrument_mappings": len(self._instrument_symbols),
                "target_buckets": len(self._quote_buckets),
                "buffered_contracts": sum(
                    len(bucket) for bucket in self._quote_buckets.values()
                ),
                "reconnects": self._reconnects,
                "callback_failures": self._callback_failures,
                "stream_status": (
                    "INTEGRITY_ERROR"
                    if self._integrity_error is not None
                    else "UNAVAILABLE"
                    if self._stream_unavailable_reason is not None
                    else "READY"
                ),
            }

    def ingest_record(self, record: object) -> None:
        """SDK callback that performs only bounded in-memory normalization."""

        # The SDK dispatches dense replay bursts from one background thread. On
        # CPython those callbacks can otherwise retain the GIL long enough to
        # starve the runtime thread that must publish an explicit fallback.
        callback_at = time.monotonic()
        if (
            callback_at - self._last_callback_yield_at
            >= OPRA_CALLBACK_YIELD_INTERVAL_SECONDS
        ):
            self._last_callback_yield_at = callback_at
            time.sleep(0)
        received = _utc(self._clock())
        rtype = _record_type(record)
        try:
            if rtype == "error":
                with self._condition:
                    self._stream_unavailable_reason = "OPRA_STREAM_ERROR_RECORD"
                    self._condition.notify_all()
                return
            if rtype == "symbol-mapping":
                self._ingest_symbol_mapping(record)
                return
            if rtype == "instrument-def":
                self._ingest_definition(record, local_received_at=received)
                return
            if rtype == self.schema:
                self._ingest_quote(record, local_received_at=received)
        except DatabentoOpraIntegrityError as exc:
            with self._condition:
                self._integrity_error = str(exc)
                self._condition.notify_all()
        except Exception:
            self._record_callback_failure(RuntimeError("OPRA_RECORD_NORMALIZATION_FAILED"))

    def fetch_snapshot(
        self,
        *,
        symbol: str,
        target_snapshot_for: pd.Timestamp,
        requested_at: pd.Timestamp,
    ) -> ProviderOptionEvidence:
        """Select the final valid BBO strictly before one target from the buffer."""

        clean_symbol = str(symbol).strip().upper()
        if clean_symbol not in self._symbols:
            raise ValueError("OPRA target symbol is outside the production scope")
        target = _utc(target_snapshot_for)
        requested = _utc(requested_at)
        if requested < target:
            raise ValueError("OPRA target cannot be requested before the target clock")
        if target.second or target.microsecond or target.minute % 15:
            raise ValueError("OPRA target must be an exact UTC quarter-hour")

        deadline = time.monotonic() + self._snapshot_wait_seconds
        with self._condition:
            while self._require_target_watermark and (
                self._watermarks.get(clean_symbol) is None
                or self._watermarks[clean_symbol] < target
            ):
                self._raise_buffer_error_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._condition.wait(timeout=remaining)
            self._raise_buffer_error_locked()
            if self._closed or not self._started:
                raise OptionProviderUnavailable("OPRA_LIVE_TRANSPORT_NOT_RUNNING")
            watermark = self._watermarks.get(clean_symbol)
            if self._require_target_watermark and (
                watermark is None or watermark < target
            ):
                raise OptionProviderUnavailable("OPRA_TARGET_WATERMARK_UNAVAILABLE")

            selection_at = max(requested, _utc(self._clock()))
            earliest = target - pd.Timedelta(
                seconds=self._maximum_quote_staleness_seconds
            )
            selected_quotes: dict[str, dict[str, object]] = {}
            for bucket_target, bucket in self._quote_buckets.items():
                if bucket_target > target or bucket_target < earliest.floor("15min"):
                    continue
                for contract_symbol, quote in bucket.items():
                    if str(quote.get("symbol")) != clean_symbol:
                        continue
                    quote_time = _utc(quote["quote_timestamp"])
                    local_received = _utc(quote["local_received_at"])
                    if not earliest <= quote_time < target or local_received > selection_at:
                        continue
                    prior = selected_quotes.get(contract_symbol)
                    if prior is None or _utc(prior["quote_timestamp"]) < quote_time:
                        selected_quotes[contract_symbol] = dict(quote)

            if not selected_quotes:
                raise OptionProviderUnavailable(
                    "OPRA_TARGET_HAS_NO_VALID_PRETARGET_BBO"
                )
            selected_definitions: list[dict[str, object]] = []
            for contract_symbol, quote in selected_quotes.items():
                versions = self._definitions.get(contract_symbol, {})
                effective = [clock for clock in versions if clock <= target]
                if not effective:
                    continue
                definition = dict(versions[max(effective)])
                activation = pd.to_datetime(
                    definition.get("definition_activation_at"),
                    utc=True,
                    errors="coerce",
                )
                if (
                    not bool(definition.get("definition_active"))
                    or definition.get("symbol") != clean_symbol
                    or not bool(definition.get("standard_contract"))
                    or str(definition.get("call_put")) not in {"CALL", "PUT"}
                    or pd.isna(activation)
                    or pd.Timestamp(activation) > target
                    or not math.isclose(
                        float(definition.get("multiplier", math.nan)),
                        100.0,
                    )
                ):
                    continue
                expiration = pd.to_datetime(
                    definition.get("expiration_date"), utc=True, errors="coerce"
                )
                if pd.isna(expiration) or pd.Timestamp(expiration) <= target.normalize():
                    continue
                selected_definitions.append(definition)

            if not selected_definitions:
                raise OptionProviderUnavailable("OPRA_TARGET_HAS_NO_ELIGIBLE_DEFINITIONS")
            eligible_contracts = {
                str(row["contract_symbol"]) for row in selected_definitions
            }
            quote_rows = [
                {**row, "target_snapshot_for": target}
                for contract_symbol, row in selected_quotes.items()
                if contract_symbol in eligible_contracts
            ]
            if not quote_rows:
                raise OptionProviderUnavailable("OPRA_TARGET_HAS_NO_VALID_PRETARGET_BBO")
            selected_definitions = [
                {**row, "target_snapshot_for": target}
                for row in selected_definitions
            ]
            maximum_local_receipt = max(
                _utc(
                    row.get(
                        "local_received_at",
                        row.get("definition_local_received_at"),
                    )
                )
                for row in (*quote_rows, *selected_definitions)
            )

        receipt = max(selection_at, maximum_local_receipt, target)
        return ProviderOptionEvidence(
            provider=self.provider,
            dataset=self.dataset,
            schema=self.schema,
            symbol=clean_symbol,
            target_snapshot_for=target,
            received_at=receipt,
            quotes=pd.DataFrame(quote_rows),
            definitions=pd.DataFrame(selected_definitions),
        )

    def _ingest_symbol_mapping(self, record: object) -> None:
        contract = str(
            _first_value(record, "stype_out_symbol", "raw_symbol") or ""
        ).strip()
        instrument_id = _integer(_first_value(record, "instrument_id"))
        symbol = _occ_underlying(contract)
        if instrument_id is None or symbol not in self._symbols:
            return
        with self._condition:
            if not self._remember_instrument_locked(instrument_id, contract):
                return
            self._condition.notify_all()

    def _ingest_definition(
        self,
        record: object,
        *,
        local_received_at: pd.Timestamp,
    ) -> None:
        contract = str(_first_value(record, "raw_symbol") or "").strip()
        symbol = str(_first_value(record, "underlying") or "").strip().upper()
        symbol = symbol if symbol in self._symbols else _occ_underlying(contract)
        if symbol not in self._symbols or not contract:
            return
        instrument_id = _integer(_first_value(record, "instrument_id"))
        provider_received = _record_timestamp(record, "ts_recv", "ts_event")
        market_event = _record_timestamp(record, "ts_event", "ts_recv")
        provider_sent = _record_timestamp(record, "ts_out")
        effective = provider_received
        activation = _record_timestamp(record, "activation")
        if effective is None or market_event is None:
            raise DatabentoOpraIntegrityError("OPRA_DEFINITION_CLOCK_MISSING")
        if market_event > provider_received:
            raise DatabentoOpraIntegrityError("OPRA_DEFINITION_EVENT_CLOCK_REVERSED")
        if provider_sent is not None and provider_sent < provider_received:
            raise DatabentoOpraIntegrityError("OPRA_DEFINITION_PROVIDER_CLOCK_REVERSED")
        if local_received_at + pd.Timedelta(seconds=5) < (
            provider_sent or provider_received
        ):
            raise DatabentoOpraIntegrityError("OPRA_DEFINITION_LOCAL_CLOCK_REVERSED")

        expiration = _record_timestamp(record, "pretty_expiration", "expiration")
        strike = _pretty_number(record, "pretty_strike_price")
        if strike is None:
            strike = _fixed_price(_first_value(record, "strike_price"))
        multiplier = _fixed_quantity(_first_value(record, "contract_multiplier"))
        if multiplier is None or multiplier <= 0.0:
            multiplier = _pretty_number(record, "pretty_unit_of_measure_qty")
        if multiplier is None or multiplier <= 0.0:
            multiplier = _fixed_quantity(_first_value(record, "unit_of_measure_qty"))
        call_put = _call_put(_first_value(record, "instrument_class"))
        cfi = _optional_text(_first_value(record, "cfi"))
        cfi_style, cfi_settlement, cfi_standard = _option_cfi_semantics(
            cfi,
            call_put=call_put,
        )
        explicit_style = _optional_text(
            _first_value(record, "exercise_style", "exerciseStyle")
        )
        explicit_settlement = _optional_text(
            _first_value(record, "settlement_type", "settlementType")
        )
        exercise_style = explicit_style or cfi_style
        settlement_type = explicit_settlement or cfi_settlement
        security_type = _optional_text(_first_value(record, "security_type"))
        action = _enum_text(_first_value(record, "security_update_action"))
        active = action not in {"D", "DELETE"}
        standard = bool(
            active
            and call_put in {"CALL", "PUT"}
            and strike is not None
            and strike > 0.0
            and multiplier is not None
            and math.isclose(multiplier, 100.0)
            and _occ_underlying(contract) == symbol
            and cfi_standard is True
            and security_type == "OPT"
            and exercise_style in {"AMERICAN", "EUROPEAN"}
            and settlement_type is not None
            and _integer(_first_value(record, "publisher_id"))
            == OPRA_CONSOLIDATED_PUBLISHER_ID
        )
        definition = {
            "provider": self.provider,
            "dataset": self.dataset,
            "source_schema": OPRA_DEFINITION_SCHEMA,
            "contract_symbol": contract,
            "symbol": symbol,
            "expiration_date": expiration.normalize() if expiration is not None else pd.NaT,
            "call_put": call_put,
            "strike": strike,
            "multiplier": multiplier,
            "standard_contract": standard,
            "definition_active": active,
            "definition_effective_at": effective,
            "definition_activation_at": activation,
            "definition_market_event_at": market_event,
            "definition_provider_received_at": provider_received,
            "definition_provider_sent_at": provider_sent,
            "definition_local_received_at": local_received_at,
            # SDK 0.81 has no dedicated fields for these attributes, but OPRA's
            # definition carries the ISO 10962 CFI classifier. Decode only a
            # complete listed call/put CFI; unknown/X attributes stay missing.
            "exercise_style": exercise_style,
            "settlement_type": settlement_type,
            "settlement_reference": _optional_text(
                _first_value(
                    record,
                    "settlement_reference",
                    "settlementReference",
                )
            ) or (f"OPRA_DEFINITION_CFI:{cfi}" if cfi_settlement else None),
            "contract_semantics_source": (
                "OPRA_DEFINITION_EXPLICIT_FIELDS"
                if explicit_style and explicit_settlement
                else "OPRA_DEFINITION_CFI_ISO10962"
                if exercise_style and settlement_type
                else "OPRA_DEFINITION_AMBIGUOUS"
            ),
            "cfi": cfi,
            "security_type": security_type,
            "publisher_id": _integer(_first_value(record, "publisher_id")),
            "instrument_id": instrument_id,
            "security_update_action": action,
        }
        with self._condition:
            versions = self._definitions.get(contract)
            prior = versions.get(effective) if versions is not None else None
            if prior is not None and _semantic_mapping(prior) != _semantic_mapping(
                definition
            ):
                raise DatabentoOpraIntegrityError("OPRA_DEFINITION_DUPLICATE_DIVERGED")
            if prior is None:
                if self._definition_count >= self._maximum_definitions:
                    self._stream_unavailable_reason = (
                        "OPRA_DEFINITION_BUFFER_CAPACITY_EXCEEDED"
                    )
                    self._condition.notify_all()
                    return
                if versions is None:
                    versions = {}
                    self._definitions[contract] = versions
                versions[effective] = definition
                self._definition_count += 1
            if instrument_id is not None:
                if not self._remember_instrument_locked(instrument_id, contract):
                    return
            self._condition.notify_all()

    def _remember_instrument_locked(self, instrument_id: int, contract: str) -> bool:
        if (
            instrument_id not in self._instrument_symbols
            and len(self._instrument_symbols) >= self._maximum_definitions
        ):
            self._stream_unavailable_reason = "OPRA_SYMBOL_MAP_CAPACITY_EXCEEDED"
            self._condition.notify_all()
            return False
        # Databento instrument IDs are only day-scoped. A later mapping may
        # legitimately replace an earlier contract without mutating buffered
        # quote rows, which are keyed by their resolved contract symbol.
        self._instrument_symbols[instrument_id] = contract
        return True

    def _ingest_quote(
        self,
        record: object,
        *,
        local_received_at: pd.Timestamp,
    ) -> None:
        publisher_id = _integer(_first_value(record, "publisher_id"))
        if publisher_id != OPRA_CONSOLIDATED_PUBLISHER_ID:
            return
        instrument_id = _integer(_first_value(record, "instrument_id"))
        contract = str(_first_value(record, "symbol", "raw_symbol") or "").strip()
        with self._condition:
            if not contract and instrument_id is not None:
                contract = self._instrument_symbols.get(instrument_id, "")
        symbol = _occ_underlying(contract)
        if symbol not in self._symbols:
            return
        # For CBBO-1s, Databento defines ts_recv as the clamped *end* of the
        # one-second interval, not the event time of the BBO update. Represent
        # the usable quote conservatively at the interval start so a record
        # ending exactly on the target remains strictly pre-target.
        interval_end = _record_timestamp(record, "ts_recv")
        market_event = (
            interval_end - pd.Timedelta(seconds=1)
            if interval_end is not None
            else None
        )
        provider_sent = _record_timestamp(record, "ts_out")
        if interval_end is None or market_event is None:
            raise DatabentoOpraIntegrityError("OPRA_QUOTE_CLOCK_MISSING")
        with self._condition:
            prior_watermark = self._watermarks.get(symbol)
            if prior_watermark is None or interval_end > prior_watermark:
                self._watermarks[symbol] = interval_end
                self._condition.notify_all()
        if provider_sent is not None and provider_sent < interval_end:
            raise DatabentoOpraIntegrityError("OPRA_QUOTE_PROVIDER_CLOCK_REVERSED")
        if local_received_at + pd.Timedelta(seconds=5) < (
            provider_sent or interval_end
        ):
            raise DatabentoOpraIntegrityError("OPRA_QUOTE_LOCAL_CLOCK_REVERSED")
        bid = _pretty_number(record, "pretty_bid_px_00")
        ask = _pretty_number(record, "pretty_ask_px_00")
        if bid is None:
            bid = _fixed_price(_first_value(record, "bid_px_00", "bid"))
        if ask is None:
            ask = _fixed_price(_first_value(record, "ask_px_00", "ask"))
        if (
            bid is None
            or ask is None
            or bid <= 0.0
            or ask <= bid
        ):
            return
        quote = {
            "provider": self.provider,
            "dataset": self.dataset,
            "source_schema": self.schema,
            "symbol": symbol,
            "contract_symbol": contract,
            "quote_timestamp": market_event,
            "market_event_timestamp": market_event,
            "market_event_clock_status": "CBBO_INTERVAL_START_CONSERVATIVE",
            "provider_interval_end_at": interval_end,
            "provider_received_at": interval_end,
            "provider_receipt_clock_status": "CBBO_INTERVAL_END",
            "provider_sent_at": provider_sent,
            "local_received_at": local_received_at,
            "last_trade_event_at": _record_timestamp(record, "ts_event"),
            "bid": bid,
            "ask": ask,
            "bid_size": _plain_number(
                _first_value(record, "bid_sz_00", "bid_size")
            ),
            "ask_size": _plain_number(
                _first_value(record, "ask_sz_00", "ask_size")
            ),
            "publisher_id": publisher_id,
            "instrument_id": instrument_id,
        }
        bucket_target = _strict_next_quarter_hour(market_event)
        with self._condition:
            bucket = self._quote_buckets.setdefault(bucket_target, {})
            prior = bucket.get(contract)
            if prior is not None:
                prior_time = _utc(prior["quote_timestamp"])
                if prior_time == market_event and _semantic_mapping(
                    prior
                ) != _semantic_mapping(quote):
                    raise DatabentoOpraIntegrityError("OPRA_QUOTE_DUPLICATE_DIVERGED")
                if prior_time >= market_event:
                    return
            if prior is None and len(bucket) >= self._maximum_contracts_per_bucket:
                self._stream_unavailable_reason = "OPRA_QUOTE_BUFFER_CAPACITY_EXCEEDED"
                self._condition.notify_all()
                return
            bucket[contract] = quote
            self._quote_buckets = OrderedDict(sorted(self._quote_buckets.items()))
            while len(self._quote_buckets) > self._retained_target_buckets:
                self._quote_buckets.popitem(last=False)
            self._condition.notify_all()

    def _record_callback_failure(self, _exc: Exception) -> None:
        with self._condition:
            self._callback_failures += 1
            self._stream_unavailable_reason = "OPRA_CALLBACK_FAILED"
            self._condition.notify_all()

    def _record_reconnect(self, _last: object, _new: object) -> None:
        with self._condition:
            self._reconnects += 1
            self._stream_unavailable_reason = None
            self._condition.notify_all()

    def _raise_buffer_error_locked(self) -> None:
        if self._integrity_error is not None:
            raise DatabentoOpraIntegrityError(self._integrity_error)
        if self._stream_unavailable_reason is not None:
            raise OptionProviderUnavailable(self._stream_unavailable_reason)

    def _terminate_client(self) -> None:
        client = self._client
        terminate = getattr(client, "terminate", None) if client is not None else None
        if callable(terminate):
            try:
                terminate()
            except Exception:
                pass


def _record_type(record: object) -> str:
    return _enum_text(_first_value(record, "rtype")).lower().replace("_", "-")


def _enum_text(value: object) -> str:
    if value is None:
        return ""
    raw = getattr(value, "name", None)
    if raw is not None:
        name = str(raw).strip()
        aliases = {
            "INSTRUMENT_DEF": "instrument-def",
            "SYMBOL_MAPPING": "symbol-mapping",
            "CBBO_1S": "cbbo-1s",
            "DELETE": "DELETE",
        }
        if name in aliases:
            return aliases[name]
    return str(value).strip()


def _first_value(record: object, *names: str) -> object | None:
    for name in names:
        try:
            value = getattr(record, name)
        except (AttributeError, OverflowError, ValueError):
            continue
        if value is not None:
            return value
    return None


def _record_timestamp(record: object, *names: str) -> pd.Timestamp | None:
    for name in names:
        value = _first_value(record, name)
        if value is None:
            continue
        try:
            if isinstance(value, numbers.Integral):
                if int(value) <= 0 or int(value) >= 2**63 - 1:
                    continue
                return pd.Timestamp(int(value), unit="ns", tz="UTC")
            parsed = pd.to_datetime(value, utc=True, errors="coerce")
            if pd.notna(parsed):
                return pd.Timestamp(parsed)
        except (OverflowError, TypeError, ValueError):
            continue
    return None


def _utc(value: object) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")


def _strict_next_quarter_hour(value: pd.Timestamp) -> pd.Timestamp:
    timestamp = _utc(value)
    return timestamp.floor("15min") + pd.Timedelta(minutes=15)


def _integer(value: object) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return None


def _plain_number(value: object) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (OverflowError, TypeError, ValueError):
        return None


def _pretty_number(record: object, name: str) -> float | None:
    return _plain_number(_first_value(record, name))


def _fixed_price(value: object) -> float | None:
    number = _plain_number(value)
    if number is None:
        return None
    return number / OPRA_PRICE_SCALE if isinstance(value, numbers.Integral) else number


def _fixed_quantity(value: object) -> float | None:
    number = _plain_number(value)
    if number is None:
        return None
    return number / OPRA_PRICE_SCALE if abs(number) >= 1_000_000 else number


def _call_put(value: object) -> str | None:
    text = _enum_text(value).strip().upper()
    if text in {"C", "CALL"}:
        return "CALL"
    if text in {"P", "PUT"}:
        return "PUT"
    return None


def _optional_text(value: object) -> str | None:
    text = str(value).strip().upper() if value is not None else ""
    return text or None


def _option_cfi_semantics(
    value: str | None,
    *,
    call_put: str | None,
) -> tuple[str | None, str | None, bool | None]:
    cfi = str(value or "").strip().upper()
    expected_group = "C" if call_put == "CALL" else "P" if call_put == "PUT" else ""
    if len(cfi) != 6 or cfi[0] != "O" or cfi[1] != expected_group:
        return None, None, None
    style = {
        "A": "AMERICAN",
        "E": "EUROPEAN",
        "B": "BERMUDAN",
    }.get(cfi[2])
    settlement = {
        "P": "PHYSICAL",
        "C": "CASH",
        "N": "NON_DELIVERABLE",
        "E": "ELECT_AT_EXERCISE",
    }.get(cfi[4])
    standardized = {"S": True, "N": False}.get(cfi[5])
    # Production parents are single-name equities; an OPRA classifier for a
    # basket, index, future, or unknown underlying cannot authorize this route.
    if cfi[3] != "S":
        return None, None, False
    return style, settlement, standardized


def _occ_underlying(contract_symbol: str) -> str:
    value = str(contract_symbol).strip().upper()
    if len(value) < 15:
        return ""
    suffix = value[-15:]
    if (
        not suffix[:6].isdigit()
        or suffix[6] not in {"C", "P"}
        or not suffix[7:].isdigit()
    ):
        return ""
    root = value[:-15].strip()
    return root if root and all(character.isalpha() or character == "." for character in root) else ""


def _semantic_mapping(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    # Reconnect replay may legitimately change transport-send/local-receipt
    # clocks while preserving the same market/provider-receipt evidence.
    ignored = {
        "local_received_at",
        "provider_sent_at",
        "definition_local_received_at",
        "definition_provider_sent_at",
    }
    return tuple(
        sorted(
            (str(key), str(item))
            for key, item in value.items()
            if key not in ignored
        )
    )


__all__ = [
    "DatabentoOpraIntegrityError",
    "DatabentoOpraLiveAdapter",
    "OPRA_CONSOLIDATED_PUBLISHER_ID",
    "OPRA_DATASET",
    "OPRA_DEFINITION_SCHEMA",
    "OPRA_LIVE_SCHEMA",
    "OPRA_PROVIDER",
]
