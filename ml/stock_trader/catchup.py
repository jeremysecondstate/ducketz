"""Net due accepted-plan intentions against actual fills and live reservations.

Manual holdings never enter the target equation: an unrelated manual buy/sale
must not be undone merely to restore an old absolute account position. Current
cash, quotes and eligible held inventory are enforced by the normal executor.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

import pandas as pd

from ml.stock_trader.contracts import PredictionSignal, canonical_sha256, utc


def native_catchup_plan(root: Path, *, action_date: str, source_run: Path | None = None):
    """Read the completed local direction ledger using its existing UI verifier.

    Earlier forecast-only publications keep their reader until a full v4
    direction-ledger plan exists. A declared v4 publication must verify; damage
    to a completed plan never silently falls back to other instructions.
    """
    root = Path(root).resolve()
    pointer = root / "ml/gameplan-trade-plan-latest/run.json"
    if not pointer.exists():
        return None
    current = json.loads(pointer.read_text(encoding="utf-8"))
    if current.get("schema_version") != "cash-aware-gameplan-trade-planning-v4":
        return None
    from app.ui.gameplan_data import load_gameplan
    from ml.artifacts import file_checksum
    ui = load_gameplan(root, action_date)
    run = ui.run_directory
    if run.parent != (root / "ml/gameplan-trade-plan-runs").resolve():
        raise ValueError("Native catch-up requires the local direction-ledger publication")
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    native = (root / str(report.get("source_gameplan_run", ""))).resolve()
    if (native.parent != (root / "ml/nightly-gameplan-runs").resolve()
            or source_run is not None and native != Path(source_run).resolve()
            or file_checksum(native / "receipt.json") != report.get("source_receipt_sha256")):
        raise ValueError("Local catch-up ledger differs from the loaded forecast source")
    ledger = json.loads((run / "direction-ledger.json").read_text(encoding="utf-8"))
    if ui.holding_policy != "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1":
        return None
    plan = {"action_date": action_date, "as_of": ui.saved_at.isoformat(), "ledger": ledger,
            "source_receipt_sha256": file_checksum(native / "receipt.json"),
            "trade_receipt_sha256": file_checksum(run / "receipt.json")}
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan, native, (native / "receipt.json", run / "receipt.json")


def _timeout_remainders(reservations):
    """Reconstruct consumed retries and permanently abandoned signed targets.

    Retry lineage belongs to the original order, not a changing catch-up
    prediction hash. Only a proven stop before submission releases its retry
    allowance. Unknown outcomes remain consuming attempts and stay reserved.
    """
    children = defaultdict(list)
    for reservation in reservations:
        parent_id = getattr(reservation, "retry_parent_id", None)
        local_rejection = (reservation.status == "REJECTED"
                           and not getattr(reservation, "broker_order_id", None)
                           and not getattr(reservation, "submitted_at", None)
                           and reservation.filled_quantity == 0)
        if parent_id and not local_rejection:
            children[parent_id].append(reservation)
    retryable, abandoned = [], []
    for parent in reservations:
        if (parent.status != "CANCELLED" or not getattr(parent, "timeout_requested_at", None)
                or getattr(parent, "retry_parent_id", None)):
            continue
        remaining = max(0, parent.quantity - parent.filled_quantity)
        attempts = children.get(parent.reservation_id, ())
        if not attempts:
            if remaining:
                retryable.append(parent)
            continue
        # Separate a reduction in the net economic intention (for example a
        # newly due opposite instruction) from executor capacity/size limits.
        # Only the latter leaves an unsent retry remainder to abandon.
        unsent = 0
        for child in attempts:
            requested = getattr(child, "retry_requested_quantity", None)
            if type(requested) is not int or not 0 < child.quantity <= requested <= remaining:
                raise ValueError("Timeout retry requires a valid frozen requested quantity")
            unsent += requested - child.quantity
        if unsent:
            abandoned.append((parent, unsent, "TIMEOUT_RETRY_UNSENT_REMAINDER_ABANDONED"))
        for child in attempts:
            # A broker-confirmed terminal cancellation consumes the attempt
            # even if an expiry/manual cancellation beat our timeout request.
            if child.status == "CANCELLED" and child.quantity > child.filled_quantity:
                abandoned.append((child, child.quantity - child.filled_quantity,
                                  "TIMEOUT_RETRY_REMAINDER_ABANDONED"))
    return retryable, abandoned


def catchup_signals(plan, *, as_of, source_fingerprint, reservations=(), blocked=None, abandoned=None):
    now = utc(as_of)
    local = now.tz_convert("America/Los_Angeles")
    if local.date().isoformat() != plan["action_date"] or not 4 <= local.hour < 17:
        return {}
    close = pd.Timestamp(f"{plan['action_date']} 17:00", tz="America/Los_Angeles").tz_convert("UTC")
    groups = defaultdict(list)
    for event in sorted(plan["ledger"]["events"], key=lambda event: (utc(event["timestamp"]), event["sequence"])):
        if utc(event["timestamp"]) > now:
            continue
        if event["action"] not in {"BUY", "SELL"} or type(event["quantity"]) is not int or event["quantity"] <= 0:
            raise ValueError("Catch-up requires frozen whole-share buy/sell quantities")
        # A planned fallback sale belongs to its selected donor's inventory.
        # Its frozen quantity remains capped by the synthesis fallback policy.
        horizon = event.get("cross_horizon_fallback", {}).get("donor_horizon", event["horizon"])
        groups[event["symbol"], horizon].append(event)
    signals = {}
    for (symbol, horizon), events in sorted(groups.items()):
        prefix = f"catchup:{plan['plan_sha256']}:{symbol}:{horizon}:"
        components = tuple(event["forecast_id"] for event in events)
        # A same-day upgrade can encounter reservations made by the original
        # per-forecast reader. Those exact component IDs already did the work.
        own = sorted((r for r in reservations if r.forecast_id.startswith(prefix) or r.forecast_id in components),
                     key=lambda r: r.reservation_id)
        planned = sum((1 if event["action"] == "BUY" else -1) * event["quantity"] for event in events)
        committed = sum((1 if r.side == "BUY" else -1) * (r.filled_quantity + r.reserved_quantity) for r in own)
        retryable, abandoned_remainders = _timeout_remainders(own)
        abandoned_delta = sum((1 if r.side == "BUY" else -1) * quantity
                              for r, quantity, _ in abandoned_remainders)
        if abandoned is not None:
            abandoned.extend({"symbol": symbol, "horizon": horizon, "direction": r.side,
                "reservation_id": r.reservation_id, "retry_parent_id": getattr(r, "retry_parent_id", None),
                "abandoned_quantity": quantity,
                "signed_abandoned_quantity": (1 if r.side == "BUY" else -1) * quantity,
                "reason_code": reason} for r, quantity, reason in abandoned_remainders)
        residual = planned - committed - abandoned_delta
        if not residual:
            continue
        direction = "BUY" if residual > 0 else "SELL"
        uncertain_retries = [r for r in own if r.status == "REJECTED" and r.side == direction
            and getattr(r, "retry_parent_id", None) and not getattr(r, "broker_order_id", None)
            and (getattr(r, "submitted_at", None) or r.filled_quantity)]
        if uncertain_retries:
            # Missing broker identity cannot prove that an attempted retry
            # stopped before POST. Never turn that ambiguity into a new order.
            if blocked is not None:
                blocked.append({"symbol": symbol, "horizon": horizon, "direction": direction,
                    "reason_code": "TIMEOUT_RETRY_OUTCOME_REQUIRES_REVIEW",
                    "reservation_id": uncertain_retries[0].reservation_id})
            continue
        rejected = [r for r in own if r.status == "REJECTED" and r.side == direction
                    and getattr(r, "broker_order_id", None)]
        if rejected:
            # A confirmed broker rejection is an unresolved failure, not a new
            # economic intention. Including it in the identity below used to
            # create an unlimited sequence of distinct, identical submissions.
            # Local pre-POST safety stops have no broker order identity and do
            # not trigger this guard. Genuine cancelled residuals still net.
            latest = max(rejected, key=lambda r: (getattr(r, "last_evidence_at", None) or "", r.reservation_id))
            if blocked is not None:
                blocked.append({"symbol": symbol, "horizon": horizon, "direction": direction,
                    "reason_code": "CONFIRMED_BROKER_REJECTION_REQUIRES_REVIEW",
                    "reservation_id": latest.reservation_id,
                    "observed_at": getattr(latest, "last_evidence_at", None),
                    "rejected_attempts": len(rejected)})
            continue
        retry_parent = next((r for r in retryable if r.side == direction), None)
        quantity = min(abs(residual), retry_parent.quantity - retry_parent.filled_quantity) if retry_parent else abs(residual)
        identity_inputs = {"components": components, "planned_delta": planned,
            "reservations": [(r.reservation_id, r.status, r.filled_quantity, r.reserved_quantity) for r in own]}
        if retry_parent or abandoned_remainders:
            identity_inputs.update(timeout_retry_parent_id=retry_parent.reservation_id if retry_parent else None,
                                   abandoned_delta=abandoned_delta)
        identity = canonical_sha256(identity_inputs)
        start = min(utc(event["timestamp"]) for event in events)
        signals[symbol, horizon] = PredictionSignal(
            symbol=symbol, primary_horizon=horizon, prediction_id=prefix + identity,
            decision_timestamp=start.isoformat(), target_window_start=start.isoformat(),
            target_window_end=close.isoformat(), actionable_until=close.isoformat(),
            prediction_created_at=utc(plan["as_of"]).isoformat(),
            calibrated_probability=1. if residual > 0 else 0., assumed_round_trip_cost=0.,
            horizon_probabilities={}, model_name="accepted-gameplan-net-intentions", model_version="v1",
            source_fingerprint=source_fingerprint, target_definition_version="accepted-gameplan-catchup-v1",
            planned_quantity=quantity, catchup_components=components,
            timeout_retry_parent_id=retry_parent.reservation_id if retry_parent else None,
            timeout_retry_quantity=quantity if retry_parent else None,
        )
    return signals
