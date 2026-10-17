"""Bounded, zero-quoted-cost Historical depth under the saved Standard plan.

CME depth is historical-only here. This module never opens a Live connection,
changes a subscription, or attributes an older historical window to the current
quarter. Native DBN retains the complete bounded response; the small context
projection keeps only the first and last actual observation per instrument.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import uuid
import warnings

from filelock import FileLock
import pandas as pd

from .market_collection import CollectionFailure, _atomic, _plain, _sha, classify_error, instant


POLICY = "databento-standard-historical-depth-v1"
MODE = "historical_available_window"
DEPTH_SCHEMAS = {"mbp-10", "mbo"}
PARAMETERS = {"capture_mode": MODE, "historical_policy": POLICY,
    "window_seconds": 900, "max_native_records": 1_000_000,
    "max_download_bytes": 256 * 1024 * 1024, "max_cost_usd": 0.0,
    "timeout_seconds": 30}
PLAN_REFERENCE = "docs/databento-plan/databento_standard_plan_data_access.md#2-cme-globex-mdp-30--standard"
GATEWAY = "https://hist.databento.com/v0/"


def is_cme_depth(request):
    return request.provider == "databento" and request.dataset == "GLBX.MDP3" and request.schema in DEPTH_SCHEMAS


def historical_request(request):
    """Return a new source identity; never mutate an existing frozen selector."""
    if not is_cme_depth(request):
        raise ValueError("Historical depth policy is specific to CME MBP-10/MBO")
    return replace(request, endpoint="timeseries.get_range", capability="UNPROBED",
        capability_reason="Included rolling-month Historical depth requires a current zero-cost quote and bounded availability",
        params={**request.params, **PARAMETERS})


def _validate(request):
    if (not is_cme_depth(request) or request.source_kind != "cross_asset" or request.symbol != "*"
            or request.endpoint != "timeseries.get_range"
            or any(request.params.get(k) != v or type(request.params.get(k)) is not type(v)
                   for k, v in PARAMETERS.items())):
        raise ValueError("Historical depth requires the exact versioned bounded policy")
    symbols = request.params.get("symbols")
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 16
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{1,12}(?:\.[vcn]\.\d{1,2})?", s) for s in symbols)
            or len(set(symbols)) != len(symbols)
            or request.params.get("stype_in") not in {"continuous", "raw_symbol"}):
        raise ValueError("Historical depth requires a bounded explicit CME scope")


def effective_window(request, start, end, availability, now):
    """Select one complete historical quarter, preserving its actual clock."""
    _validate(request)
    start, end, now = instant(start), instant(end), instant(now)
    if (not start < end <= now or end-start != pd.Timedelta(minutes=15)
            or start != start.floor("15min") or end != end.floor("15min")):
        raise ValueError("Historical context requires a completed bounded decision quarter")
    schema = availability.get("schema", {}).get(request.schema) if isinstance(availability, dict) else None
    if not isinstance(schema, dict) or not schema.get("start") or not schema.get("end"):
        raise CollectionFailure("UNAVAILABLE", "Provider did not disclose the exact historical schema range")
    first, last = instant(schema["start"]), instant(schema["end"])
    if first >= last:
        raise ValueError("Provider historical schema range is invalid")
    included_start = now - pd.DateOffset(months=1)
    selected_end = min(last, end, now).floor("15min")
    selected_start = selected_end - pd.Timedelta(seconds=request.params["window_seconds"])
    if selected_start < max(first, included_start):
        raise CollectionFailure("UNAVAILABLE", "No complete historical window exists inside the included rolling month and provider range")
    return selected_start, selected_end, {"provider_schema_start": first.isoformat(),
        "provider_schema_end": last.isoformat(), "included_history_start": included_start.isoformat(),
        "included_history_end": now.isoformat(), "effective_start": selected_start.isoformat(),
        "effective_end": selected_end.isoformat(), "requested_start": start.isoformat(),
        "requested_end": end.isoformat(), "window_seconds": request.params["window_seconds"],
        "historical_lag_seconds": (end-selected_end).total_seconds()}


class HistoricalGateway:
    """Official Historical HTTP RPCs with one cancellable total deadline.

    The SDK's synchronous download has no byte limit. The documented HTTP
    endpoint is streamed here with aiohttp (also an SDK dependency); native
    decoding and symbol resolution use the installed Databento DBNStore.
    """
    def __init__(self, key):
        self.key = key
        self.session = None

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession(auth=aiohttp.BasicAuth(self.key, ""),
            timeout=aiohttp.ClientTimeout(total=PARAMETERS["timeout_seconds"]),
            headers={"User-Agent": "ducketz-intraday-historical-depth/1"})
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    @staticmethod
    def _status(code):
        if code == 429:
            raise CollectionFailure("RATE_LIMITED", "Historical provider rate limit")
        if code in {401, 402, 403}:
            raise CollectionFailure("NOT_AUTHORIZED", "Historical provider denied the requested entitlement")
        if code != 200:
            raise CollectionFailure("UNAVAILABLE", "Historical provider did not complete the requested operation", {"http_status": code})

    async def metadata(self, method, params):
        if method not in {"get_dataset_range", "get_cost", "get_billable_size", "get_record_count"}:
            raise ValueError("Unknown Historical metadata method")
        # Match the maintained SDK: range is GET; scoped estimates use POST.
        operation = self.session.get if method == "get_dataset_range" else self.session.post
        arguments = {"params" if method == "get_dataset_range" else "data": params}
        async with operation(GATEWAY + "metadata." + method, **arguments, allow_redirects=False) as response:
            self._status(response.status)
            content = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("Historical metadata exceeds its bounded JSON size")
            return json.loads(content)

    async def download(self, params, path, byte_limit):
        # No redirect can forward authentication to another host. Native bytes
        # are create-only, including partial downloads retained after failures.
        async with self.session.post(GATEWAY + "timeseries.get_range", data={**params,
                "encoding": "dbn", "compression": "zstd", "stype_out": "instrument_id"},
                allow_redirects=False) as response:
            self._status(response.status)
            size = 0
            with path.open("xb") as output:
                async for chunk in response.content.iter_chunked(65536):
                    if size + len(chunk) > byte_limit:
                        raise CollectionFailure("CAPACITY_BLOCKED", "Historical native download exceeded its byte budget")
                    output.write(chunk)
                    size += len(chunk)
                output.flush()
                import os
                os.fsync(output.fileno())
            return size


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_depth(path, request, start, end, max_records, *, deadline=None):
    """Validate native identity and retain first/last actual depth observations."""
    import databento as db
    import databento_dbn as dbn
    store = db.DBNStore.from_file(path)
    count = 0
    try:
        meta = store.metadata
        if (meta.dataset != request.dataset or str(meta.schema) != request.schema
                or str(meta.stype_in) != request.params["stype_in"]
                or str(meta.stype_out) != "instrument_id"
                or int(meta.start) != start.value or meta.end is None or int(meta.end) != end.value
                or set(meta.symbols) != set(request.params["symbols"]) or meta.partial or meta.not_found):
            raise ValueError("Historical native metadata differs from the exact scoped request")
        expected = int(dbn.RType.from_schema(dbn.Schema(request.schema)))
        # DBNStore signals truncated trailing records with a warning. A partial
        # native response can never be promoted to a complete quarter.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for record in store:
                if int(record.rtype) != expected:
                    raise ValueError("Historical depth contains an unexpected record type")
                count += 1
                if deadline is not None and count % 1000 == 0 and time.monotonic() > deadline:
                    raise TimeoutError("Historical native validation exceeded its deadline")
                if count > request.params["max_native_records"]:
                    raise CollectionFailure("CAPACITY_BLOCKED", "Historical native response exceeded its record budget")
                if not start.value <= int(record.ts_recv) < end.value:
                    raise ValueError("Historical depth observation lies outside its effective range")
        groups = {}
        selected = 0
        for frame in store.to_df(schema=request.schema, count=10_000):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("Historical native projection exceeded its deadline")
            for row in frame.reset_index().to_dict(orient="records"):
                selected += 1
                symbol = row.get("symbol")
                if not isinstance(symbol, str) or not symbol:
                    raise ValueError("Historical depth lacks resolved native symbol identity")
                clock = instant(row["ts_recv"])
                if not start <= clock < end or instant(row["ts_event"]) > end:
                    raise ValueError("Historical depth timestamp exceeds its causal window")
                key = (symbol, int(row["instrument_id"]))
                group = groups.setdefault(key, [row, row, 0])
                if clock < instant(group[0]["ts_recv"]): group[0] = row
                if clock >= instant(group[1]["ts_recv"]): group[1] = row
                group[2] += 1
                if len(groups) * 2 > max_records:
                    raise CollectionFailure("CAPACITY_BLOCKED", "Historical depth projection exceeded its record budget")
        if selected != count:
            raise ValueError("Historical depth projection did not consume every native record")
        from .market_collection_replay import PROJECTION_VERSION
        rows = []
        for key in sorted(groups):
            first, last, observations = groups[key]
            chosen = [("only", last)] if observations == 1 else [("first", first), ("last", last)]
            for position, row in chosen:
                rows.append(_plain({**row, "projection_version": PROJECTION_VERSION,
                    "projection_position": position, "observed_native_record_count": observations}))
        return rows, count
    finally:
        store.reader.close()


async def _capture(key, request, start, end, raw_path, *, max_records, cache_root,
                   gateway_factory, decoder, clock):
    _validate(request)
    began = instant(clock())
    deadline = time.monotonic() + request.params["timeout_seconds"]
    evidence = {"mode": MODE, "historical_policy": POLICY, "plan_reference": PLAN_REFERENCE,
        "transport": "Historical HTTP timeseries.get_range", "dataset": request.dataset,
        "schema": request.schema, "symbols": request.params["symbols"], "stype_in": request.params["stype_in"],
        "request_started_at": began.isoformat(), "completed": False,
        "coverage_claim": "Complete bounded historical response only; neither current-quarter nor market-wide completeness"}
    try:
        async with asyncio.timeout(request.params["timeout_seconds"]):
            async with gateway_factory(key) as gateway:
                availability = await gateway.metadata("get_dataset_range", {"dataset": request.dataset})
                first, last, window = effective_window(request, start, end, availability, began)
                evidence.update(window, availability_received_at=instant(clock()).isoformat())
                params = {"dataset": request.dataset, "schema": request.schema,
                    "symbols": ",".join(request.params["symbols"]), "stype_in": request.params["stype_in"],
                    "start": first.isoformat(), "end": last.isoformat()}
                identity = _sha({"source_id": request.source_id, "effective_query": params})
                cache_root.mkdir(parents=True, exist_ok=True)
                response_path = cache_root / (identity + ".json")
                with FileLock(str(cache_root / (identity + ".lock")), timeout=0):
                    if response_path.exists():
                        saved = json.loads(response_path.read_text())
                        claimed = saved.pop("response_sha256", None)
                        native = Path(saved["evidence"]["raw_path"])
                        if (claimed != _sha(saved) or saved.get("query") != params
                                or not native.resolve().is_relative_to(cache_root.resolve())
                                or native.stat().st_size > request.params["max_download_bytes"]
                                or _file_hash(native) != saved["evidence"]["raw_sha256"]):
                            raise ValueError("Historical response cache failed native integrity checks")
                        return saved["rows"], {**saved["evidence"], **window, "cache_reused": True,
                            "cache_verified_at": instant(clock()).isoformat()}
                    cost = await gateway.metadata("get_cost", params)
                    if type(cost) not in (float, int) or not pd.notna(cost) or cost != 0:
                        raise CollectionFailure("NOT_AUTHORIZED", "Historical request has no verified zero-cost plan quote",
                            {"cost_quote_usd": cost if type(cost) in (float, int) and pd.notna(cost) else None})
                    size = await gateway.metadata("get_billable_size", params)
                    count = await gateway.metadata("get_record_count", params)
                    if type(size) is not int or type(count) is not int or min(size, count) < 0:
                        raise ValueError("Historical preflight returned invalid size or count")
                    evidence.update(cost_quote_usd=cost, estimated_billable_bytes=size,
                        estimated_native_records=count, preflight_received_at=instant(clock()).isoformat())
                    if size > request.params["max_download_bytes"] or count > request.params["max_native_records"]:
                        raise CollectionFailure("CAPACITY_BLOCKED", "Historical preflight exceeds the bounded native size or record budget")
                    if shutil.disk_usage(cache_root).free < 1024**3 + 2 * size:
                        raise CollectionFailure("CAPACITY_BLOCKED", "Historical preflight lacks the native storage reserve")
                    raw_path = cache_root / (identity + "-" + uuid.uuid4().hex + ".dbn")
                    evidence["raw_path"] = str(raw_path)
                    await gateway.download({**params, "limit": str(request.params["max_native_records"] + 1)},
                        raw_path, request.params["max_download_bytes"])
                    evidence.update(download_finished_at=instant(clock()).isoformat(),
                        raw_sha256=_file_hash(raw_path), raw_bytes=raw_path.stat().st_size)
                    rows, actual = decoder(raw_path, request, first, last, max_records, deadline=deadline)
                    if time.monotonic() > deadline:
                        raise TimeoutError("Historical decode exceeded the complete capture deadline")
                    evidence.update(completed=True, selected_native_records=actual, selected_records=len(rows),
                        cache_reused=False, finished_at=instant(clock()).isoformat(),
                        projection_policy="First/last actual native depth record per instrument; no book reconstruction or filling",
                        projection_version=request.params.get("projection_version"))
                    saved = {"query": params, "rows": rows, "evidence": evidence}
                    saved["response_sha256"] = _sha(saved)
                    _atomic(response_path, saved)
                    return rows, evidence
    except Exception as exc:
        status, reason = classify_error(exc)
        evidence.update(getattr(exc, "collection_evidence", {}), completed=False,
            failed_at=instant(clock()).isoformat())
        if evidence.get("raw_path") and Path(evidence["raw_path"]).is_file():
            evidence.update(raw_sha256=_file_hash(Path(evidence["raw_path"])),
                raw_bytes=Path(evidence["raw_path"]).stat().st_size)
        raise CollectionFailure(status, reason, evidence) from None


def capture(key, request, start, end, raw_path, *, max_records=100_000, cache_root=None,
            gateway_factory=HistoricalGateway, decoder=decode_depth,
            clock=lambda: datetime.now(timezone.utc)):
    """Synchronous adapter entry; the data-only worker owns this event loop."""
    path = Path(raw_path)
    return asyncio.run(_capture(key, request, start, end, path, max_records=max_records,
        cache_root=Path(cache_root) if cache_root else path.parent / "historical", gateway_factory=gateway_factory,
        decoder=decoder, clock=clock))
