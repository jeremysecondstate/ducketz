"""Finite Databento Live replay; preserve native control and mapping evidence."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import time

from datafetching.databento_opra_replay import _CappedStream, _check_control, _system_evidence
from .market_collection import CollectionFailure, classify_error, instant

PROJECTION_VERSION = "intraday-native-context-projection-v1"


def capture(api_key, request, start, end, raw_path, *, max_records=100_000,
            timeout_seconds=30, max_bytes=2 * 1024 * 1024 * 1024, client_factory=None):
    """No Historical API/pay-as-you-go requests, new purchase, or auto-reconnect.

    Start is explicit and within 48 hours. The gateway's acknowledgement and
    replay-completion are required; its retention may be shorter. The completed
    quarter is selected from genuine records after native replay is retained.
    """
    import databento as db
    now = datetime.now(timezone.utc)
    current = request.params.get("capture_mode") == "current_stream"
    capture_seconds = request.params.get("capture_seconds", 5)
    if current and not 0 < capture_seconds < timeout_seconds:
        raise ValueError("Current native observation window exceeds the deadline")
    start, end = instant(start), instant(end)
    if not start < end <= now or end - start > timedelta(minutes=15):
        raise ValueError("Replay requires a bounded completed quarter")
    if now - start > timedelta(hours=48):
        raise RuntimeError("Requested replay interval is not available within the bounded retention")
    if not 0 < timeout_seconds <= 60 or not 0 < max_bytes <= 2 * 1024 * 1024 * 1024:
        raise ValueError("Replay deadline/byte bound exceeds the collection budget")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    stream = _CappedStream(raw_path, max_bytes)
    acknowledgements, completions, errors = [], [], []
    gateway_failures = []
    callback_failures = []
    client, subscription_id, timer, raised = None, -1, None, None
    current_window_finished = False
    current_ack_at, current_ack_monotonic, current_end_at, current_duration = None, None, None, None
    received_records = 0

    def stop():
        if client is not None:
            client.stop()

    def finish_current():
        nonlocal current_window_finished, current_end_at, current_duration
        current_end_at = datetime.now(timezone.utc)
        current_duration = time.monotonic()-current_ack_monotonic
        current_window_finished = True
        stop()

    def failed(exc):
        # Exception strings can contain credentials. Only a safe type is retained.
        errors.append(type(exc).__name__)
        status, reason = classify_error(exc)
        callback_failures.append({"status": status, "reason": reason, "type": type(exc).__name__})
        stop()

    def reconnected(*args):
        errors.append("UnexpectedReconnect")
        stop()

    def callback(record):
        nonlocal received_records, timer, current_ack_at, current_ack_monotonic
        if isinstance(record, db.ErrorMsg):
            status, reason = classify_error(RuntimeError(str(record.err)))
            gateway_failures.append({"status": status, "reason": reason,
                                     "code": str(getattr(record, "code", "unknown"))})
            errors.append("GatewayError")
            stop()
        elif isinstance(record, db.SystemMsg):
            evidence = _system_evidence(record)
            if evidence["code"] in (1, 3):
                complete = evidence["code"] == 3
                _check_control(evidence, schema=request.schema, subscription_id=subscription_id, completion=complete)
                (completions if complete else acknowledgements).append(evidence)
                if current and not complete and timer is None:
                    import threading
                    current_ack_monotonic = time.monotonic()
                    current_ack_at = datetime.now(timezone.utc)
                    if current_ack_monotonic+capture_seconds > began+timeout_seconds:
                        raise TimeoutError("Native ACK left insufficient current observation time")
                    timer = threading.Timer(capture_seconds, finish_current)
                    timer.daemon = True
                    timer.start()
                if complete and not current:
                    stop()
        elif not isinstance(record, db.SymbolMappingMsg):
            received_records += 1

    began = time.monotonic()
    try:
        client = (client_factory or db.Live)(key=api_key, compression=db.Compression.ZSTD,
            reconnect_policy="none", slow_reader_behavior="warn")
        client.add_callback(callback, exception_callback=failed)
        client.add_reconnect_callback(reconnected, exception_callback=failed)
        client.add_stream(stream, exception_callback=failed)
        subscription_id = client.subscribe(dataset=request.dataset, schema=request.schema,
            symbols=list(request.params["symbols"]), stype_in=request.params["stype_in"],
            **({} if current else {"start": start.isoformat()}))
        client.start()
        remaining = timeout_seconds - (time.monotonic() - began)
        if remaining <= 0:
            raise TimeoutError("Replay subscription exceeded deadline")
        client.block_for_close(timeout=remaining)
        if gateway_failures:
            failure = gateway_failures[0]
            raise CollectionFailure(failure["status"], failure["reason"], {"gateway_failures": gateway_failures})
        if callback_failures:
            failure = callback_failures[0]
            raise CollectionFailure(failure["status"], failure["reason"], {"callback_failures": callback_failures})
        if errors:
            raise RuntimeError("Incomplete replay: " + ",".join(errors))
        if (time.monotonic() - began >= timeout_seconds or len(acknowledgements) != 1
                or (not current and len(completions) != 1) or (current and not current_window_finished)):
            raise TimeoutError("Replay did not establish an acknowledged complete response")
        stream.flush()
    except Exception as exc:
        raised = exc
    finally:
        if timer is not None:
            timer.cancel()
        if client is not None:
            try:
                client.terminate()
                if client.is_connected():
                    client.block_for_close(timeout=1.0)
            except Exception:
                pass
        stream.close()
    if raised is not None:
        # Some SDK versions raise a gateway error before invoking callbacks.
        # Native ErrorMsg is still authoritative. Inspect only a bounded prefix;
        # do not echo its potentially sensitive message into model snapshots.
        failure = native_failure(raw_path)
        if failure is not None:
            raise failure from None
        raise raised
    # Verify native controls survived the capture; callbacks alone are not the
    # durable source evidence. Raw failure artifacts remain private for diagnosis.
    store = db.DBNStore.from_file(raw_path)
    native_ack, native_complete = [], []
    try:
        meta = store.metadata
        if meta.dataset != request.dataset or (meta.schema is not None and str(meta.schema) != request.schema):
            raise ValueError("Native replay dataset/schema differs from request")
        if meta.partial or meta.not_found:
            raise ValueError("Native replay contains unresolved requested symbols")
        import pandas as pd
        if not current and int(meta.start) != pd.Timestamp(start).value:
            raise ValueError("Native replay start differs from request")
        import databento_dbn as dbn
        expected_rtype = int(dbn.RType.from_schema(dbn.Schema(request.schema)))
        data_ids, mapped_ids = set(), set()
        for record in store:
            if isinstance(record, db.ErrorMsg):
                raise RuntimeError("Native replay contains a gateway error")
            if isinstance(record, db.SystemMsg):
                value = _system_evidence(record)
                if value["code"] in (1, 3):
                    (native_complete if value["code"] == 3 else native_ack).append(value)
                continue
            if isinstance(record, db.SymbolMappingMsg):
                mapped_ids.add(int(record.instrument_id))
                continue
            if int(record.rtype) != expected_rtype:
                raise ValueError("Native replay contains a different schema")
            data_ids.add(int(record.instrument_id))
            if request.schema == "definition" and str(getattr(record, "raw_symbol", "")):
                mapped_ids.add(int(record.instrument_id))
        if data_ids.difference(mapped_ids):
            raise ValueError("Native replay lacks requested instrument mappings")
        if native_ack != acknowledgements or native_complete != completions:
            raise ValueError("Native replay controls differ from delivery evidence")
    finally:
        store.reader.close()
    finished = datetime.now(timezone.utc)
    store = db.DBNStore.from_file(raw_path)
    rows = []
    try:
        # Full native events stay in DBN. Only the versioned context projection
        # enters a model snapshot; conversion memory is bounded by one chunk and
        # the explicit per-instrument projection, not the number of native ticks.
        rows, selected_count = project_frames(store.to_df(schema=request.schema, count=10_000),
            schema=request.schema, start=current_ack_at if current else start,
            end=current_end_at if current else end, max_records=max_records)
    finally:
        store.reader.close()
    digest = hashlib.sha256()
    with raw_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return rows, {"mode": "current_stream_snapshot" if current else "live_intraday_replay", "completed": True,
        "dataset": request.dataset, "schema": request.schema, "symbols": list(request.params["symbols"]),
        "stype_in": request.params["stype_in"], "source_request_start": None if current else start.isoformat(),
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "connection_started_at": now.isoformat(), "finished_at": finished.isoformat(),
        "capture_started_at": (current_ack_at if current else now).isoformat(),
        "capture_finished_at": (current_end_at if current else finished).isoformat(),
        "actual_post_ack_observation_seconds": current_duration if current else None,
        "subscription_id": subscription_id, "sdk_version": db.__version__,
        "subscription_ack": acknowledgements, "replay_completed": completions,
        "raw_path": str(raw_path), "raw_sha256": digest.hexdigest(), "raw_bytes": raw_path.stat().st_size,
        "received_native_records": received_records, "selected_quarter_native_records": selected_count,
        "selected_quarter_records": len(rows), "projection_version": PROJECTION_VERSION,
        "projection_policy": "native bars preserved; actual trades reduced to first/last/count/summed size per instrument; other schemas retain first and last native observation per instrument",
        "timeout_seconds": timeout_seconds, "max_bytes": max_bytes,
        "coverage_claim": ("Actual current observation window only; no complete-quarter or missed-event replay claim"
            if current else "Complete replay response; not complete market-wide observations")}


def native_failure(path, *, maximum_records=100_000):
    """Classify a retained native gateway error even when SDK callbacks missed it."""
    import databento as db
    store = None
    try:
        store = db.DBNStore.from_file(path)
        for index, record in enumerate(store):
            if index >= maximum_records:
                break
            if isinstance(record, db.ErrorMsg):
                status, reason = classify_error(RuntimeError(str(record.err)))
                return CollectionFailure(status, reason, {"gateway_failures": [{
                    "status": status, "reason": reason, "code": str(getattr(record, "code", "unknown")),
                    "basis": "retained_native_error"}]})
    except Exception:
        pass  # preserve the original SDK exception if no native proof can be read
    finally:
        if store is not None:
            store.reader.close()
    return None


def project_frames(frames, *, schema, start, end, max_records=100_000):
    """Project actual observations while preserving the full source in native DBN.

    This never fills a missing timestamp or manufactures a minute candle. Trade
    summaries explicitly name their count and first/last observation times. Quote
    and order projections retain two actual native records, never reconstructed
    order books. OHLCV records retain their native grain and timestamps.
    """
    import math
    sampled = schema in {"bbo-1m", "bbo-1s", "cbbo-1m", "cbbo-1s"}
    clock = "ts_recv" if sampled or schema in {"mbp-1", "mbp-10", "cmbp-1"} else "ts_event"
    bars = schema.startswith("ohlcv-")
    selected_count, native_bars, groups = 0, [], {}
    for source in frames:
        frame = source.reset_index()
        if clock not in frame:
            raise ValueError("Native market frame has no event clock")
        mask = ((frame[clock] > start) & (frame[clock] <= end) if sampled else
                (frame[clock] >= start) & (frame[clock] < end))
        for row in frame[mask].to_dict(orient="records"):
            selected_count += 1
            if bars:
                native_bars.append(row)
                if len(native_bars) > max_records:
                    raise RuntimeError("Native bar projection record budget exceeded")
                continue
            key = (str(row.get("symbol", row.get("raw_symbol", ""))), str(row.get("instrument_id", "")))
            group = groups.setdefault(key, {"first": row, "last": row, "count": 0, "volume": 0,
                                            "valid_trades": 0, "first_trade": None, "last_trade": None})
            group["count"] += 1
            if row[clock] < group["first"][clock]:
                group["first"] = row
            if row[clock] >= group["last"][clock]:
                group["last"] = row
            if schema == "trades":
                price, size = row.get("price"), row.get("size")
                if (isinstance(price, (int, float)) and math.isfinite(price) and price > 0
                        and isinstance(size, (int, float)) and math.isfinite(size) and size >= 0):
                    group["valid_trades"] += 1
                    group["volume"] += size
                    if group["first_trade"] is None or row[clock] < group["first_trade"][clock]:
                        group["first_trade"] = row
                    if group["last_trade"] is None or row[clock] >= group["last_trade"][clock]:
                        group["last_trade"] = row
            if len(groups) * (1 if schema == "trades" else 2) > max_records:
                raise RuntimeError("Instrument projection record budget exceeded")
    if bars:
        return native_bars, selected_count
    rows = []
    for key in sorted(groups):
        group = groups[key]
        if schema == "trades":
            first, last = group["first_trade"], group["last_trade"]
            if first is None or last is None:
                continue
            row = dict(last)
            row.update(first_price=first["price"], last_price=last["price"],
                first_event_at=first[clock], last_event_at=last[clock],
                observed_trade_count=group["valid_trades"], excluded_invalid_trade_count=group["count"] - group["valid_trades"],
                price=last["price"], size=group["volume"], volume=group["volume"],
                projection_version=PROJECTION_VERSION)
            rows.append(row)
        else:
            first, last = dict(group["first"]), dict(group["last"])
            first.update(projection_version=PROJECTION_VERSION, projection_position="first",
                         observed_native_record_count=group["count"])
            last.update(projection_version=PROJECTION_VERSION, projection_position="last",
                        observed_native_record_count=group["count"])
            if group["count"] == 1 or first[clock] == last[clock]:
                last["projection_position"] = "only"
                rows.append(last)
            else:
                rows.extend((first, last))
    return rows, selected_count
