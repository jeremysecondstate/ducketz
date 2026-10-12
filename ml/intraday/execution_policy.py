"""Pure offline plans for the settled cutoff and new-forecast replacement rules.

These helpers neither submit nor cancel orders, release commitments, size a new
intent, nor establish market eligibility. They inspect a validated OfflineBook
snapshot. Live broker integration, shared account migration and eventual Scout
execution remain separate work. A ready plan only permits the caller to continue
its other checks and must be evaluated again immediately before submission.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Literal

from .book import Fill, Request, VERSION
from .prediction import ContractError, PACIFIC, encoded, identity, instant, number, quantity

POLICY_VERSION = "intraday-new-forecast-replaces-unfilled-strict-1645-pacific-v1"


@dataclass(frozen=True)
class SubmissionGate:
    state: Literal["before_submission_cutoff", "submission_cutoff", "wrong_session_date"]
    checked_at: str
    eligible_session: str
    cutoff_at: str
    reason: str


def submission_gate(*, as_of: str | datetime, eligible_session: date) -> SubmissionGate:
    """Reject submission at/after 16:45 Pacific, with DST from the zone database.

    Before-cutoff does not mean the market is open, the session is eligible,
    orders are authorized or an input/model/quote is usable. The caller supplies
    an independently established eligible session date; this helper does not
    invent calendars, an overnight target or a first eligible submission time.
    """
    if type(eligible_session) is not date:
        raise ContractError("An explicit eligible session date is required")
    checked = instant(as_of)
    local = checked.astimezone(PACIFIC)
    cutoff = datetime.combine(eligible_session, time(16, 45), tzinfo=PACIFIC)
    if local.date() != eligible_session:
        state, reason = "wrong_session_date", "Submission date differs from the supplied eligible session"
    elif checked >= instant(cutoff):
        state, reason = "submission_cutoff", "New submissions are forbidden at or after 16:45 Pacific"
    else:
        state, reason = "before_submission_cutoff", "Time check only; market, model, sizing, quote and account checks remain"
    return SubmissionGate(state, checked.isoformat(), eligible_session.isoformat(), instant(cutoff).isoformat(), reason)


@dataclass(frozen=True)
class PriorOrderPlan:
    """Unfilled-order planning only; filled BUY costs and holdings stay accounted.

    A resolved unfilled commitment does not release filled BUY cash costs while
    account balances catch up. The authority retains that separate accounting;
    this pure plan never releases or mutates any purchasing/share capacity.
    """
    request_id: str
    forecast_id: str
    status: str
    action: Literal["retrieve_status_then_cancel_unfilled", "reconcile_status_and_cancellation",
                    "reconcile_unsent_reservation", "preserve_terminal_evidence", "reject_unknown_state",
                    "recover_existing_intention"]
    unfilled_commitment_retained: bool
    preserved_fills: tuple[Fill, ...]
    reason: str


@dataclass(frozen=True)
class ForecastTransitionPlan:
    state: Literal["ready_for_other_checks", "reconcile_required", "submission_cutoff", "wrong_session_date",
                   "existing_intention", "invalid_order_state"]
    request_id: str
    forecast_id: str
    symbol: str
    stream: str
    submission_time: SubmissionGate
    prior_orders: tuple[PriorOrderPlan, ...]
    reason: str
    policy_version: str = POLICY_VERSION


def _validate_scope(snapshot: Mapping, intent: Request):
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("binding"), Mapping):
        raise ContractError("Validated OfflineBook snapshot required")
    binding = snapshot["binding"]
    if binding.get("version") != VERSION or binding.get("executor") != "Atlas":
        raise ContractError("This offline helper preserves the current Atlas-only book scope")
    identity(binding.get("account_id"))
    for field in ("atlas_symbols", "gameplan_symbols"):
        symbols = binding.get(field)
        if not isinstance(symbols, (list, tuple)) or not symbols or len(set(symbols)) != len(symbols):
            raise ContractError("Offline book must preserve explicit unique symbol membership")
        for symbol in symbols:
            identity(symbol)
    if not set(binding["atlas_symbols"]) <= set(binding["gameplan_symbols"]):
        raise ContractError("Atlas intraday scope differs from the book membership")
    if (intent.stream != "atlas-15m" or intent.horizon != "15m"
            or intent.symbol not in binding["atlas_symbols"]):
        raise ContractError("New forecast intention is outside this offline intraday stream scope")
    for field in ("orders", "fills", "evidence"):
        if not isinstance(snapshot.get(field), Mapping):
            raise ContractError("Offline snapshot lacks durable orders, fills or complete status evidence")


def _validate_order(request_id, order, snapshot, *, checked_at):
    """Check the relationships this plan relies on, not independent book admission."""
    if not isinstance(order, Mapping) or not isinstance(order.get("request"), Mapping):
        raise ContractError("Malformed persisted order")
    request = order["request"]
    for field in ("request_id", "forecast_id", "symbol", "stream", "horizon"):
        identity(request.get(field))
    if request_id != request["request_id"]:
        raise ContractError("Persistent order identity differs from its snapshot key")
    if order.get("account_id") != snapshot["binding"]["account_id"]:
        raise ContractError("Persisted order belongs to another book account")
    permitted = quantity(order.get("permitted"), positive=True)
    filled = quantity(order.get("filled"))
    if filled > permitted:
        raise ContractError("Recorded fills exceed the committed order")
    if request.get("side") not in {"BUY", "SELL"}:
        raise ContractError("Persisted order side is invalid")
    if quantity(request.get("desired"), positive=True) < permitted:
        raise ContractError("Committed quantity exceeds its original desired quantity")
    number(request.get("limit"), positive=True)
    fills = []
    for fill_id, raw in snapshot["fills"].items():
        if not isinstance(raw, Mapping):
            raise ContractError("Malformed durable fill evidence")
        if raw.get("request_id") != request_id:
            continue
        if raw.get("fill_id") != fill_id:
            raise ContractError("Persistent fill identity differs from its snapshot key")
        identity(fill_id)
        quantity(raw.get("quantity"), positive=True)
        number(raw.get("price"), positive=True)
        if instant(raw.get("executed_at")) > checked_at:
            raise ContractError("Recorded fill is unavailable at the planning time")
        fills.append(Fill(fill_id, raw["quantity"], raw["price"], raw["executed_at"]))
    if sum(fill.quantity for fill in fills) != filled:
        raise ContractError("Cumulative quantity differs from the preserved identified fills")
    status = order.get("status")
    if not isinstance(status, str):
        raise ContractError("Persisted order must have an explicit status")
    if status in {"RESERVED", "SUBMITTING", "UNKNOWN", "WORKING"} and filled:
        raise ContractError("Persisted execution quantities disagree with order status")
    if status == "PARTIAL" and not 0 < filled < permitted:
        raise ContractError("Partial status needs a filled and an unfilled portion")
    if status == "FILLED" and filled != permitted:
        raise ContractError("Filled status must preserve the complete executed quantity")
    if status == "CANCELLED" and filled == permitted:
        raise ContractError("A fully executed order must remain FILLED rather than CANCELLED")
    if status in {"CANCELLED", "REJECTED", "NOT_ACCEPTED", "FILLED", "WORKING", "PARTIAL"}:
        observed = order.get("last_observed")
        if observed is None or instant(observed) > checked_at or not order.get("broker_id"):
            raise ContractError("Resolved/working state needs available identified complete status evidence")
        matching = []
        for evidence in snapshot["evidence"].values():
            if (isinstance(evidence, Mapping) and evidence.get("request_id") == request_id
                    and evidence.get("broker_id") == order["broker_id"] and evidence.get("status") == status
                    and instant(evidence.get("observed_at")) == instant(observed)
                    and evidence.get("complete") is True and evidence.get("order_quantity") == permitted):
                matching.append(evidence)
        retained = {fill.fill_id: asdict(fill) for fill in fills}
        if not any(isinstance(evidence.get("fills"), (list, tuple))
                   and len(evidence["fills"]) == len(retained)
                   and all(isinstance(fill, Mapping) and isinstance(fill.get("fill_id"), str)
                           for fill in evidence["fills"])
                   and {fill["fill_id"] for fill in evidence["fills"]} == set(retained)
                   and all(isinstance(fill, Mapping) and retained.get(fill.get("fill_id")) == dict(fill)
                           for fill in evidence["fills"]) for evidence in matching):
            raise ContractError("Complete status evidence must preserve every intervening fill")
    if status in {"REJECTED", "NOT_ACCEPTED"} and filled:
        raise ContractError("Nonacceptance cannot erase executed fills")
    return request, status, tuple(sorted(fills, key=lambda fill: fill.fill_id))


def plan_new_forecast(snapshot: Mapping, *, intent: Request, as_of: str | datetime,
                      eligible_session: date) -> ForecastTransitionPlan:
    """New forecast replaces prior unfilled intention only after reconciliation.

    The function never changes a snapshot or copies/subtracts prior quantities
    into the new intent. Completed shares remain filled history. A caller must
    retrieve authoritative status and reconcile intervening fills, confirm
    cancellation of the unfilled portion before reserving/submitting the
    independently sized new intention. Sizing and intention persistence may
    precede cancellation. A cancel request or acknowledgment is insufficient.
    Same-forecast repricing/remainder replacement is a different procedure.
    """
    if not isinstance(intent, Request):
        raise ContractError("Explicit immutable Request intention required")
    for value in (intent.request_id, intent.forecast_id, intent.symbol, intent.stream):
        identity(value)
    # A newly rounded zero quantity still supersedes an old unfilled intention.
    # A zero limit is the existing pre-quote placeholder, never an order price.
    quantity(intent.desired)
    number(intent.limit)
    if intent.side not in {"BUY", "SELL"} or intent.parent_request is not None:
        raise ContractError("This helper plans a new forecast, not same-forecast remainder repricing")
    gate = submission_gate(as_of=as_of, eligible_session=eligible_session)
    checked = instant(gate.checked_at)
    _validate_scope(snapshot, intent)
    plans, existing = [], False
    for request_id, order in sorted(snapshot["orders"].items()):
        request, status, fills = _validate_order(request_id, order, snapshot, checked_at=checked)
        same_scope = request["symbol"] == intent.symbol and request["stream"] == intent.stream
        if request_id == intent.request_id:
            original = request.get("sizing", {}).get("original_intent", request)
            if encoded(original) != encoded(asdict(intent)):
                raise ContractError("Persistent request identity reused with changed intention")
            existing = True
        elif same_scope and request["forecast_id"] == intent.forecast_id:
            raise ContractError("Forecast already has a persistent intention; recover it instead of duplicating")
        if not same_scope:
            continue
        if request["horizon"] != "15m":
            raise ContractError("Intraday stream order carries an inconsistent horizon")
        if status not in {"RESERVED", "WORKING", "PARTIAL", "SUBMITTING", "UNKNOWN", "CANCEL_PENDING",
                          "FILLED", "CANCELLED", "REJECTED", "NOT_ACCEPTED"}:
            action, retained, reason = "reject_unknown_state", True, "Unrecognized order state cannot authorize replacing or releasing the commitment"
        elif request_id == intent.request_id:
            action, retained, reason = "recover_existing_intention", status not in {"FILLED", "CANCELLED", "REJECTED", "NOT_ACCEPTED"}, "Existing identity must be recovered; never create a second intention"
        elif status in {"WORKING", "PARTIAL"}:
            action, retained, reason = "retrieve_status_then_cancel_unfilled", True, "Reconcile current status and intervening fills, then confirm cancellation of the unfilled portion"
        elif status in {"SUBMITTING", "UNKNOWN", "CANCEL_PENDING"}:
            action, retained, reason = "reconcile_status_and_cancellation", True, "Acceptance/cancellation remains unresolved; retain identity and commitment while status is retried"
        elif status == "RESERVED":
            action, retained, reason = "reconcile_unsent_reservation", True, "Confirm submission never started and explicitly release the unsent reservation before a new intention"
        elif status in {"FILLED", "CANCELLED", "REJECTED", "NOT_ACCEPTED"}:
            action, retained, reason = "preserve_terminal_evidence", False, "Terminal complete status and all executed fills remain durable; size the new forecast independently"
        plans.append(PriorOrderPlan(request_id, request["forecast_id"], status, action, retained, fills, reason))
    if any(plan.action == "reject_unknown_state" for plan in plans):
        state, reason = "invalid_order_state", "Unknown prior order state requires explicit reconciliation"
    elif existing:
        state, reason = "existing_intention", "Recover the original intention; the time gate applies to any eventual submission"
    elif any(plan.unfilled_commitment_retained for plan in plans):
        state, reason = "reconcile_required", "Prior unfilled intention must be resolved first; reconciliation continues even after cutoff"
    elif gate.state != "before_submission_cutoff":
        state, reason = gate.state, gate.reason
    else:
        state, reason = "ready_for_other_checks", "Replacement policy is satisfied; model, session, fresh quote, sizing and shared capacity checks still apply"
    return ForecastTransitionPlan(state, intent.request_id, intent.forecast_id, intent.symbol, intent.stream,
                                  gate, tuple(plans), reason)
