"""Market-only provider adapters; entitlement is established by response evidence.

No account/order endpoint is called here. Databento uses existing Live access
where entitled; CME depth uses separately identified, zero-quoted-cost bounded
Historical access. REST snapshots are observations at receipt, never a
reconstruction of the requested quarter.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import uuid

from .market_collection import CollectionResult, SourceRequest, instant


def _list(value):
    if isinstance(value, (tuple, list)):
        return tuple(str(x) for x in value)
    if not value:
        return ()
    return tuple(json.loads(value)) if value.lstrip().startswith("[") else tuple(x.strip() for x in value.split(",") if x.strip())


def default_requests(symbols):
    """Compile existing local settings; constructors/spec lists perform no I/O."""
    from app.services.fmp_corporate_data import FmpCorporateDataProvider
    from app.services.databento_cme_context import DatabentoCmeContextProvider
    from app.services.fmp_macro_context import FmpMacroContextProvider
    from dataclasses import replace
    from .market_collection_replay import PROJECTION_VERSION
    from .market_collection_history import historical_request, is_cme_depth
    requests = []
    equities = os.getenv("DATABENTO_EQUITIES_DATASET", "EQUS.MINI").strip() or "EQUS.MINI"
    if equities != "EQUS.MINI":
        raise ValueError("Intraday equity context requires the explicitly entitled EQUS.MINI feed")
    corporate = FmpCorporateDataProvider()
    for symbol in symbols:
        for schema, kind in (("trades", "stock_trades"), ("mbp-1", "stock_quote"),
                             ("bbo-1m", "stock_quote"), ("ohlcv-1m", "stock_bars"),
                             ("definition", "fundamentals")):
            requests.append(SourceRequest("databento", kind, symbol, equities, schema,
                params={"symbols": [symbol], "stype_in": "raw_symbol"}))
        for schema, kind, endpoint in (("quote", "stock_quote", "quotes"),
                                      ("ohlcv-1m", "stock_bars", "pricehistory"),
                                      ("chain-greeks-full", "options", "chains")):
            requests.append(SourceRequest("schwab", kind, symbol, "marketdata-v1", schema, endpoint,
                {"surface": "full"} if endpoint == "chains" else {}))
        for spec in corporate.corporate_specs(symbol):
            kind = "stock_quote" if spec.key == "quote" else "fundamentals"
            params = dict(spec.params)
            # Filings dates must follow the fetch receipt; do not freeze today's
            # trailing window into every future quarter's saved configuration.
            params.pop("from", None)
            params.pop("to", None)
            requests.append(SourceRequest("fmp", kind, symbol, "stable", spec.key,
                spec.endpoint, params))
        for endpoint, kind in (("earnings", "events"), ("dividends", "events"),
                               ("splits", "events"), ("aftermarket-trade", "stock_trades"),
                               ("aftermarket-quote", "stock_quote"),
                               ("historical-chart/5min", "stock_bars"),
                               ("historical-chart/15min", "stock_bars")):
            requests.append(SourceRequest("fmp", kind, symbol, "stable", endpoint.replace("/", "-"),
                endpoint, {"symbol": symbol}))
        for schema in ("trades", "cmbp-1", "cbbo-1m", "ohlcv-1m", "definition", "statistics"):
            requests.append(SourceRequest("databento", "opra_options", symbol, "OPRA.PILLAR", schema,
                params={"symbols": [symbol + ".OPT"], "stype_in": "parent",
                    **({"capture_mode": "current_stream", "capture_seconds": 5} if schema == "cmbp-1" else {})}))
        requests.extend((
            SourceRequest("fmp", "stock_bars", symbol, "stable", "historical-chart-1min",
                capability="NOT_AUTHORIZED", capability_reason="The supplied FMP Premium outline excludes Ultimate one-minute data"),
            SourceRequest("databento", "stock_quote", symbol, equities, "mbo",
                capability="UNSUPPORTED", capability_reason="EQUS.MINI does not supply a full-depth order book"),
            SourceRequest("databento", "events", symbol, equities, "status",
                capability="UNSUPPORTED", capability_reason="The current EQUS.MINI schema catalog does not include status"),
            SourceRequest("databento", "opra_options", symbol, "OPRA.PILLAR", "computed-greeks",
                capability="UNSUPPORTED", capability_reason="OPRA delivers market records; computed Greeks come from Schwab"),
        ))
    cme = DatabentoCmeContextProvider()
    if cme.dataset:
        # Retain configured schemas and explicitly collect the entitled trades,
        # definitions and statistics needed to interpret that context.
        schemas = tuple(dict.fromkeys((*cme.schemas, "mbo", "trades", "definition", "statistics", "status")))
        for scope, stype in ((cme.context_symbols, cme.context_stype_in),
                             (cme.contract_symbols, cme.contract_stype_in)):
            for schema in schemas if scope else ():
                request = SourceRequest("databento", "cross_asset", "*", cme.dataset, schema,
                    params={"symbols": list(scope), "stype_in": stype, "projection_version": PROJECTION_VERSION})
                if is_cme_depth(request):
                    # Retain the old identity as a visible descriptor. Historical
                    # transport has a new identity and cannot rebind a model.
                    requests.append(replace(request, capability="UNSUPPORTED",
                        capability_reason="Standard CME depth is historical-only; this legacy Live source is disabled"))
                    requests.append(historical_request(request))
                else:
                    requests.append(request)
    # These are the existing local commodity symbols, not another PC's tuning.
    macro = FmpMacroContextProvider()
    commodity_symbols = [spec.output_symbol for spec in macro.commodity_proxy_specs()]
    if commodity_symbols:
        requests.append(SourceRequest("fmp", "cross_asset", "*", "stable", "commodity-quote",
            "batch-commodity-quotes", {"configured_symbols": commodity_symbols},
            capability="UNSUPPORTED", capability_reason="Legacy bulk commodity source replaced by distinct exact single-symbol quote requests"))
        from .market_collection_fmp_commodities import native_commodity_requests
        requests.extend(native_commodity_requests(commodity_symbols))
    from .market_collection_extended import extended_requests
    from .market_collection_fmp_context import context_requests
    from .market_collection_schwab_stream import stream_requests
    requests.extend(extended_requests(symbols))
    requests.extend(context_requests(symbols))
    native_futures = _list(os.getenv("DUCKETS_SCHWAB_FUTURES_SYMBOLS", ""))
    if not native_futures:
        # Documented Schwab root keys are distinct native scopes, not claims
        # that they equal Databento's continuous-contract roll convention.
        supported_roots = {"ES", "NQ", "RTY", "CL", "GC", "ZN", "6E"}
        roots = [str(value).split(".")[0] for value in cme.context_symbols]
        native_futures = tuple("/"+root for root in roots if root in supported_roots)
    requests.extend(stream_requests(symbols, futures=native_futures))
    return tuple(replace(r, params={**r.params, "projection_version": PROJECTION_VERSION})
                 if r.provider == "databento" and r.capability == "UNPROBED" else r for r in requests)


def capability_catalog():
    """Explicit catalog gaps remain visible alongside attempted source records."""
    return [
        {"provider": "databento", "dataset": "EQUS.MINI", "status": "CONFIGURED_UNPROBED",
         "collected_schemas": ["trades", "mbp-1", "bbo-1m", "ohlcv-1m", "definition"],
         "unsupported": ["mbo", "mbp-10", "status"],
         "derivable_but_not_materialized": ["tbbo", "bbo-1s", "ohlcv-1s", "ohlcv-1h", "ohlcv-1d"],
         "limitation": "Component-venue aggregate; no claim of consolidated market-wide coverage"},
        {"provider": "databento", "dataset": "OPRA.PILLAR", "status": "CONFIGURED_UNPROBED",
         "collected_schemas": ["trades", "cmbp-1", "cbbo-1m", "ohlcv-1m", "definition", "statistics"],
         "derivable_but_not_materialized": ["tcbbo", "cbbo-1s", "ohlcv-1s", "ohlcv-1h", "ohlcv-1d"],
         "unsupported": ["mbo", "computed-greeks"],
         "limitation": "CMBP has no replay: a bounded current stream window is retained. Other schemas request replay; source retention/capacity failures remain explicit"},
        {"provider": "databento", "dataset": "GLBX.MDP3", "status": "LOCAL_CONFIGURATION_REQUIRED",
         "collected_schemas": ["configured entitled Live schemas", "trades", "definition", "statistics", "status"],
         "historical_only": ["mbp-10", "mbo"],
         "limitation": "Existing futures scopes only. Depth uses the included rolling month, zero-cost quote, full available historical quarter, and explicit capacity bounds. Older depth is collection-only until a model explicitly admits its distinct identity; no current-quarter or reconstructed MBO book claim"},
        {"provider": "schwab", "dataset": "marketdata-v1", "status": "CONFIGURED_UNPROBED",
         "implemented": ["equity quote", "bounded one-minute OHLCV", "full provider-returnable current options chain with Greeks", "instrument fundamental projection", "bounded native LEVELONE_EQUITIES/OPTIONS/FUTURES, CHART_EQUITY/FUTURES, NYSE/NASDAQ/OPTIONS_BOOK streams"],
         "unimplemented": ["uninterrupted between-refresh stream recording", "futures options outside the configured context scope"],
         "limitation": "Native streams capture actual current windows only, with necessary read-only userPreference bootstrap; no missed-event replay or historical Greeks reconstructed"},
        {"provider": "fmp", "dataset": "stable", "status": "CONFIGURED_UNPROBED",
         "implemented": ["existing corporate statement/profile/metrics/ratios/growth/filings specs", "quotes", "earnings", "dividends", "splits", "aftermarket snapshots", "5/15-minute OHLCV", "configured commodity quotes", "bounded analyst estimates/ratings/targets", "DCF", "bounded stock news and press releases", "nine provider technical indicators at15min period10", "configured GDP/CPI/unemploymentRate/federalFunds current macro series"],
         "not_configured": ["treasury-rates", "economic-calendar", "market-risk-premium"],
         "redundancy": "Provider technical indicators retain distinct source selectors alongside local price-derived features; other timeframe variants are outside this15m scope",
         "not_authorized_by_supplied_plan_outline": ["one-minute Ultimate charts"],
         "unsupported": ["options analytics", "order-book depth"],
         "limitation": "A plan description does not prove endpoint entitlement; response categories remain explicit"},
    ]


def _utc(value, *, unit=None, local_zone=None):
    import pandas as pd
    if value is None or value == "":
        return None
    try:
        stamp = pd.Timestamp(value, unit=unit) if unit else pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            if local_zone is None:
                return None  # a provider date without a documented zone is not an event clock
            stamp = stamp.tz_localize(local_zone, ambiguous="raise", nonexistent="raise")
        return stamp.tz_convert("UTC").isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _first(row, names):
    return next((row[k] for k in names if row.get(k) is not None), None)


def _greek(value):
    import math
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value != -999:
        return value
    return None


def quote_record(raw, symbol, *, event_at=None):
    aliases = {"bid": ("bid", "bidPrice", "bid_price", "bid_px_00"),
        "ask": ("ask", "askPrice", "ask_price", "ask_px_00"),
        "bid_size": ("bid_size", "bidSize", "bid_sz_00"),
        "ask_size": ("ask_size", "askSize", "ask_sz_00"),
        "last": ("last", "lastPrice", "price", "last_price", "close"),
        "price": ("price", "lastPrice", "last", "close"),
        "size": ("size", "lastSize", "volume"),
        "volume": ("volume", "totalVolume", "size"),
        "open": ("open",), "high": ("high",), "low": ("low",), "close": ("close",)}
    return {"symbol": symbol, "event_at": event_at, "raw": raw,
            **{name: _first(raw, keys) for name, keys in aliases.items()}}


class SchwabAdapter:
    def __init__(self, session_factory=None, *, root=None, requests=()):
        self.session_factory = session_factory
        self.session = None
        from .market_collection_schwab_stream import SchwabStreamCollector
        self.stream = SchwabStreamCollector(root, requests) if root is not None else None

    def __call__(self, request, start, end, max_records):
        if self.session is None:
            from datafetching.schwab_fetch import DataFetchingSchwabSession
            self.session = (self.session_factory or DataFetchingSchwabSession)()
        symbol, session = request.symbol, self.session
        if request.endpoint == "stream":
            if self.stream is None:
                raise RuntimeError("Market streamer durable root was not configured")
            return self.stream(session, request, start, end, max_records)
        from .market_collection_extended import handles, collect_extended, fetch_full_schwab_chain
        if handles(request):
            return collect_extended(request, start, end, max_records, schwab_session=session)
        if request.endpoint == "quotes":
            raw = session.get_equity_quotes([symbol]).get(symbol)
            if raw is None:
                return CollectionResult([])
            row = quote_record(raw, symbol, event_at=_utc(_first(raw, ("quoteTime", "tradeTime")), unit="ms", local_zone="UTC"))
            row.update(quote_timestamp=_utc(raw.get("quoteTime"), unit="ms", local_zone="UTC"),
                       trade_timestamp=_utc(raw.get("tradeTime"), unit="ms", local_zone="UTC"))
            return CollectionResult([row], evidence={"mode": "current_snapshot", "realtime": raw.get("quote_realtime")})
        if request.endpoint == "pricehistory":
            raw = session.get_price_history(symbol, period_type="day", period=None,
                frequency_type="minute", frequency=1, need_extended_hours_data=True,
                start_datetime=start, end_datetime=end)
            records = [quote_record(r, symbol, event_at=_utc(r.get("datetime"), unit="ms", local_zone="UTC"))
                       for r in raw.get("candles", [])]
            records = [r for r in records if r["event_at"] and start <= instant(r["event_at"]) < end]
            return CollectionResult(records, evidence={"extended_hours_requested": True, "mode": "bounded_price_history"})
        if request.endpoint != "chains":
            return CollectionResult([], "UNSUPPORTED", "Unknown Schwab market endpoint")
        full_surface = request.params.get("surface") == "full"
        raw = fetch_full_schwab_chain(session, symbol) if full_surface else session.get_option_chain_snapshot(symbol)
        records = []
        for field, option_type in (("callExpDateMap", "call"), ("putExpDateMap", "put")):
            for strikes in raw.get(field, {}).values():
                for contracts in strikes.values():
                    for contract in contracts:
                        row = quote_record(contract, symbol, event_at=_utc(_first(contract, ("quoteTimeInLong", "tradeTimeInLong")), unit="ms", local_zone="UTC"))
                        row.update(underlying_symbol=symbol, contract_symbol=contract.get("symbol"),
                            option_type=option_type, strike=contract.get("strikePrice"),
                            expiration=contract.get("expirationDate"), open_interest=contract.get("openInterest"),
                            implied_volatility=(float(contract["volatility"]) / 100
                                if isinstance(contract.get("volatility"), (int, float)) and contract["volatility"] >= 0 else None),
                            implied_volatility_unit="decimal",
                            **{k: _greek(contract.get(k)) for k in ("delta", "gamma", "theta", "vega", "rho")})
                        row.update(quote_timestamp=_utc(contract.get("quoteTimeInLong"), unit="ms", local_zone="UTC"),
                            trade_timestamp=_utc(contract.get("tradeTimeInLong"), unit="ms", local_zone="UTC"),
                            underlying_quote_timestamp=_utc((raw.get("underlying") or {}).get("quoteTime"), unit="ms", local_zone="UTC"),
                            expiration_at=_utc(contract.get("expirationDate"), local_zone="UTC",
                                unit="ms" if isinstance(contract.get("expirationDate"), (int, float)) else None))
                        records.append(row)
        return CollectionResult(records, evidence={"mode": "current_option_chain", "is_delayed": raw.get("isDelayed"),
            "underlying": raw.get("underlying"),
            "surface": "all provider-returnable strikes/expirations requested without slice filters" if full_surface else "100 strikes, 200 calendar days",
            "provider_contract_count": raw.get("numberOfContracts"),
            "normalized_contract_count": len(records)})


class FmpAdapter:
    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory
        self.provider = None

    def _provider(self):
        if self.provider is None:
            from app.services.fmp_corporate_data import FmpCorporateDataProvider
            self.provider = (self.provider_factory or FmpCorporateDataProvider)()
        return self.provider

    def __call__(self, request, start, end, max_records):
        from .market_collection_fmp_commodities import handles as commodity_handles, collect as collect_commodity
        if commodity_handles(request):
            return collect_commodity(request, start, end, max_records, provider_factory=self._provider)
        from .market_collection_fmp_context import handles as context_handles, collect_context
        if context_handles(request):
            return collect_context(request, start, end, max_records, fmp_provider=self._provider())
        from .market_collection_extended import handles, collect_extended
        if handles(request):
            return collect_extended(request, start, end, max_records, fmp_fetch_page=self._collect_page)
        return self._collect_page(request, start, end, max_records)

    def _collect_page(self, request, start, end, max_records):
        self._provider()
        params = dict(request.params)
        commodity_symbols = params.pop("configured_symbols", None)
        if commodity_symbols is not None and (request.endpoint != "batch-commodity-quotes"
                or request.symbol != "*" or not isinstance(commodity_symbols, list)
                or not commodity_symbols or len(set(commodity_symbols)) != len(commodity_symbols)
                or any(not re.fullmatch(r"[A-Z0-9._/-]{1,30}", symbol) for symbol in commodity_symbols)):
            raise ValueError("Invalid exact configured commodity scope")
        if request.endpoint.startswith("historical-chart/"):
            params.update({"from": instant(start).tz_convert("America/New_York").date().isoformat(),
                           "to": instant(end).tz_convert("America/New_York").date().isoformat()})
        if request.endpoint == "sec-filings-search/symbol":
            today = datetime.now(timezone.utc).date()
            params.update({"from": (today - timedelta(days=370)).isoformat(), "to": today.isoformat()})
        payload = self.provider._get_json(request.endpoint, params)
        rows = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Unexpected FMP result shape")
        records = []
        occurrences = {}
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValueError("FMP result contains a non-record row")
            symbol = str(raw.get("symbol") or params.get("symbol") or request.symbol).upper()
            if commodity_symbols is not None and symbol not in commodity_symbols:
                continue
            if request.symbol != "*" and symbol != request.symbol:
                raise ValueError("FMP returned a symbol outside the requested scope")
            epoch_unit = "ms" if request.endpoint in {"aftermarket-quote", "aftermarket-trade"} else "s"
            stamp = _utc(raw.get("timestamp"), unit=epoch_unit, local_zone="UTC")
            if request.endpoint.startswith("historical-chart/"):
                # FMP intraday chart wall clocks are exchange-local US/Eastern.
                stamp = _utc(raw.get("date"), local_zone="America/New_York")
                if stamp is None or not start <= instant(stamp) < end:
                    continue
            row = quote_record(raw, symbol, event_at=stamp)
            if request.source_kind in {"fundamentals", "events"}:
                # Accepted/filing strings lacking a timezone cannot prove an
                # intraday publication instant. Preserve them raw and use receipt.
                published = _utc(_first(raw, ("publishedDate", "acceptedDate", "filingDate")))
                row.update(published_at=published, publication_time_basis="provider_timestamp" if published else "receipt_only",
                           event_date=_utc(raw.get("date"), local_zone="America/New_York"),
                           event_date_precision="provider_date; midnight Eastern used only for day distance",
                           event_type="split" if request.schema == "splits" else request.schema)
                if published:
                    row["event_at"] = published
                if request.source_kind == "fundamentals":
                    row["period_end"] = raw.get("date")
                row.update({re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): v
                            for k, v in raw.items() if isinstance(v, (int, float)) and k != "timestamp"})
                row.update(earnings_yield=_first(raw, ("earningsYield", "earningsYieldTTM")),
                    debt_equity=_first(raw, ("debtToEquity", "debtToEquityRatio", "debtToEquityRatioTTM", "debtEquityRatio")),
                    profit_margin=_first(raw, ("netProfitMargin", "netProfitMarginTTM")),
                    revenue_growth=raw.get("revenueGrowth"),
                    pe_ratio=_first(raw, ("pe", "peRatio", "priceToEarningsRatio", "priceToEarningsRatioTTM")),
                    market_cap=_first(raw, ("marketCap", "marketCapTTM", "mktCap")))
            if request.source_kind == "stock_trades" or (request.source_kind == "opra_options" and request.schema == "trades"):
                from .market_collection import canonical
                import hashlib
                identity = hashlib.sha256(canonical(raw)).hexdigest()
                count = occurrences.get(identity, 0)
                occurrences[identity] = count + 1
                row["trade_id"] = identity + ":" + str(count)
                row["identity_basis"] = "provider_row_content_and_occurrence; no consolidated exchange trade identifier claimed"
            records.append(row)
        missing = sorted(set(commodity_symbols or [])-{r["symbol"] for r in records})
        return CollectionResult(records, "PARTIAL" if missing else "COMPLETE",
            "Some configured native commodity symbols were absent from the received batch" if missing else "",
            evidence={"endpoint": request.endpoint,
            "resolved_params": params,
            "configured_symbols": commodity_symbols, "missing_configured_symbols": missing,
            "mode": "bounded_intraday_chart" if request.endpoint.startswith("historical-chart/") else "current_rest_snapshot",
            "timezone_rule": "America/New_York for intraday chart wall clocks; ambiguous/nonexistent rejected"})


class DatabentoAdapter:
    def __init__(self, root, replay=None, requests=(), *, historical=None):
        import threading
        self.root, self.replay = Path(root), replay
        self.historical = historical
        self.requests = tuple(requests)
        self.cache = {}
        self.failures = {}
        self.cache_lock = threading.Lock()
        self.group_locks = {}

    def _batch(self, request, start, end, max_records, key):
        from .market_collection_replay import capture
        from .market_collection_history import MODE, capture as capture_historical
        from .market_collection import canonical
        from dataclasses import replace
        import hashlib
        import threading
        scopes = []
        semantics = {k: v for k, v in request.params.items() if k != "symbols"}
        for other in self.requests:
            if (other.provider == "databento" and other.dataset == request.dataset
                    and other.schema == request.schema and other.params.get("stype_in") == request.params.get("stype_in")
                    and {k: v for k, v in other.params.items() if k != "symbols"} == semantics
                    and other.symbol != "*" and request.symbol != "*"):
                scopes.extend(other.params.get("symbols", []))
        scopes = sorted(set(scopes or request.params["symbols"]))
        params = {**request.params, "symbols": scopes}
        historical = request.params.get("capture_mode") == MODE
        identity = hashlib.sha256(canonical([request.dataset, request.schema, params, start, end])).hexdigest()
        with self.cache_lock:
            lock = self.group_locks.setdefault(identity, threading.Lock())
        with lock:
            if identity in self.cache and not historical:
                return self.cache[identity]
            import time
            failed = self.failures.get(identity)
            if failed and time.monotonic() - failed[0] < 60:
                raise failed[1]
            response_path = self.root / "responses" / start.date().isoformat() / (identity + ".json")
            from .market_collection import _sha
            if response_path.exists() and not historical:
                saved = json.loads(response_path.read_text(encoding="utf-8"))
                claimed = saved.pop("response_sha256", None)
                if claimed != _sha(saved):
                    raise ValueError("Completed replay response cache checksum mismatch")
                self.cache[identity] = (saved["rows"], saved["evidence"])
                return self.cache[identity]
            path = self.root / "raw" / start.date().isoformat() / (uuid.uuid4().hex + ".dbn")
            try:
                operation = (self.historical or capture_historical) if historical else (self.replay or capture)
                rows, evidence = operation(key, replace(request, params=params), start, end, path,
                    max_records=max_records, **({"cache_root": self.root / "historical"} if historical else {}))
            except Exception as exc:
                from .market_collection import classify_error, CollectionFailure
                status, reason = classify_error(exc)
                evidence = {"raw_path": str(path), **getattr(exc, "collection_evidence", {}), "acquisition_symbols": scopes,
                            "acquisition_stype_in": params["stype_in"], "completed": False}
                path = Path(evidence["raw_path"])
                if path.exists():
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    evidence.update(raw_sha256=digest.hexdigest(), raw_bytes=path.stat().st_size)
                failure = CollectionFailure(status, reason, evidence)
                self.failures[identity] = (time.monotonic(), failure)
                raise failure from None
            evidence = {**evidence, "acquisition_symbols": scopes, "acquisition_stype_in": params["stype_in"]}
            for row in rows:
                symbol = str(row.get("symbol") or row.get("raw_symbol") or "")
                if params["stype_in"] == "parent":
                    valid = any(re.fullmatch(re.escape(s.removesuffix(".OPT")) + r"\s*\d{6}[CP]\d{8}", symbol) for s in scopes)
                elif params["stype_in"] == "continuous":
                    roots = [s.split(".")[0] for s in scopes]
                    valid = symbol in scopes or any(re.fullmatch(re.escape(root) + r"[FGHJKMNQUVXZ]\d{1,2}", symbol) for root in roots)
                else:
                    valid = symbol in scopes
                if not valid:
                    raise ValueError("Native response has missing or out-of-scope symbol mapping")
            # Persist the completed response cache so a new process can use its
            # native delivery without repeating paid-network acquisition. The
            # canonical quarter snapshots remain independently append-only.
            from .market_collection import _atomic
            response = {"rows": rows, "evidence": evidence}
            response["response_sha256"] = _sha(response)
            _atomic(response_path, response)
            self.cache[identity] = (rows, evidence)
            # Bound the memory cache; disk receipt cache survives process restarts.
            if len(self.cache) > 24:
                self.cache.pop(next(iter(self.cache)))
            return rows, evidence

    def __call__(self, request, start, end, max_records):
        from .market_collection_history import MODE, is_cme_depth
        if is_cme_depth(request) and request.params.get("capture_mode") != MODE:
            return CollectionResult([], "UNSUPPORTED",
                "Standard CME MBP-10/MBO is historical-only; legacy Live request disabled before provider access",
                {"provider_called": False, "required_historical_policy": "databento-standard-historical-depth-v1"})
        key = os.getenv("DATABENTO_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Missing API credentials")
        rows, evidence = self._batch(request, start, end, max_records, key)
        records = []
        occurrences = {}
        for raw in rows:
            symbol = str(raw.get("symbol") or raw.get("raw_symbol") or "")
            if request.symbol != "*":
                matches = (bool(re.fullmatch(re.escape(request.symbol) + r"\s*\d{6}[CP]\d{8}", symbol))
                           if request.source_kind == "opra_options" else symbol == request.symbol)
                if not matches:
                    continue  # another explicitly configured scope in this shared replay
            observation_clock = "ts_recv" if request.schema in {"bbo-1m", "bbo-1s", "cbbo-1m", "cbbo-1s", "mbp-1", "mbp-10", "cmbp-1"} or request.params.get("capture_mode") == MODE else "ts_event"
            stamp = _utc(raw.get(observation_clock), local_zone="UTC")
            if stamp is None:
                raise ValueError("Databento market row lacks an event timestamp")
            row = quote_record(raw, symbol, event_at=stamp)
            row["provider_received_at"] = _utc(raw.get("ts_recv"), local_zone="UTC")
            row["event_clock_basis"] = observation_clock
            row["exchange_event_at"] = _utc(raw.get("ts_event"), local_zone="UTC")
            for field in ("first_price", "last_price", "first_event_at", "last_event_at", "observed_trade_count",
                          "excluded_invalid_trade_count", "observed_native_record_count", "projection_version", "projection_position"):
                if field in raw:
                    row[field] = raw[field]
            if request.schema in {"bbo-1m", "bbo-1s", "cbbo-1m", "cbbo-1s"}:
                row["interval_end"] = stamp
                seconds = 60 if request.schema.endswith("1m") else 1
                row["interval_start"] = (instant(stamp) - timedelta(seconds=seconds)).isoformat()
            if request.source_kind == "cross_asset":
                root = re.match(r"^(ES|NQ|RTY|CL|GC|ZN|6E)(?:[._-]|[FGHJKMNQUVXZ]|$)", symbol)
                row["root_symbol"] = root.group(1) if root else None
                if raw.get("instrument_id") is not None:
                    row["symbol"] = symbol + "#" + str(raw["instrument_id"])
            if request.source_kind == "opra_options":
                if not re.fullmatch(re.escape(request.symbol) + r"\s*\d{6}[CP]\d{8}", symbol):
                    raise ValueError("OPRA mapping is outside the requested underlying")
                row.update(symbol=request.symbol, underlying_symbol=request.symbol, contract_symbol=symbol,
                    option_type="call" if re.search(r"\d{6}C\d{8}$", symbol) else "put")
                if request.schema in {"cmbp-1", "cbbo-1m", "cbbo-1s"}:
                    row["volume"] = None
                    row["volume_basis"] = "No quarter trade volume inferred from a quote's last-sale size"
            elif request.symbol != "*" and symbol != request.symbol:
                raise ValueError("Databento mapping is outside the requested equity")
            if request.source_kind == "fundamentals":
                row.update(published_at=stamp, publication_time_basis="provider_event_timestamp")
            if request.source_kind == "stock_trades" or (request.source_kind == "opra_options" and request.schema == "trades"):
                from .market_collection import canonical
                import hashlib
                identity = hashlib.sha256(canonical(raw)).hexdigest()
                count = occurrences.get(identity, 0)
                occurrences[identity] = count + 1
                row["trade_id"] = identity + ":" + str(count)
                row["identity_basis"] = "native_record_content_and_occurrence_within_exact_quarter"
            records.append(row)
        return CollectionResult(records, evidence=evidence)


def default_adapters(root, requests=()):
    return {"schwab": SchwabAdapter(root=root, requests=requests), "fmp": FmpAdapter(), "databento": DatabentoAdapter(root, requests=requests)}
