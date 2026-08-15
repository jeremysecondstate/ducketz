from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.decision_time import CycleTargetDecision, cycle_target_decision
from datafetching.fred_vintages import materialize_current_fred_rate_receipt
from datafetching.orchestrate import DEFAULT_WATCHLIST, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.option_pricing.candidate import freeze_candidate, read_current_candidate
from ml.option_pricing.eligibility import (
    read_current_eligibility_report,
    read_eligibility_policy,
)
from ml.option_pricing.lockbox import read_lockbox_result
from ml.option_pricing.loop_native_eligibility import (
    LOOP_NATIVE_ELIGIBILITY_PROTOCOL_VERSION,
    read_current_loop_native_eligibility_policy,
    read_current_loop_native_eligibility_report,
)
from ml.option_pricing.operations import (
    EXIT_EVIDENCE,
    EXIT_OK,
    operational_preflight_report,
    publish_operational_readiness,
    read_current_operational_readiness,
    read_current_runtime_health,
    rollback_option_pricing_pointer,
)
from ml.option_pricing.publication import (
    diagnose_option_pricing_publications,
    read_current_option_pricing_publication,
    recover_option_pricing_orphan,
)
from ml.option_pricing.rates import (
    load_point_in_time_rate_observations,
    rate_coverage_report,
)
from ml.option_pricing.strategy_outcomes import (
    build_strategy_outcome_evidence,
    publish_strategy_outcome_evidence,
    read_current_strategy_outcome_evidence,
)
from options.publication import committed_option_snapshots
from ml.universe import PRODUCTION_OPTION_ROUTE_COUNT


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Option Pricing research evidence and production diagnostic artifacts."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Verify and print current evidence status")
    commands.add_parser(
        "readiness",
        help="Print a concise, read-only production-readiness plan",
    )
    commands.add_parser(
        "capture-current-rate",
        help="Persist the latest FRED receipt for future causal Pricing cycles",
    )
    commands.add_parser(
        "strategy-evaluate",
        help="Publish a causal Strategy shadow outcome comparison",
    )
    commands.add_parser(
        "operational-preflight",
        help="Publish installation, CLI, capacity, chain, and benchmark checks",
    )
    commands.add_parser(
        "freeze-candidate",
        help="Freeze the current run only after all offline gates pass",
    )
    rollback = commands.add_parser(
        "rollback-pointer",
        help="Restore the immediately prior verified Pricing pointer",
    )
    rollback.add_argument("--authorization-record", type=Path, required=True)
    commands.add_parser(
        "diagnose-publications",
        help="Read-only verification of pointer divergence and orphan runs",
    )
    recover = commands.add_parser(
        "recover-orphan",
        help="Promote one verified child orphan with a separate authorization record",
    )
    recover.add_argument("--run-directory", type=Path, required=True)
    recover.add_argument("--authorization-record", type=Path, required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    if args.command == "status":
        return _status(root)
    if args.command == "readiness":
        return _readiness(root)
    if args.command == "capture-current-rate":
        paths = materialize_current_fred_rate_receipt(root)
        observations, _ = load_point_in_time_rate_observations(root)
        latest = (
            observations.iloc[-1].to_dict()
            if observations is not None and not observations.empty
            else {}
        )
        _print_json(
            {
                "status": "PASS" if paths and latest else "NOT_PROVEN",
                "datastore": str(root),
                "paths": [str(path) for path in paths],
                "available_at": latest.get("available_at"),
                "risk_free_rate": latest.get("risk_free_rate"),
                "historical_coverage_claimed": False,
                "automated_action_allowed": False,
            }
        )
        return EXIT_OK if paths and latest else EXIT_EVIDENCE
    if args.command == "strategy-evaluate":
        observations, report, sources = build_strategy_outcome_evidence(root)
        published = publish_strategy_outcome_evidence(
            root,
            observations=observations,
            report=report,
            source_files=sources,
        )
        _print_json(published)
        return EXIT_OK if published.get("status") == "PASS" else EXIT_EVIDENCE
    if args.command == "operational-preflight":
        report = operational_preflight_report(root)
        published = publish_operational_readiness(root, report=report)
        _print_json(published)
        return EXIT_OK if published.get("status") == "PASS" else EXIT_EVIDENCE
    if args.command == "freeze-candidate":
        publication = read_current_option_pricing_publication(root)
        eligibility = read_current_eligibility_report(root)
        policy_reference = eligibility.report.get("eligibility_policy")
        policy_reference = (
            policy_reference if isinstance(policy_reference, Mapping) else {}
        )
        policy = read_eligibility_policy(
            Path(str(policy_reference.get("path", ""))),
            datastore_root=root,
        )
        candidate = freeze_candidate(
            root,
            pricing_run=publication.run_directory,
            policy_artifact=policy,
            eligibility_report=eligibility.report,
        )
        _print_json(candidate)
        return EXIT_OK
    if args.command == "rollback-pointer":
        receipt = rollback_option_pricing_pointer(
            root,
            authorization_path=args.authorization_record,
        )
        _print_json(receipt)
        return EXIT_OK
    if args.command == "diagnose-publications":
        diagnosis = diagnose_option_pricing_publications(root)
        _print_json(diagnosis)
        return (
            EXIT_OK
            if diagnosis.get("pointer_status") in {"VERIFIED", "MISSING"}
            else EXIT_EVIDENCE
        )
    if args.command == "recover-orphan":
        publication = recover_option_pricing_orphan(
            root,
            run_directory=args.run_directory,
            authorization_record=args.authorization_record,
        )
        _print_json(
            {
                "status": "RECOVERED",
                "run_path": publication.run_directory.relative_to(root).as_posix(),
                "published_at": publication.receipt.get("published_at"),
                "automated_action_allowed": False,
            }
        )
        return EXIT_OK
    parser.error("unsupported command")
    return 2


def _status(root: Path) -> int:
    status: dict[str, object] = {
        "datastore": str(root),
        "automated_action_allowed": False,
    }
    failures = 0
    for name, reader in (
        ("pricing", lambda: _pricing_status(root)),
        ("eligibility", lambda: read_current_eligibility_report(root).report),
        ("candidate", lambda: read_current_candidate(root)),
        ("strategy", lambda: read_current_strategy_outcome_evidence(root)),
        ("operational", lambda: read_current_operational_readiness(root)),
    ):
        try:
            value = reader()
            if value is None:
                status[name] = {
                    "status": "NOT_PROVEN",
                    "reason": f"No current {name} artifact",
                }
                failures += 1
            else:
                status[name] = value
        except Exception as exc:
            status[name] = {"status": "INVALID", "reason": f"{type(exc).__name__}: {exc}"}
            failures += 1
    candidate = status.get("candidate")
    candidate_id = (
        str(candidate.get("candidate_id", "")).strip()
        if isinstance(candidate, Mapping)
        else ""
    )
    if candidate_id:
        try:
            status["lockbox"] = read_lockbox_result(
                root, candidate_id=candidate_id
            )
        except Exception as exc:
            status["lockbox"] = {
                "status": "NOT_PROVEN",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            failures += 1
    else:
        status["lockbox"] = {
            "status": "NOT_PROVEN",
            "reason": "No verified frozen candidate exists",
        }
        failures += 1
    _print_json(status)
    return EXIT_EVIDENCE if failures else EXIT_OK


def _readiness(root: Path) -> int:
    """Render current blockers without publishing or mutating evidence."""

    try:
        eligibility = read_current_eligibility_report(root).report
    except Exception as exc:
        _print_json(
            {
                "datastore": str(root),
                "gate_status": "NOT_PRODUCTION_ELIGIBLE",
                "reason": f"{type(exc).__name__}: {exc}",
                "automated_action_allowed": False,
            }
        )
        return EXIT_EVIDENCE

    market_decision = cycle_target_decision()
    rate_observations, _ = load_point_in_time_rate_observations(root)
    next_pricing_cycle = market_decision.next_eligible_cycle(
        phase_offset_minutes=1
    )
    next_target = next_pricing_cycle - pd.Timedelta(minutes=1)
    next_rate_coverage = rate_coverage_report(
        rate_observations,
        target_snapshot_fors=(next_target,),
    )
    next_live_rate_inputs = _live_rate_input_report(
        root,
        symbols=read_watchlist(DEFAULT_WATCHLIST),
        target_snapshot_for=next_target,
        prediction_created_at=next_pricing_cycle,
        rate_observations=rate_observations,
    )
    summary = build_readiness_summary(
        datastore_root=root,
        eligibility=eligibility,
        operational=_optional_status(
            lambda: read_current_operational_readiness(root)
        ),
        strategy=_optional_status(
            lambda: read_current_strategy_outcome_evidence(root)
        ),
        candidate=_optional_status(lambda: read_current_candidate(root)),
        health=_optional_status(lambda: read_current_runtime_health(root)),
        market_decision=market_decision,
        next_rate_coverage=next_rate_coverage,
        next_live_rate_inputs=next_live_rate_inputs,
    )
    summary["loop_native_v3"] = _loop_native_readiness_view(root)
    _print_json(summary)
    return (
        EXIT_OK
        if summary.get("gate_status") == "PRODUCTION_ELIGIBLE"
        else EXIT_EVIDENCE
    )


def build_readiness_summary(
    *,
    datastore_root: Path,
    eligibility: Mapping[str, object],
    operational: Mapping[str, object] | None,
    strategy: Mapping[str, object] | None,
    candidate: Mapping[str, object] | None,
    health: Mapping[str, object] | None,
    market_decision: CycleTargetDecision,
    next_rate_coverage: Mapping[str, object] | None = None,
    next_live_rate_inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the compact operator view used by the read-only readiness command."""

    gates = [
        {
            "number": int(gate.get("number", 0)),
            "name": str(gate.get("name", "UNKNOWN")),
            "status": str(gate.get("status", "NOT_PROVEN")),
        }
        for gate in eligibility.get("gates", ())
        if isinstance(gate, Mapping)
    ]
    failed_gates = [gate for gate in gates if gate["status"] != "PASS"]
    failed_numbers = {int(gate["number"]) for gate in failed_gates}
    actions: list[str] = []
    current_rate_status = _status_name(next_rate_coverage)
    live_rate_status = _status_name(next_live_rate_inputs)
    if live_rate_status != "PASS":
        actions.append(
            "Ensure each latest source option surface has a valid provider rate or "
            "predates a captured FRED receipt before the next Black-Scholes target."
        )
    if failed_numbers.intersection(range(2, 9)):
        actions.append(
            "Legacy v2 only (optional benchmark under v3): complete the guarded real "
            "OPRA definition and CBBO evidence phases, "
            "including exact underlying-bar and point-in-time rate coverage; fixtures "
            "and current-revised macro history are ineligible."
        )
    if 9 in failed_numbers:
        actions.append(
            "Keep Strategy in --pricing-mode active for production decisions and "
            "periodically publish strategy-evaluate research evidence after outcomes "
            "mature."
        )
    if 10 in failed_numbers:
        actions.append(
            "Keep Loop A, Pricing, and Options running in regular sessions until every "
            "pilot call/put route spans 20 distinct sessions; this causal evidence "
            "cannot be backfilled."
        )
    operational_status = _status_name(operational)
    if operational_status != "PASS":
        actions.append(
            "Run operational-preflight in the production Python environment and keep "
            "the passing artifact less than 24 hours old at promotion time."
        )
    candidate_status = _status_name(candidate)
    if candidate_status != "PASS":
        actions.append(
            "Freeze a candidate only after offline gates 1-8 pass; never freeze an "
            "incomplete or fixture-backed model."
        )
    closed_lockbox = eligibility.get("closed_lockbox")
    lockbox_status = _status_name(
        closed_lockbox if isinstance(closed_lockbox, Mapping) else None
    )
    if lockbox_status != "PASS":
        actions.append(
            "Keep the lockbox closed until all prerequisite gates, candidate identity, "
            "fresh operational evidence, and one-time operator authorization pass."
        )

    prospective = eligibility.get("prospective_summary")
    prospective = prospective if isinstance(prospective, Mapping) else {}
    latest_health_alerts = (
        health.get("alerts", ()) if isinstance(health, Mapping) else ()
    )
    return {
        "schema_version": "option-pricing-readiness-summary-v1",
        "datastore": str(Path(datastore_root).resolve()),
        "gate_status": str(
            eligibility.get("gate_status", "NOT_PRODUCTION_ELIGIBLE")
        ),
        "automated_action_allowed": False,
        "market": {
            "cycle_mode": market_decision.cycle_mode,
            "target_state": market_decision.target_state.value,
            "target_snapshot_for": (
                market_decision.target_snapshot_for.isoformat()
                if market_decision.target_snapshot_for is not None
                else None
            ),
            "next_eligible_pricing_cycle": market_decision.next_eligible_cycle(
                phase_offset_minutes=1
            ).isoformat(),
        },
        "gates": gates,
        "blocking_gates": failed_gates,
        "prospective": dict(prospective),
        "next_session_inputs": {
            "live_surface_rates": live_rate_status,
            "live_surface_rate_report": dict(next_live_rate_inputs or {}),
            "current_rate_receipt_for_future_surfaces": current_rate_status,
            "current_rate_receipt_report": dict(next_rate_coverage or {}),
        },
        "promotions": {
            "candidate": candidate_status,
            "closed_lockbox": lockbox_status,
            "operational_artifact": operational_status,
            "strategy_artifact": _status_name(strategy),
            "operational_status_in_current_eligibility_snapshot": _status_name(
                eligibility.get("operational_promotion")
                if isinstance(eligibility.get("operational_promotion"), Mapping)
                else None
            ),
        },
        "health": {
            "status": _status_name(health, default="NOT_EVALUATED"),
            "scope": (
                "LAST_ACTIONABLE_GENERATION"
                if market_decision.cycle_mode == "MONITOR_ONLY"
                else "CURRENT_ACTIONABLE_GENERATION"
            ),
            "checked_at": health.get("checked_at") if health else None,
            "alert_kinds": sorted(
                {
                    str(alert.get("kind", "UNKNOWN"))
                    for alert in latest_health_alerts
                    if isinstance(alert, Mapping)
                }
            ),
        },
        "recommended_next_actions": actions,
    }


def _loop_native_readiness_view(datastore_root: Path) -> dict[str, object]:
    """Render v3 alongside, without mutating or reinterpreting legacy evidence."""

    root = Path(datastore_root).resolve()
    policy_pointer = (
        root
        / "ml"
        / "option-pricing-loop-native-eligibility-policy-latest"
        / "run.json"
    )
    report_pointer = (
        root / "ml" / "option-pricing-loop-native-eligibility-latest" / "run.json"
    )
    try:
        policy = read_current_loop_native_eligibility_policy(root)
        policy_view: dict[str, object] = {
            "status": "PASS",
            "run_path": policy.receipt.get("run_path"),
            "published_at": policy.receipt.get("published_at"),
            "policy_hash_sha256": policy.policy_hash,
        }
    except Exception as exc:
        policy_view = {
            "status": "INVALID" if policy_pointer.exists() else "NOT_PUBLISHED",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    try:
        report = read_current_loop_native_eligibility_report(root)
        report_view: dict[str, object] = {
            "status": "PASS",
            "run_path": report.receipt.get("run_path"),
            "published_at": report.receipt.get("published_at"),
            "report_hash_sha256": report.policy_hash,
            "gate_status": report.payload.get("gate_status"),
            "gates": report.payload.get("gates"),
            "routes": report.payload.get("routes"),
            "capture_ready": report.payload.get("capture_ready") is True,
            "research_gate_eligible": (
                report.payload.get("research_gate_eligible") is True
            ),
            "production_authorized": (
                report.payload.get("production_authorized") is True
            ),
        }
    except Exception as exc:
        report_view = {
            "status": "INVALID" if report_pointer.exists() else "NOT_PUBLISHED",
            "reason": f"{type(exc).__name__}: {exc}",
            "capture_ready": False,
            "research_gate_eligible": False,
            "production_authorized": False,
        }
    return {
        "protocol_version": LOOP_NATIVE_ELIGIBILITY_PROTOCOL_VERSION,
        "policy": policy_view,
        "report": report_view,
        "historical_opra_required": True,
        "historical_opra_role": "CANONICAL_HISTORICAL_AND_PROSPECTIVE_MARKET_EVIDENCE",
        "required_symbol_side_routes": PRODUCTION_OPTION_ROUTE_COUNT,
        "automated_action_allowed": False,
        "recommended_next_actions": [
            "Deploy the verified Pricing and Options reader changes together only after "
            "operator-approved process replacement.",
            f"Collect at least 20 receipt-proven prospective OPRA sessions across all "
            f"{PRODUCTION_OPTION_ROUTE_COUNT} production routes; offline backfill rows "
            "never increment prospective counts. Retain Schwab as explicit fallback, "
            "enrichment, disagreement, and execution evidence.",
            "Do not freeze a candidate or open the lockbox until every prerequisite "
            "passes and explicit authorization is recorded.",
        ],
    }


def _optional_status(
    reader: Callable[[], object],
) -> Mapping[str, object] | None:
    try:
        value = reader()
    except Exception as exc:
        return {
            "status": "INVALID",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return value if isinstance(value, Mapping) else None


def _live_rate_input_report(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    target_snapshot_for: object,
    prediction_created_at: object,
    rate_observations: pd.DataFrame | None,
) -> dict[str, object]:
    """Verify the actual rate route on each latest pre-target option surface."""

    target = pd.Timestamp(pd.to_datetime(target_snapshot_for, utc=True))
    created = pd.Timestamp(pd.to_datetime(prediction_created_at, utc=True))
    observations = (
        rate_observations.copy()
        if rate_observations is not None
        else pd.DataFrame()
    )
    if not observations.empty:
        observations["available_at"] = pd.to_datetime(
            observations.get("available_at"), utc=True, errors="coerce"
        )
        observations["risk_free_rate"] = pd.to_numeric(
            observations.get("risk_free_rate"), errors="coerce"
        )
    routes: dict[str, dict[str, object]] = {}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        try:
            eligible = [
                snapshot
                for snapshot in committed_option_snapshots(
                    datastore_root, symbol=symbol
                )
                if snapshot.snapshot_for < target
                and (
                    snapshot.receipt_published_at or snapshot.available_at
                )
                < created
            ]
        except Exception as exc:
            routes[symbol] = {
                "status": "INVALID",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            continue
        if not eligible:
            routes[symbol] = {
                "status": "NOT_PROVEN",
                "reason": "No strictly earlier committed option surface exists.",
            }
            continue
        surface = max(
            eligible,
            key=lambda snapshot: (
                snapshot.snapshot_for,
                snapshot.receipt_published_at or snapshot.available_at,
            ),
        )
        available_at = surface.receipt_published_at or surface.available_at
        try:
            contracts = pd.read_parquet(surface.contracts_path)
        except Exception as exc:
            routes[symbol] = {
                "status": "INVALID",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            continue
        provider_source = (
            contracts["interest_rate"]
            if "interest_rate" in contracts
            else pd.Series(dtype=float)
        )
        provider = pd.to_numeric(provider_source, errors="coerce").dropna()
        provider = provider.loc[provider.between(-0.20, 1.0)]
        if not provider.empty:
            routes[symbol] = {
                "status": "PASS",
                "source": "OPTION_SURFACE_PROVIDER_RATE",
                "source_snapshot_for": surface.snapshot_for.isoformat(),
                "source_available_at": available_at.isoformat(),
                "risk_free_rate": float(provider.median()),
            }
            continue
        fallback = observations.loc[
            observations.get(
                "available_at",
                pd.Series(dtype="datetime64[ns, UTC]"),
            ).lt(available_at)
            & observations.get(
                "risk_free_rate", pd.Series(dtype=float)
            ).between(-0.20, 1.0)
        ].sort_values("available_at")
        if fallback.empty:
            routes[symbol] = {
                "status": "NOT_PROVEN",
                "reason": "Surface has no provider rate or strictly prior FRED receipt.",
                "source_snapshot_for": surface.snapshot_for.isoformat(),
                "source_available_at": available_at.isoformat(),
            }
        else:
            row = fallback.iloc[-1]
            routes[symbol] = {
                "status": "PASS",
                "source": "STRICTLY_PRIOR_FRED_RECEIPT",
                "source_snapshot_for": surface.snapshot_for.isoformat(),
                "source_available_at": available_at.isoformat(),
                "rate_available_at": pd.Timestamp(
                    row["available_at"]
                ).isoformat(),
                "risk_free_rate": float(row["risk_free_rate"]),
            }
    passing = {
        symbol: route
        for symbol, route in routes.items()
        if route["status"] == "PASS"
    }
    source_counts: dict[str, int] = {}
    for route in passing.values():
        source = str(route.get("source", "UNKNOWN"))
        source_counts[source] = source_counts.get(source, 0) + 1
    blocking = {
        symbol: route
        for symbol, route in routes.items()
        if route["status"] != "PASS"
    }
    return {
        "status": (
            "PASS"
            if routes and len(passing) == len(routes)
            else "NOT_PROVEN"
        ),
        "target_snapshot_for": target.isoformat(),
        "symbol_count": len(routes),
        "passing_symbol_count": len(passing),
        "source_counts": source_counts,
        "blocking_routes": blocking,
    }


def _status_name(
    value: Mapping[str, object] | None,
    *,
    default: str = "NOT_PROVEN",
) -> str:
    if not isinstance(value, Mapping):
        return default
    return str(value.get("status", default))


def _pricing_status(root: Path) -> Mapping[str, object]:
    publication = read_current_option_pricing_publication(root)
    return {
        "status": "VERIFIED",
        "run_directory": str(publication.run_directory),
        "run_timestamp": publication.manifest.get("run_timestamp"),
        "published_at": publication.receipt.get("published_at"),
        "publication_id": publication.receipt.get("publication_id"),
        "automated_action_allowed": False,
    }


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
