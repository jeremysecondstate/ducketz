"""Read-only rolling price changes, independent of saved Gameplan evidence.

One refresh freezes an aware UTC instant T and requests native Schwab one-minute
history through the existing authenticated market-data session. Both endpoints
come from the *same response*, extended hours requested, on the provider's native
historical price basis. Quotes, previous-close fields, cached files and other
providers are never spliced into that basis. No dividend/total-return adjustment
is applied. A fresh read-only FMP split response is required for each symbol.
The existing split validator normalizes reported corporate split events only
when boundary prices confirm the factor; already
reflected factors are not applied twice and ambiguous boundaries fail closed.

Candles are open-labelled: their close is eligible only at open + one minute.
The displayed observation timestamp is that completion bound, not an invented
trade timestamp inside the candle. Only finite, positive, positive-volume native
candles are admitted. No interpolation or synthetic carry-forward candles.

The closure policy uses the installed XNAS calendar (holidays/DST/early closes).
Expected native equity history sessions are 04:00--20:00 Eastern on full days
and 04:00--17:00 on 13:00 early-close days (the provider's holiday FAQ).
This conservative market-data envelope includes premarket observations rather
than treating the broker's later order-entry opening as a market closure. A price is stale
after ten *expected trading minutes* have elapsed without an observation. Closed
time does not consume that allowance, but missing open-session evidence does.
Thus a weekend can legitimately carry Friday's close at both cutoffs; a missing
Friday afternoon cannot be disguised as a weekend closure. Actual endpoints and
their ages remain available to the UI. This is a price return, not total return.

Calendar/source references (policy, not runtime network dependencies):
https://www.schwab.com/stocks/extended-hours-trading
https://workplace.schwab.com/story/how-does-extended-hours-trading-work
https://nasdaqtrader.com/Trader.aspx?id=Calendar
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from pathlib import Path
import queue
import threading
import time as monotonic_time
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from datafetching.symbol_universe import production_watchlist_path, read_symbols

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
SOURCE = "Schwab · native 1-minute history · extended hours · FMP split validation"
PRICE_SOURCE = "schwab:pricehistory:1m"
PRICE_BASIS = "Schwab same-response historical scale; fresh FMP split check; reported splits normalized"
POLICY = ("T minus 24 elapsed UTC hours; latest completed positive-volume 1m close at/before each cutoff. "
          "XNAS holidays; 04–20 Eastern, early-close days 04–17. Closed time may carry the last actual close; "
          "over 10 expected trading minutes missing is stale. Same-response native history; freshly checked FMP splits "
          "normalized, ambiguous boundaries unavailable. No interpolation or dividend total return.")
MINUTE = timedelta(minutes=1)
MAX_OPEN_AGE = timedelta(minutes=10)
LOOKBACK = timedelta(days=10)
BATCH_TIMEOUT_SECONDS = 45.0
MAX_CONCURRENT_REQUESTS = 3


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Price cutoffs and observations require aware datetimes")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class PriceObservation:
    price: float
    observed_at: datetime
    source: str = PRICE_SOURCE
    basis: str = PRICE_BASIS
    interval_start: datetime | None = None


@dataclass(frozen=True)
class SymbolPriceChange:
    symbol: str
    change_pct: float | None
    status: str
    reason: str
    current: PriceObservation | None = None
    previous: PriceObservation | None = None


@dataclass(frozen=True)
class RollingPriceSnapshot:
    as_of: datetime
    rows: tuple[SymbolPriceChange, ...]
    source: str = SOURCE
    policy: str = POLICY
    error: str | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)

    @property
    def available_count(self) -> int:
        return len(self._valid_changes())

    @property
    def total_count(self) -> int:
        return len(self.rows)

    def _valid_changes(self) -> tuple[float, ...]:
        return tuple(row.change_pct for row in self.rows
                     if row.status in {"available", "closed"} and row.change_pct is not None
                     and math.isfinite(row.change_pct))

    @property
    def average_pct(self) -> float | None:
        values = self._valid_changes()
        return math.fsum(values) / len(values) if values and len(values) == len(self.rows) else None

    @property
    def partial_average_pct(self) -> float | None:
        values = self._valid_changes()
        return math.fsum(values) / len(values) if values else None


@dataclass(frozen=True)
class MarketSession:
    expected_open: datetime
    expected_close: datetime
    eligible_open: datetime


def market_sessions(as_of: datetime) -> tuple[MarketSession, ...]:
    """Build a small calendar window; no provider or saved-history reads."""
    import exchange_calendars as xcals

    stamp = utc(as_of)
    calendar = xcals.get_calendar("XNAS", start=(stamp - LOOKBACK - timedelta(days=2)).date(),
                                 end=(stamp + timedelta(days=2)).date())
    result = []
    for _, row in calendar.schedule.iterrows():
        opened = row["open"].to_pydatetime().astimezone(EASTERN)
        closed = row["close"].to_pydatetime().astimezone(EASTERN)
        full_day = opened.time().replace(tzinfo=None) == time(9, 30) and closed.time().replace(tzinfo=None) == time(16)
        if full_day:
            expected_open = datetime.combine(opened.date(), time(4), EASTERN)
            expected_close = datetime.combine(opened.date(), time(20), EASTERN)
            eligible_open = datetime.combine(opened.date(), time(4), EASTERN)
        elif closed.time().replace(tzinfo=None) == time(13):
            expected_open = datetime.combine(opened.date(), time(4), EASTERN)
            expected_close = datetime.combine(opened.date(), time(17), EASTERN)
            eligible_open = datetime.combine(opened.date(), time(4), EASTERN)
        else:
            # An unusual emergency short session has no assumed extension.
            expected_open, expected_close, eligible_open = opened, closed, opened
        result.append(MarketSession(utc(expected_open), utc(expected_close), utc(eligible_open)))
    return tuple(result)


def _open_elapsed(start: datetime, end: datetime, sessions: Iterable[MarketSession]) -> timedelta:
    return sum((max(timedelta(0), min(end, session.expected_close) - max(start, session.expected_open))
                for session in sessions), timedelta(0))


def _is_closed(cutoff: datetime, sessions: Iterable[MarketSession]) -> bool:
    return not any(session.expected_open < cutoff <= session.expected_close for session in sessions)


def observations_from_payload(payload: object, *, sessions: tuple[MarketSession, ...]) -> tuple[PriceObservation, ...]:
    """Admit only native one-minute positive-volume closes; later revisions win."""
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("Native price history has no candles list")
    # Do not silently ignore explicit contradictory metadata in a future adapter.
    for key in ("corporate_action_adjustment", "price_basis"):
        if key in payload and payload[key] not in {None, "none", "unadjusted_market_scale", PRICE_BASIS}:
            raise ValueError("Native price history has incompatible adjustment metadata")
    by_stamp = {}
    for candle in payload["candles"]:
        if not isinstance(candle, dict):
            continue
        try:
            stamp = datetime.fromtimestamp(float(candle["datetime"]) / 1000.0, UTC)
            price, volume = float(candle["close"]), float(candle["volume"])
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            continue
        if (stamp.second or stamp.microsecond or not math.isfinite(price) or price <= 0
                or not math.isfinite(volume) or volume <= 0):
            continue
        end = stamp + MINUTE
        if not any(session.eligible_open <= stamp and end <= session.expected_close for session in sessions):
            continue
        by_stamp[stamp] = PriceObservation(price, end, interval_start=stamp)
    return tuple(by_stamp[stamp] for stamp in sorted(by_stamp))


def normalize_known_splits(payload: object, *, events: tuple = (), as_of: datetime | None = None) -> dict:
    """Reuse the shared split validator; never apply a factor twice.

    Known split boundaries must match either the native split ratio (back-adjust
    older rows) or approximately one (already reflected by the provider).
    Ambiguous boundaries and unexplained >3x discontinuities fail closed. With
    no known split event this preserves provider-native history; the source has
    no adjustment switch. The loader requires a fresh successful corporate-action
    response before invoking this helper; no stale/missing-file fallback is used.
    """
    import pandas as pd
    from technicals.split_adjustments import RATIO_TOLERANCE, SplitEvent, apply_split_adjustments

    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("Native price history has no candles list")
    if not payload["candles"]:
        return payload
    frame = pd.DataFrame(payload["candles"])
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not required <= set(frame.columns):
        raise ValueError("Native price history lacks original OHLCV")
    frame["timestamp"] = pd.to_datetime(frame["datetime"], unit="ms", utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    valid = frame["timestamp"].notna()
    if as_of is not None:
        valid &= frame["timestamp"] + pd.Timedelta(minutes=1) <= pd.Timestamp(utc(as_of))
    for column in ("open", "high", "low", "close"):
        valid &= frame[column].map(lambda v: math.isfinite(v) and v > 0)
    valid &= frame["volume"].map(lambda v: math.isfinite(v) and v > 0)
    frame = frame.loc[valid].drop_duplicates("timestamp", keep="last")
    # FMP's ex-date is a civil date, not UTC midnight. In winter the previous
    # day's extended trading runs until 01:00 UTC and must remain pre-split.
    effective_events = tuple(SplitEvent(pd.Timestamp(datetime.combine(event.ex_date.date(), time(4), EASTERN)).tz_convert("UTC"),
        event.numerator, event.denominator, event.source_file) for event in events)
    # A small split can fall inside both tolerance bands. The shared helper's
    # branch ordering cannot establish whether it is already reflected then.
    for event in effective_events:
        pre = frame.loc[frame["timestamp"] < event.ex_date].sort_values("timestamp")
        post = frame.loc[frame["timestamp"] >= event.ex_date].sort_values("timestamp")
        if pre.empty or post.empty:
            continue
        ratio = float(pre.iloc[-1]["close"]) / float(post.iloc[0]["open"])
        if (abs(math.log(ratio)) <= math.log(RATIO_TOLERANCE)
                and abs(math.log(ratio / event.split_ratio)) <= math.log(RATIO_TOLERANCE)):
            raise ValueError("Ambiguous split boundary matches both adjusted and raw price scales")
    result = apply_split_adjustments(frame, events=effective_events, provider="schwab", timeframe="1m")
    return {**payload, "candles": result.frame.drop(columns="timestamp").to_dict(orient="records")}


def split_events_from_payload(symbol: str, payload: object) -> tuple:
    """Validate a fresh FMP split response; confirmed [] means no listed splits.

    A missing file, error object, null response, malformed event or conflicting
    ratios cannot stand in for a successful empty corporate-action response.
    The stable endpoint returns rows; its historical wrapper remains supported.
    """
    import pandas as pd
    from technicals.split_adjustments import SplitEvent

    if isinstance(payload, dict):
        if payload.get("symbol") not in (None, symbol) or not isinstance(payload.get("historical"), list):
            raise ValueError("Split response is not a confirmed symbol history")
        payload = payload["historical"]
    if not isinstance(payload, (list, tuple)) or len(payload) > 20000:
        raise ValueError("Split response is missing or exceeds bounded event policy")
    events = []
    for row in payload:
        if not isinstance(row, dict) or row.get("symbol", symbol) != symbol:
            raise ValueError("Split response row has an invalid symbol")
        if not {"date", "numerator", "denominator"} <= row.keys():
            raise ValueError("Split response event lacks date or ratio")
        ex_date = pd.Timestamp(date.fromisoformat(str(row["date"])), tz="UTC")
        numerator, denominator = float(row["numerator"]), float(row["denominator"])
        if pd.isna(ex_date) or not math.isfinite(numerator) or not math.isfinite(denominator) or min(numerator, denominator) <= 0:
            raise ValueError("Corporate split event has invalid date or ratio")
        if numerator != denominator:
            events.append(SplitEvent(ex_date, numerator, denominator, Path("FMP-live-splits-response")))
    by_date = {}
    for event in events:
        if event.ex_date in by_date and event.split_ratio != by_date[event.ex_date].split_ratio:
            raise ValueError("Corporate split response contains conflicting ratios")
        by_date[event.ex_date] = event
    return tuple(by_date[date] for date in sorted(by_date))


def _fetch_split_payload(symbol: str):
    from app.services.fmp_corporate_data import FmpCorporateDataProvider
    # The existing Schwab session has already initialized the application's
    # normal configuration/.env binding. Reuse the existing FMP subscription.
    return FmpCorporateDataProvider(timeout_seconds=10)._get_json("splits", {"symbol": symbol})


def calculate_change(symbol: str, observations: Iterable[PriceObservation], *, as_of: datetime,
                     sessions: tuple[MarketSession, ...]) -> SymbolPriceChange:
    stamp = utc(as_of)
    previous_cutoff = stamp - timedelta(hours=24)
    ordered = sorted((observation for observation in observations
                      if math.isfinite(observation.price) and observation.price > 0),
                     key=lambda observation: utc(observation.observed_at))
    current = next((o for o in reversed(ordered) if utc(o.observed_at) <= stamp), None)
    previous = next((o for o in reversed(ordered) if utc(o.observed_at) <= previous_cutoff), None)
    if current is None or previous is None:
        return SymbolPriceChange(symbol, None, "unavailable", "Missing eligible price at one or both cutoffs", current, previous)
    if current.source != previous.source or current.basis != previous.basis:
        return SymbolPriceChange(symbol, None, "unavailable", "Prices have incompatible source or adjustment basis", current, previous)
    for name, observation, cutoff in (("Current", current, stamp), ("24h reference", previous, previous_cutoff)):
        observed = utc(observation.observed_at)
        if observed < stamp - LOOKBACK:
            return SymbolPriceChange(symbol, None, "stale", f"{name} exceeds bounded history policy", current, previous)
        if _open_elapsed(observed, cutoff, sessions) > MAX_OPEN_AGE:
            return SymbolPriceChange(symbol, None, "stale", f"{name} is over 10 expected market minutes old", current, previous)
    change = 100.0 * (current.price / previous.price - 1.0)
    if not math.isfinite(change):
        return SymbolPriceChange(symbol, None, "unavailable", "Price ratio is not finite", current, previous)
    closed = _is_closed(stamp, sessions) or _is_closed(previous_cutoff, sessions)
    reason = ("Market closed at a cutoff; latest eligible actual close carried across closed time"
              if closed else "Latest eligible completed native close at each UTC cutoff")
    return SymbolPriceChange(symbol, change, "closed" if closed else "available", reason, current, previous)


def local_symbols(repository_root: Path | None = None) -> tuple[str, ...]:
    """Production watchlist only: ignore process candidate-universe overrides."""
    return read_symbols(production_watchlist_path(repository_root))


def unavailable_snapshot(as_of: datetime, error: str, *, symbols: Iterable[str] | None = None,
                         repository_root: Path | None = None) -> RollingPriceSnapshot:
    try:
        selected = tuple(symbols) if symbols is not None else local_symbols(repository_root)
    except Exception:
        selected = ()
    return RollingPriceSnapshot(utc(as_of), tuple(SymbolPriceChange(symbol, None, "unavailable", error)
                                                for symbol in selected), error=error)


def _session_factory():
    # Existing authenticated market-data capability. No accounts, orders, saved
    # reviews, persistent retries, credentials, or provider substitutions here.
    from datafetching.schwab_fetch import DataFetchingSchwabSession
    return DataFetchingSchwabSession()


def load_rolling_price_change(*, as_of: datetime | None = None, repository_root: Path | None = None,
                             session_factory: Callable | None = None,
                             timeout_seconds: float = BATCH_TIMEOUT_SECONDS,
                             split_fetcher: Callable | None = None) -> RollingPriceSnapshot:
    """Fresh bounded batch; safe to call from an independent background worker.

    Existing Schwab history and FMP split requests each use a ten-second HTTP timeout.
    Three daemon readers bound fan-out; a 45s batch deadline also bounds session
    initialization/auth/slow readers. Late results are discarded, not cached.
    """
    stamp = utc(as_of or datetime.now(UTC))
    try:
        symbols = local_symbols(repository_root)
    except Exception:
        return unavailable_snapshot(stamp, "Local production watchlist unavailable", symbols=())
    try:
        sessions = market_sessions(stamp)
    except Exception:
        return unavailable_snapshot(stamp, "Exchange calendar unavailable", symbols=symbols)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Batch timeout must be finite and positive")
    pending, replies = queue.Queue(), queue.Queue()
    for symbol in symbols:
        pending.put(symbol)
    factory = session_factory or _session_factory
    fetch_splits = split_fetcher or _fetch_split_payload
    session_lock = threading.Lock()
    shared = {}
    stop = threading.Event()
    deadline = monotonic_time.monotonic() + timeout_seconds

    def provider():
        with session_lock:
            if "error" in shared:
                raise RuntimeError(shared["error"])
            if "session" not in shared:
                try:
                    shared["session"] = factory()
                except Exception as exc:
                    shared["error"] = type(exc).__name__
                    raise
            return shared["session"]

    def read_batch():
        while not stop.is_set() and monotonic_time.monotonic() < deadline:
            try:
                symbol = pending.get_nowait()
            except queue.Empty:
                return
            try:
                selected_provider = provider()
                if stop.is_set() or monotonic_time.monotonic() >= deadline:
                    return
                raw = selected_provider.get_price_history(symbol, period_type="day", period=None,
                    frequency_type="minute", frequency=1, need_extended_hours_data=True,
                    start_datetime=stamp - LOOKBACK, end_datetime=stamp)
                if isinstance(raw, dict) and raw.get("symbol") not in (None, symbol):
                    raise ValueError("History symbol mismatch")
                if stop.is_set() or monotonic_time.monotonic() >= deadline:
                    return
                try:
                    split_events = split_events_from_payload(symbol, fetch_splits(symbol))
                except Exception as exc:
                    result = SymbolPriceChange(symbol, None, "unavailable", f"Fresh split evidence unavailable ({type(exc).__name__})")
                    replies.put(result)
                    continue
                raw = normalize_known_splits(raw, events=split_events, as_of=stamp)
                observations = observations_from_payload(raw, sessions=sessions)
                result = calculate_change(symbol, observations, as_of=stamp, sessions=sessions)
            except Exception as exc:
                result = SymbolPriceChange(symbol, None, "unavailable", f"Market history unavailable ({type(exc).__name__})")
            replies.put(result)

    for index in range(min(MAX_CONCURRENT_REQUESTS, len(symbols))):
        threading.Thread(target=read_batch, name=f"rolling-price-reader-{index}", daemon=True).start()
    results = {}
    while len(results) < len(symbols):
        left = deadline - monotonic_time.monotonic()
        if left <= 0:
            break
        try:
            result = replies.get(timeout=left)
        except queue.Empty:
            break
        results[result.symbol] = result
    stop.set()
    return RollingPriceSnapshot(stamp, tuple(results.get(symbol, SymbolPriceChange(symbol, None,
        "unavailable", "Market history request timed out")) for symbol in symbols))
