"""Exact exchange-session evaluation of fixed 6/12-month research calls."""
from __future__ import annotations

import math

import pandas as pd

from ml.calendars import ExchangeSessionCalendar
from .policy import timestamp


def calendar(now: object) -> ExchangeSessionCalendar:
    current = timestamp(now)
    return ExchangeSessionCalendar("XNYS", start=(current - pd.DateOffset(years=3)).date(),
                                   end=(current + pd.DateOffset(years=3)).date())


def latest_session(now: object) -> str:
    current = timestamp(now)
    cal = calendar(current)
    sessions = cal.sessions[cal.sessions <= current.tz_localize(None).normalize()]
    for session in reversed(sessions):
        if cal.session_close(session) <= current:
            return session.date().isoformat()
    raise ValueError("No completed market session")


def windows(published_at: object) -> dict:
    published = timestamp(published_at)
    cal = calendar(published)
    candidates = cal.sessions[cal.sessions >= published.tz_localize(None).normalize()]
    entry = next(s for s in candidates if cal.session_open(s) > published)
    result = {"entry_session": entry.date().isoformat(), "entry_at": cal.session_open(entry).isoformat()}
    for months in (6, 12):
        anniversary = entry + pd.DateOffset(months=months)
        target = cal.sessions[cal.sessions >= anniversary][0]
        result[f"{months}m_session"] = target.date().isoformat()
        result[f"{months}m_at"] = cal.session_close(target).isoformat()
    return result


def _path(snapshot: dict | None, start: str, end: str, cal: ExchangeSessionCalendar,
          *, start_at_close: bool = False) -> dict:
    if snapshot is None or snapshot.get("error"):
        return {"status": "MISSING_PROVIDER_DATA"}
    if snapshot.get("basis") != "fmp_dividend_adjusted_ohlc":
        return {"status": "UNVERIFIED_ADJUSTMENT_BASIS"}
    selected = {}
    for row in snapshot["rows"]:
        if row.get("symbol", snapshot["symbol"]) != snapshot["symbol"]:
            return {"status": "WRONG_SECURITY"}
        session = row.get("date", "")
        if start <= session <= end:
            if session in selected:
                return {"status": "DUPLICATE_SESSION"}
            selected[session] = row
    required = [s.date().isoformat() for s in cal.sessions
                if start <= s.date().isoformat() <= end]
    if not required or any(s not in selected for s in required):
        return {"status": "MISSING_SESSIONS_OR_CORPORATE_ACTION"}
    try:
        entry = float(selected[start]["adjClose" if start_at_close else "adjOpen"])
        closes = [float(selected[s]["adjClose"]) for s in required]
        if entry <= 0 or not all(math.isfinite(x) and x > 0 for x in [entry, *closes]):
            raise ValueError("Invalid adjusted prices")
    except (KeyError, TypeError, ValueError):
        return {"status": "INVALID_PRICES"}
    # Both endpoints come from this same adjustment vintage, never from mixed fetches.
    wealth = [1.0, *(close / entry for close in closes)]
    peak, drawdown = 1.0, 0.0
    for value in wealth:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return {"status": "OBSERVED", "total_return": wealth[-1] - 1,
            "max_close_drawdown": drawdown, "entry_adjusted_open": entry,
            "end_adjusted_close": closes[-1], "sessions": len(required),
            "snapshot_sha256": snapshot["sha256"], "snapshot_path": snapshot["path"]}


def evaluate(call: dict, prices: dict[str, dict], now: object, frozen: dict | None = None) -> dict:
    current = timestamp(now)
    cal = calendar(call["published_at"])
    dates = call["windows"]
    entry, latest = dates["entry_session"], latest_session(current)
    result = {"call_id": call["call_id"], "security_id": call["security_id"],
              "symbol": call["symbol"], "evaluated_at": current.isoformat(),
              "entry_session": entry, "as_of_session": latest, "windows": {}}
    prior = frozen or {}
    for horizon in ("since_inception", "6m", "12m"):
        if horizon == "since_inception" and "12m" in prior:
            result["windows"][horizon] = prior["12m"]
            continue
        if horizon in prior:
            result["windows"][horizon] = prior[horizon]
            continue
        maturity = dates.get(f"{horizon}_at")
        end = min(latest, dates["12m_session"]) if horizon == "since_inception" else dates[f"{horizon}_session"]
        if timestamp(dates["entry_at"]) > current or entry > latest:
            result["windows"][horizon] = {"status": "PENDING_ENTRY"}
            continue
        if maturity and timestamp(maturity) > current:
            result["windows"][horizon] = {"status": "PENDING_MATURITY", "matures_at": maturity}
            continue
        series = {name: _path(prices.get(ticker), entry, end, cal) for name, ticker in (
            ("stock", call["symbol"]), ("broad", "SPY"), ("sector", call["sector_benchmark"]))}
        complete = all(v["status"] == "OBSERVED" for v in series.values())
        scored = {"status": "EVALUATED" if complete else "MISSING_DATA", "end_session": end,
                  "series": series, "evaluated_at": current.isoformat()}
        if complete:
            stock_return = series["stock"]["total_return"]
            scored.update(excess_vs_spy=stock_return - series["broad"]["total_return"],
                          excess_vs_sector=stock_return - series["sector"]["total_return"])
        result["windows"][horizon] = scored
    if "12m" in prior:
        result["week"] = {"status": "HORIZON_COMPLETE"}
    elif entry <= latest:
        previous_close = latest_session(current - pd.Timedelta(days=7))
        start = max(previous_close, entry)
        end = min(latest, dates["12m_session"])
        if start > end:
            result["week"] = {"status": "HORIZON_COMPLETE"}
        else:
            weekly = _path(prices.get(call["symbol"]), start, end, cal,
                           start_at_close=previous_close >= entry)
            result["week"] = {**weekly, "start_session": start, "end_session": end,
                              "basis": "previous_week_close" if previous_close >= entry else "inception_open"}
    else:
        result["week"] = {"status": "PENDING_ENTRY"}
    return result
