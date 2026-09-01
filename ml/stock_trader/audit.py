from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, write_manifest
from ml.current_publication import authoritative_receipt_runs
from ml.stock_trader.contracts import STOCK_TRADER_WEEKLY_AUDIT_SCHEMA_VERSION, finite, utc
from ml.stock_trader.publication import read_decision_run, read_execution_event


STOCK_TRADER_WEEKLY_AUDIT_RECEIPT_VERSION = "stock-trader-weekly-audit-receipt-v2"
STOCK_TRADER_WEEKLY_AUDIT_POINTER_VERSION = "stock-trader-weekly-audit-pointer-v2"


@dataclass(frozen=True)
class StockTraderWeeklyAuditResult:
    run_directory: Path
    report_path: Path
    markdown_path: Path
    status: str
    pair_count: int
    mature_pair_count: int


def build_stock_trader_weekly_audit(
    datastore_root: Path,
    *,
    window_start: object,
    window_end: object,
    evaluated_at: object | None = None,
) -> StockTraderWeeklyAuditResult:
    """Pair every stock decision explanation with its later market reality."""

    root = Path(datastore_root).resolve()
    start = utc(window_start)
    end = utc(window_end)
    timestamp = utc(evaluated_at)
    if end <= start:
        raise ValueError("Stock trader audit window_end must follow window_start")
    decisions, decision_sources = load_verified_decisions(
        root, window_start=start, window_end=end
    )
    prediction_handoffs = load_verified_prediction_handoffs(
        root, window_start=start, window_end=end
    )
    prediction_ids = {
        str(prediction.get("prediction_id"))
        for decision in decisions
        if isinstance((prediction := decision.get("prediction")), Mapping)
        and prediction.get("prediction_id")
    }
    evaluations, evaluation_sources = _load_loop_b_evaluations(root, prediction_ids)
    pairs = [
        _pair_decision_with_reality(
            root,
            decision,
            evaluation=evaluations.get(_prediction_id(decision)),
            evaluated_at=timestamp,
        )
        for decision in decisions
    ]
    summary = _audit_summary(pairs)
    summary["prediction_handoff_runs"] = _prediction_handoff_run_summary(
        prediction_handoffs
    )
    status = (
        "NO_STOCK_TRADER_DECISIONS"
        if not pairs
        else "OUTCOMES_PENDING"
        if summary["pending_pair_count"] > 0
        else "WEEKLY_AUDIT_COMPLETE"
    )
    report: dict[str, object] = {
        "schema_version": STOCK_TRADER_WEEKLY_AUDIT_SCHEMA_VERSION,
        "status": status,
        "evaluated_at": timestamp.isoformat(),
        "window": {
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
        },
        "scope": {
            "asset_class": "EQUITY",
            "stock_symbols_only": True,
            "options_trading_included": False,
            "pairing_key": "decision_id",
            "market_outcome_source": "receipt-verified Loop B evaluations",
        },
        "summary": summary,
        "prediction_handoffs": prediction_handoffs,
        "decision_outcome_pairs": pairs,
    }
    run = create_timestamp_directory(
        root / "ml" / "stock-trader-weekly-audits", timestamp=timestamp
    )
    report_path = run / "audit.json"
    markdown_path = run / "audit.md"
    _write_json_atomic(report_path, report)
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    manifest_path = write_manifest(
        run,
        run_timestamp=timestamp,
        input_files=tuple(dict.fromkeys((*decision_sources, *evaluation_sources))),
        output_files=(report_path.name, markdown_path.name),
        configuration={
            "authority": "EVALUATION_ONLY",
            "window": report["window"],
            "pairing_key": "decision_id",
            "broker_mutation_performed": False,
            "model_mutation_performed": False,
        },
        datastore_root=root,
    )
    receipt_path = run / "receipt.json"
    receipt = {
        "schema_version": STOCK_TRADER_WEEKLY_AUDIT_RECEIPT_VERSION,
        "run_path": run.relative_to(root).as_posix(),
        "evaluated_at": timestamp.isoformat(),
        "status": status,
        "pair_count": len(pairs),
        "mature_pair_count": summary["mature_pair_count"],
        "manifest_sha256": file_checksum(manifest_path),
        "audit_sha256": file_checksum(report_path),
        "markdown_sha256": file_checksum(markdown_path),
    }
    _write_json_atomic(receipt_path, receipt)
    pointer_path = root / "ml" / "stock-trader-weekly-audit-latest" / "run.json"
    _write_json_atomic(
        pointer_path,
        {
            "schema_version": STOCK_TRADER_WEEKLY_AUDIT_POINTER_VERSION,
            "run_path": receipt["run_path"],
            "evaluated_at": timestamp.isoformat(),
            "receipt_sha256": file_checksum(receipt_path),
            "manifest_sha256": receipt["manifest_sha256"],
        },
    )
    return StockTraderWeeklyAuditResult(
        run_directory=run,
        report_path=report_path,
        markdown_path=markdown_path,
        status=status,
        pair_count=len(pairs),
        mature_pair_count=int(summary["mature_pair_count"]),
    )


def load_verified_decisions(
    datastore_root: Path,
    *,
    window_start: object,
    window_end: object,
) -> tuple[list[dict[str, object]], tuple[Path, ...]]:
    root = Path(datastore_root).resolve()
    start = utc(window_start)
    end = utc(window_end)
    decisions_by_id: dict[str, dict[str, object]] = {}
    sources: list[Path] = []
    runs_root = root / "ml" / "stock-trader-decision-runs"
    if not runs_root.is_dir():
        return [], ()
    for run in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        payload, _receipt = read_decision_run(root, run)
        raw_handoff = payload.get("prediction_handoff")
        prediction_handoff = (
            dict(raw_handoff) if isinstance(raw_handoff, Mapping) else {}
        )
        raw_decisions = payload.get("decisions")
        if not isinstance(raw_decisions, list):
            raise ValueError(f"Stock trader decision run has no decisions array: {run}")
        for raw_decision in raw_decisions:
            if not isinstance(raw_decision, Mapping):
                raise ValueError(f"Stock trader decision is not an object: {run}")
            decided_at = utc(raw_decision.get("decided_at"))
            if start <= decided_at < end:
                decision_id = str(raw_decision.get("decision_id") or "")
                if not decision_id:
                    raise ValueError(f"Stock trader decision has no decision_id: {run}")
                existing = decisions_by_id.get(decision_id)
                if existing is None:
                    decision = dict(raw_decision)
                    decision["duplicate_decision_receipt_count"] = 0
                    decision["prediction_handoff"] = prediction_handoff
                    decisions_by_id[decision_id] = decision
                else:
                    existing["duplicate_decision_receipt_count"] = (
                        int(existing.get("duplicate_decision_receipt_count", 0)) + 1
                    )
        sources.extend(
            (run / "decisions.json", run / "manifest.json", run / "receipt.json")
        )
    output = list(decisions_by_id.values())
    output.sort(key=lambda item: (str(item.get("decided_at")), str(item.get("symbol"))))
    return output, tuple(dict.fromkeys(sources))


def load_verified_prediction_handoffs(
    datastore_root: Path,
    *,
    window_start: object,
    window_end: object,
) -> list[dict[str, object]]:
    root = Path(datastore_root).resolve()
    start = utc(window_start)
    end = utc(window_end)
    output: list[dict[str, object]] = []
    runs_root = root / "ml" / "stock-trader-decision-runs"
    if not runs_root.is_dir():
        return output
    for run in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        payload, _receipt = read_decision_run(root, run)
        decided_at = utc(payload.get("decided_at"))
        handoff = payload.get("prediction_handoff")
        if start <= decided_at < end and isinstance(handoff, Mapping) and handoff:
            output.append(
                {
                    "run_path": run.relative_to(root).as_posix(),
                    "decided_at": decided_at.isoformat(),
                    **dict(handoff),
                }
            )
    return output


def _load_loop_b_evaluations(
    root: Path, prediction_ids: set[str]
) -> tuple[dict[str, dict[str, object]], tuple[Path, ...]]:
    if not prediction_ids:
        return {}, ()
    runs = authoritative_receipt_runs(root)
    matches: dict[str, tuple[pd.Timestamp, dict[str, object]]] = {}
    sources: list[Path] = []
    for run, promoted_at in sorted(runs.items(), key=lambda item: item[1]):
        path = run / "evaluations.parquet"
        if not path.is_file():
            continue
        frame = pd.read_parquet(path)
        required = {
            "id",
            "evaluation_status",
            "evaluated_at",
            "observed_forward_raw_return",
            "observed_forward_cost_adjusted_return",
            "assumed_round_trip_cost",
            "target_window_end",
        }
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError(
                f"Loop B evaluation artifact is missing columns at {path}: "
                + ", ".join(missing)
            )
        selected = frame.loc[frame["id"].astype("string").isin(prediction_ids)]
        for _, row in selected.iterrows():
            prediction_id = str(row["id"])
            evaluated_at = pd.to_datetime(row["evaluated_at"], utc=True, errors="coerce")
            ranking = (
                pd.Timestamp(evaluated_at)
                if not pd.isna(evaluated_at)
                else pd.Timestamp(promoted_at)
            )
            current = matches.get(prediction_id)
            if current is None or ranking >= current[0]:
                matches[prediction_id] = (ranking, _jsonable_row(row.to_dict()))
        sources.extend(
            path_value
            for path_value in (
                path,
                run / "manifest.json",
                run / "publication.json",
            )
            if path_value.is_file()
        )
    return (
        {prediction_id: row for prediction_id, (_ranking, row) in matches.items()},
        tuple(dict.fromkeys(sources)),
    )


def _pair_decision_with_reality(
    root: Path,
    decision: Mapping[str, object],
    *,
    evaluation: Mapping[str, object] | None,
    evaluated_at: pd.Timestamp,
) -> dict[str, object]:
    decision_id = str(decision.get("decision_id") or "")
    prediction = decision.get("prediction")
    prediction_row = prediction if isinstance(prediction, Mapping) else {}
    enrichment = decision.get("enrichment")
    enrichment_row = enrichment if isinstance(enrichment, Mapping) else {}
    quote = decision.get("quote")
    quote_row = quote if isinstance(quote, Mapping) else {}
    action = str(decision.get("action") or "NO_TRADE")
    suggested_action = str(decision.get("suggested_action") or "NONE")
    direction = action if action in {"BUY", "SELL"} else suggested_action
    sign = 1.0 if direction == "BUY" else -1.0 if direction == "SELL" else 0.0
    midpoint = _midpoint(quote_row)
    hypothetical_quantity = int(finite(decision.get("hypothetical_quantity"), default=0.0) or 0)
    selected_quantity = int(finite(decision.get("quantity"), default=0.0) or 0)
    execution = read_execution_event(root, decision_id) if decision_id else None
    execution_result = (
        execution.get("result")
        if isinstance(execution, Mapping) and isinstance(execution.get("result"), Mapping)
        else {}
    )
    reconciliation = (
        execution.get("reconciliation")
        if isinstance(execution, Mapping)
        and isinstance(execution.get("reconciliation"), Mapping)
        else {}
    )
    filled_quantity = max(
        0.0, finite(reconciliation.get("filled_quantity"), default=0.0) or 0.0
    )
    average_fill_price = finite(reconciliation.get("average_fill_price"))
    observed_raw: float | None = None
    observed_up_cost_adjusted: float | None = None
    aligned_raw: float | None = None
    aligned_net: float | None = None
    counterfactual_dollars: float | None = None
    selected_dollars: float | None = None
    filled_dollars: float | None = None
    fill_slippage_return: float | None = None
    expected_error: float | None = None
    evaluation_status = "NOT_YET_EVALUATED"
    target_end = prediction_row.get("target_window_end")
    if evaluation is not None:
        evaluation_status = str(evaluation.get("evaluation_status") or "UNKNOWN")
        target_end = evaluation.get("target_window_end") or target_end
        if evaluation_status == "EVALUATED":
            observed_raw = finite(evaluation.get("observed_forward_raw_return"))
            observed_up_cost_adjusted = finite(
                evaluation.get("observed_forward_cost_adjusted_return")
            )
            cost = max(
                0.0,
                finite(evaluation.get("assumed_round_trip_cost"), default=0.0) or 0.0,
            )
            if observed_raw is not None and sign:
                aligned_raw = sign * observed_raw
                aligned_net = aligned_raw - cost
                if midpoint is not None:
                    counterfactual_dollars = (
                        hypothetical_quantity * midpoint * aligned_net
                    )
                    selected_dollars = selected_quantity * midpoint * aligned_net
                    if average_fill_price is not None and filled_quantity > 0.0:
                        fill_slippage_return = (
                            (average_fill_price - midpoint) / midpoint
                            if direction == "BUY"
                            else (midpoint - average_fill_price) / midpoint
                        )
                        filled_dollars = (
                            filled_quantity
                            * midpoint
                            * (aligned_net - fill_slippage_return)
                        )
                expected = finite(decision.get("expected_net_return"))
                if expected is not None:
                    expected_error = aligned_net - expected
    if evaluation_status != "EVALUATED" and target_end is not None:
        try:
            if utc(target_end) <= evaluated_at:
                evaluation_status = "MATURE_OUTCOME_UNAVAILABLE"
        except (TypeError, ValueError):
            evaluation_status = "TARGET_WINDOW_INVALID"
    if not prediction_row.get("prediction_id"):
        evaluation_status = "NOT_EVALUABLE_NO_PREDICTION"
        pair_status = "NOT_EVALUABLE"
    else:
        pair_status = (
            "PAIRED_MATURE_OUTCOME" if evaluation_status == "EVALUATED" else "PENDING"
        )
    return {
        "decision_id": decision_id,
        "duplicate_decision_receipt_count": int(
            finite(decision.get("duplicate_decision_receipt_count"), default=0.0)
            or 0
        ),
        "pair_status": pair_status,
        "decided_at": decision.get("decided_at"),
        "symbol": decision.get("symbol"),
        "decision_lane": decision.get("decision_lane", "LIVE"),
        "prediction_id": prediction_row.get("prediction_id"),
        "prediction_horizon": prediction_row.get("primary_horizon"),
        "target_definition_version": prediction_row.get(
            "target_definition_version"
        ),
        "checkpoint_session": prediction_row.get(
            "checkpoint_session", "REGULAR"
        ),
        "suggested_action": suggested_action,
        "decision_action": action,
        "quantity": selected_quantity,
        "hypothetical_quantity": hypothetical_quantity,
        "decision_reason_code": decision.get("decision_reason_code"),
        "decision_reason": decision.get("decision_reason"),
        "order_style_reason_code": decision.get("order_style_reason_code"),
        "order_style_reason": decision.get("order_style_reason"),
        "order_type": decision.get("order_type"),
        "limit_price": decision.get("limit_price"),
        "reference_midpoint": midpoint,
        "expected_net_return": decision.get("expected_net_return"),
        "expected_net_dollars": decision.get("expected_net_dollars"),
        "trade_probability": decision.get("trade_probability"),
        "allocation_fraction": decision.get("allocation_fraction"),
        "execution_urgency": decision.get("execution_urgency"),
        "prediction_handoff": (
            dict(decision.get("prediction_handoff"))
            if isinstance(decision.get("prediction_handoff"), Mapping)
            else {}
        ),
        "execution_status": execution_result.get("status", "NOT_SUBMITTED"),
        "broker_order_id": execution_result.get("broker_order_id"),
        "broker_reconciliation": {
            "status": reconciliation.get("reconciliation_status", "NOT_RECONCILED"),
            "observed_at": reconciliation.get("observed_at"),
            "broker_status": reconciliation.get("broker_status"),
            "filled_quantity": filled_quantity,
            "remaining_quantity": reconciliation.get("remaining_quantity"),
            "average_fill_price": average_fill_price,
            "fill_count": reconciliation.get("fill_count", 0),
            "decision_midpoint_slippage_return": fill_slippage_return,
        },
        "market_reality": {
            "status": evaluation_status,
            "evaluated_at": evaluation.get("evaluated_at") if evaluation else None,
            "target_window_end": target_end,
            "observed_forward_raw_return": observed_raw,
            "observed_up_cost_adjusted_return": observed_up_cost_adjusted,
            "direction_used": direction,
            "direction_aligned_raw_return": aligned_raw,
            "direction_aligned_net_return": aligned_net,
            "hypothetical_quantity_result_dollars": counterfactual_dollars,
            "selected_quantity_result_dollars": selected_dollars,
            "filled_quantity_slippage_adjusted_result_dollars": filled_dollars,
            "net_expected_value_error": expected_error,
        },
        "model": {
            "name": enrichment_row.get("model_name"),
            "version": enrichment_row.get("model_version"),
            "model_fingerprint": enrichment_row.get("model_fingerprint"),
            "feature_values": enrichment_row.get("feature_values", {}),
        },
        "policy_version": decision.get("policy_version"),
        "policy_fingerprint": decision.get("policy_fingerprint"),
    }


def _audit_summary(pairs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    decision_reasons: Counter[str] = Counter()
    order_styles: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    lanes: Counter[str] = Counter()
    checkpoint_sessions: Counter[str] = Counter()
    target_definitions: Counter[str] = Counter()
    handoff_statuses: Counter[str] = Counter()
    reason_results: dict[str, list[float]] = defaultdict(list)
    style_results: dict[str, list[float]] = defaultdict(list)
    handoff_results: dict[str, list[float]] = defaultdict(list)
    checkpoint_session_results: dict[str, list[float]] = defaultdict(list)
    target_definition_results: dict[str, list[float]] = defaultdict(list)
    fallback_decisions = 0
    mature = 0
    pending = 0
    non_evaluable = 0
    total_counterfactual = 0.0
    total_selected = 0.0
    total_filled = 0.0
    for pair in pairs:
        reason = str(pair.get("decision_reason_code") or "UNKNOWN")
        style = str(pair.get("order_style_reason_code") or "UNKNOWN")
        action = str(pair.get("decision_action") or "UNKNOWN")
        lane = str(pair.get("decision_lane") or "LIVE")
        checkpoint_session = str(
            pair.get("checkpoint_session") or "REGULAR"
        ).upper()
        target_definition = str(
            pair.get("target_definition_version") or "UNRECORDED"
        )
        decision_reasons[reason] += 1
        order_styles[style] += 1
        actions[action] += 1
        lanes[lane] += 1
        checkpoint_sessions[checkpoint_session] += 1
        target_definitions[target_definition] += 1
        handoff = pair.get("prediction_handoff")
        handoff_row = handoff if isinstance(handoff, Mapping) else {}
        handoff_status = str(handoff_row.get("status") or "NOT_RECORDED")
        handoff_statuses[handoff_status] += 1
        fallback_decisions += int(bool(handoff_row.get("fallback_used")))
        reality = pair.get("market_reality")
        if pair.get("pair_status") == "NOT_EVALUABLE":
            non_evaluable += 1
            continue
        if not isinstance(reality, Mapping) or reality.get("status") != "EVALUATED":
            pending += 1
            continue
        mature += 1
        aligned = finite(reality.get("direction_aligned_net_return"))
        if aligned is not None:
            reason_results[reason].append(aligned)
            style_results[style].append(aligned)
            handoff_results[handoff_status].append(aligned)
            checkpoint_session_results[checkpoint_session].append(aligned)
            target_definition_results[target_definition].append(aligned)
        total_counterfactual += finite(
            reality.get("hypothetical_quantity_result_dollars"), default=0.0
        ) or 0.0
        total_selected += finite(
            reality.get("selected_quantity_result_dollars"), default=0.0
        ) or 0.0
        total_filled += finite(
            reality.get("filled_quantity_slippage_adjusted_result_dollars"),
            default=0.0,
        ) or 0.0
    return {
        "decision_count": len(pairs),
        "mature_pair_count": mature,
        "pending_pair_count": pending,
        "non_evaluable_pair_count": non_evaluable,
        "actions": dict(sorted(actions.items())),
        "decision_lanes": dict(sorted(lanes.items())),
        "checkpoint_session_counts": dict(sorted(checkpoint_sessions.items())),
        "target_definition_counts": dict(sorted(target_definitions.items())),
        "prediction_handoff_status_counts": dict(sorted(handoff_statuses.items())),
        "fallback_decision_count": fallback_decisions,
        "decision_reason_counts": dict(sorted(decision_reasons.items())),
        "order_style_reason_counts": dict(sorted(order_styles.items())),
        "decision_reason_outcomes": _group_results(decision_reasons, reason_results),
        "order_style_outcomes": _group_results(order_styles, style_results),
        "prediction_handoff_outcomes": _group_results(
            handoff_statuses, handoff_results
        ),
        "checkpoint_session_outcomes": _group_results(
            checkpoint_sessions, checkpoint_session_results
        ),
        "target_definition_outcomes": _group_results(
            target_definitions, target_definition_results
        ),
        "aggregate_hypothetical_result_dollars": round(total_counterfactual, 2),
        "aggregate_selected_quantity_result_dollars": round(total_selected, 2),
        "aggregate_filled_quantity_slippage_adjusted_result_dollars": round(
            total_filled, 2
        ),
    }


def _group_results(
    counts: Mapping[str, int], results: Mapping[str, Sequence[float]]
) -> dict[str, object]:
    output: dict[str, object] = {}
    for name, count in sorted(counts.items()):
        values = list(results.get(name, ()))
        output[name] = {
            "decision_count": count,
            "mature_count": len(values),
            "mean_direction_aligned_net_return": (
                sum(values) / len(values) if values else None
            ),
            "positive_outcome_rate": (
                sum(value > 0.0 for value in values) / len(values) if values else None
            ),
        }
    return output


def _prediction_handoff_run_summary(
    handoffs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    statuses = Counter(str(row.get("status") or "UNKNOWN") for row in handoffs)
    waits = [
        value
        for row in handoffs
        if (value := finite(row.get("wait_seconds"))) is not None
    ]
    return {
        "run_count": len(handoffs),
        "status_counts": dict(sorted(statuses.items())),
        "fallback_run_count": sum(bool(row.get("fallback_used")) for row in handoffs),
        "mean_wait_seconds": sum(waits) / len(waits) if waits else None,
        "maximum_wait_seconds": max(waits) if waits else None,
    }


def _render_markdown(report: Mapping[str, object]) -> str:
    window = report.get("window") if isinstance(report.get("window"), Mapping) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    pairs = report.get("decision_outcome_pairs")
    rows = pairs if isinstance(pairs, list) else []
    lines = [
        "# Stock trader weekly decision/outcome audit",
        "",
        f"Status: `{report.get('status')}`  ",
        f"Window: `{window.get('start')}` to `{window.get('end_exclusive')}`  ",
        f"Decisions / mature outcomes: `{summary.get('decision_count', 0)} / {summary.get('mature_pair_count', 0)}`",
        f"Receipt handoffs / fallback handoffs: `{summary.get('prediction_handoff_runs', {}).get('run_count', 0) if isinstance(summary.get('prediction_handoff_runs'), Mapping) else 0} / {summary.get('prediction_handoff_runs', {}).get('fallback_run_count', 0) if isinstance(summary.get('prediction_handoff_runs'), Mapping) else 0}`",
        "",
        "Every explanation is joined to its later market result by the stable `decision_id`.",
        "",
        "| Time | Session | Symbol | Decision | Handoff | Decision reason | Order-style reason | Outcome | Aligned net return | Hypothetical result |",
        "|---|---|---:|---:|---|---|---|---|---:|---:|",
    ]
    for pair in rows:
        if not isinstance(pair, Mapping):
            continue
        reality = pair.get("market_reality")
        reality_row = reality if isinstance(reality, Mapping) else {}
        aligned = finite(reality_row.get("direction_aligned_net_return"))
        dollars = finite(reality_row.get("hypothetical_quantity_result_dollars"))
        handoff = pair.get("prediction_handoff")
        handoff_row = handoff if isinstance(handoff, Mapping) else {}
        handoff_label = str(handoff_row.get("status") or "NOT_RECORDED")
        if handoff_row.get("fallback_used"):
            handoff_label += " (fallback)"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(pair.get("decided_at")),
                    _cell(pair.get("checkpoint_session")),
                    _cell(pair.get("symbol")),
                    _cell(pair.get("decision_action")),
                    _cell(handoff_label),
                    _cell(pair.get("decision_reason_code")),
                    _cell(pair.get("order_style_reason_code")),
                    _cell(reality_row.get("status")),
                    f"{aligned:.4%}" if aligned is not None else "--",
                    f"${dollars:,.2f}" if dollars is not None else "--",
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "The result dollars are counterfactual at the decision midpoint unless a future fill-reconciliation artifact supplies exact fill economics.",
            "",
        )
    )
    return "\n".join(lines)


def _prediction_id(decision: Mapping[str, object]) -> str:
    prediction = decision.get("prediction")
    return (
        str(prediction.get("prediction_id") or "")
        if isinstance(prediction, Mapping)
        else ""
    )


def _midpoint(quote: Mapping[str, object]) -> float | None:
    bid = finite(quote.get("bid"))
    ask = finite(quote.get("ask"))
    return (bid + ask) / 2.0 if bid is not None and ask is not None else None


def _jsonable_row(row: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in row.items():
        if value is pd.NA or (isinstance(value, float) and pd.isna(value)):
            output[str(key)] = None
        elif isinstance(value, pd.Timestamp):
            output[str(key)] = value.isoformat()
        elif hasattr(value, "item"):
            try:
                output[str(key)] = value.item()
            except ValueError:
                output[str(key)] = str(value)
        else:
            output[str(key)] = value
    return output


def _cell(value: object) -> str:
    return str(value if value is not None else "--").replace("|", "\\|")


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pair hourly stock-trader decisions with mature Loop B outcomes."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument(
        "--window-start",
        help="UTC/offset timestamp; omit both window flags for the latest completed XNYS week.",
    )
    parser.add_argument(
        "--window-end",
        help="Exclusive UTC/offset timestamp; omit both window flags for the latest completed XNYS week.",
    )
    parser.add_argument("--evaluated-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir, target=args.datastore_target
        )
        if bool(args.window_start) != bool(args.window_end):
            raise ValueError("Provide both --window-start and --window-end, or neither")
        evaluated_at = utc(args.evaluated_at)
        if args.window_start:
            window_start = args.window_start
            window_end = args.window_end
        else:
            # Reuse the repository's exchange-calendar definition so the
            # Saturday job handles holidays and early closes identically to
            # the Loop C weekly review.
            from ml.loop_c.weekly_review import resolve_weekly_review_window

            window = resolve_weekly_review_window(evaluated_at)
            window_start = window.session_open
            window_end = window.session_close + pd.Timedelta(nanoseconds=1)
        with exclusive_runtime_lock(
            root / "locks" / "stock-trader-weekly-audit.lock",
            process_name="stock-trader-weekly-audit",
        ):
            result = build_stock_trader_weekly_audit(
                root,
                window_start=window_start,
                window_end=window_end,
                evaluated_at=evaluated_at,
            )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(
        json.dumps(
            {
                "status": result.status,
                "run_directory": str(result.run_directory),
                "pair_count": result.pair_count,
                "mature_pair_count": result.mature_pair_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "StockTraderWeeklyAuditResult",
    "build_stock_trader_weekly_audit",
    "load_verified_decisions",
    "load_verified_prediction_handoffs",
    "main",
]
