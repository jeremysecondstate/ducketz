"""Read-only display of the Powder journal; never creates a ledger or a signer."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from app.services.hyperliquid_paper_view import (
    HyperliquidPaperViewService, PaperViewSnapshot, SourceState, _number, _timestamp,
)


def _utc(value):
    value = _number(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def project_powder(report, *, paper=None, runtime=None, now=None):
    """Project only actual observations/fills; never subtract equity to invent P/L."""
    now = time.time() if now is None else now
    snapshot = PaperViewSnapshot(observed_at_utc=_utc(now))
    if paper:
        snapshot.forecasts = paper.forecasts
        snapshot.sources = {key: source for key, source in paper.sources.items()
                            if key != "ledger" and "paper" not in key.lower()}
    snapshot.runtime = dict(runtime or {})
    if runtime:
        reported = str(runtime.get("state", "UNKNOWN"))
        stamp = _timestamp(runtime.get("updated_at_utc"))
        status_age = now - stamp if stamp is not None else None
        live = runtime.get("process_alive")
        state = ("error" if reported == "HALTED" else "stopped" if reported == "STOPPED" or live is False
                 else "partial" if reported != "RUNNING" or live is not True
                 else "stale" if status_age is None or not 0 <= status_age <= 90 else "fresh")
        snapshot.sources["powder_runtime"] = SourceState("Powder runtime", state,
            runtime.get("updated_at_utc"), status_age, 30,
            f"Saved state: {reported}. " + str(runtime.get("reason") or "Process identity unverified."))
    observation = report.get("latest_observation") or {}
    observed = _number(observation.get("observed_at"))
    age = now - observed if observed is not None else None
    state = "missing" if observed is None else "fresh" if 0 <= age <= 90 else "stale"
    if report.get("status") == "error":
        state = "error"
    snapshot.sources["powder_ledger"] = SourceState(
        "Powder observations", state, _utc(observed), age, 30,
        report.get("error", "Actual exchange observations; P/L and drawdown await cashflow accounting."))
    snapshot.runtime["connected"] = bool(observation)
    snapshot.runtime["pending_intents"] = report.get("counts", {}).get("pending", 0)
    snapshot.portfolio_observed_at_utc = _utc(observed)
    snapshot.seed = report.get("metadata", {}).get("baseline") or {}
    binding = report.get("metadata", {}).get("binding") or {}
    snapshot.policy = binding.get("policy", {})
    for account, row in observation.get("accounts", {}).items():
        snapshot.accounts[account] = {"equity": row.get("equity"),
                                      "free_cash": row.get("available_cash"),
                                      "gross_exposure": row.get("gross")}
        for symbol, position in row.get("positions", {}).items():
            quantity, mark = _number(position.get("quantity")), _number(position.get("mark"))
            if quantity is None or mark is None or not quantity:
                continue
            entry = _number(position.get("entry_price"))
            kind = position.get("kind")
            snapshot.positions.append({"position_id": f"{account}:{kind}:{symbol}",
                "account": account, "coin": symbol, "kind": kind,
                "quantity": quantity, "mark_price": mark,
                "avg_entry": entry if entry and kind == "perp" else None,
                "notional": abs(quantity * mark), "side": "long" if quantity > 0 else "short",
                "unrealized_pnl": (mark - entry) * quantity if entry and kind == "perp" else None,
                "timestamp_utc": _utc(observed), "source": "exchange_observation"})
    if snapshot.accounts:
        for source, target in (("equity", "equity"), ("gross_exposure", "gross_exposure")):
            values = [_number(row.get(source)) for row in snapshot.accounts.values()]
            if all(value is not None for value in values):
                snapshot.pooled[target] = sum(values)
    for intent in report.get("intents", []):
        request = intent.get("request", {})
        decision = intent.get("decision", {})
        forecast = decision.get("forecast") or {}
        qualified = forecast.get("qualified")
        snapshot.decisions.append({**forecast, **decision, "decision_id": intent.get("id"),
            "qualification": "qualified" if qualified is True else "research" if qualified is False else "unavailable",
            "account": request.get("account"), "coin": request.get("symbol"),
            "kind": "spot" if request.get("account") == "clearpond" else "perp",
            "timestamp_utc": _utc(intent.get("created_at")), "action": intent.get("state"),
            "reason": intent.get("reason"), "details": intent})
    for fill in report.get("fills", []):
        raw = fill.get("raw") or fill
        size, price = _number(fill.get("quantity", raw.get("sz"))), _number(fill.get("price", raw.get("px")))
        if size is None or price is None:
            continue
        quantity = abs(size) * (-1 if raw.get("side") == "A" else 1) if "side" in raw else size
        stamp = _number(fill.get("time", raw.get("time")))
        snapshot.fills.append({**fill, "fill_id": f"{fill.get('account')}:{fill.get('tid')}",
            "coin": fill.get("symbol", fill.get("coin")),
            "kind": "spot" if fill.get("account") == "clearpond" else "perp",
            "quantity": quantity, "price": price, "notional": abs(size * price),
            "feeToken": fill.get("fee_token", raw.get("feeToken", "unknown token")),
            "timestamp_utc": _utc(stamp / 1000) if stamp is not None else None,
            "details": fill})
    warnings = []
    if state in {"stale", "error"}:
        warnings.append("Powder observations are " + state + "; inspect Operations before relying on these balances.")
    if snapshot.runtime.get("pending_intents"):
        warnings.append("Powder has unresolved orders; new submissions are blocked until reconciliation.")
    if snapshot.runtime.get("last_error"):
        warnings.append(str(snapshot.runtime["last_error"]))
    for name, source in snapshot.sources.items():
        if name == "powder_runtime" and source.state != "fresh":
            warnings.append(f"Powder runtime: {source.state}. {source.detail}")
    snapshot.warnings = tuple(warnings)
    return snapshot


class HyperliquidWorkspaceViewService(HyperliquidPaperViewService):
    def load_snapshot(self):
        paper = super().load_snapshot()
        # Importing the read helper has no database/runtime lifecycle effects.
        from ml.hyperliquid_powder_ledger import read_status
        report = read_status(self.data_root / "_powder" / "ledger.sqlite3", limit=self.journal_limit)
        runtime = {}
        path = self.data_root / "_powder" / "_runtime" / "status.json"
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("Powder status file is too large")
            runtime = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(runtime, dict):
                raise ValueError("Powder status must be an object")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            runtime = {"last_error": f"Powder runtime status unavailable: {exc}"}
        if runtime:
            runtime["process_alive"] = self.process_probe(runtime.get("pid"), "ml.hyperliquid_powder_runtime")
        paper.powder = project_powder(report, paper=paper, runtime=runtime, now=self.clock())
        return paper
