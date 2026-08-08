from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml.artifacts import file_checksum, write_manifest
from ml.option_pricing.candidate import (
    freeze_candidate,
    read_candidate,
    verify_fresh_future_lockbox_targets,
)
from ml.option_pricing.eligibility import (
    ELIGIBILITY_REPORT_VERSION,
    EligibilityError,
    EligibilityPolicy,
    publish_eligibility_policy,
    publish_eligibility_report,
    read_eligibility_policy,
)
from ml.option_pricing.lineage import verify_completed_option_pricing_lineage
from ml.option_pricing.lockbox import (
    LOCKBOX_AUTHORIZATION_VERSION,
    LockboxError,
    score_closed_lockbox_once,
)
from ml.option_pricing import opra_materialization
from ml.option_pricing.operations import (
    ROLLBACK_AUTHORIZATION_VERSION,
    RuntimeLimits,
    build_runtime_health,
    dependency_contract_report,
    publish_operational_readiness,
    rollback_option_pricing_pointer,
)
from ml.option_pricing.publication import (
    OPTION_PRICING_PUBLICATION_VERSION,
    OPTION_PRICING_REPORT_NAME,
    OptionPricingPublicationError,
    pricing_pointer_path,
    publish_option_pricing_run,
    read_current_option_pricing_publication,
)
from ml.option_pricing.rates import rate_coverage_report
from ml.option_pricing.strategy_outcomes import (
    StrategyOutcomeError,
    compare_strategy_outcomes,
    publish_strategy_outcome_evidence,
    strategy_pair_values,
)
from ml.option_pricing_runtime import run_option_pricing_once
from ml.parquet_contracts import (
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    empty_frame,
    write_parquet_with_schema,
)


_OUTPUTS = {
    "pricing-samples.parquet": OPTION_PRICING_SAMPLE_SCHEMA,
    "pricing-predictions.parquet": OPTION_PRICING_PREDICTION_SCHEMA,
    "pricing-evaluations.parquet": OPTION_PRICING_EVALUATION_SCHEMA,
    "pricing-surfaces.parquet": OPTION_PRICING_SURFACE_SCHEMA,
    "pricing-monitoring.parquet": OPTION_PRICING_MONITORING_SCHEMA,
}


def test_lineage_is_derived_from_receipts_and_input_checksums(tmp_path: Path) -> None:
    result = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA", "GOOG", "MU"),
        run_timestamp="2026-07-06T14:01:00Z",
        runtime_clock=lambda: "2026-07-06T14:01:01Z",
    )
    publication = read_current_option_pricing_publication(tmp_path)
    reference = publication.manifest["configuration"]["eligibility_policy"]
    policy = read_eligibility_policy(
        tmp_path / reference["path"], datastore_root=tmp_path
    )
    verified = verify_completed_option_pricing_lineage(
        tmp_path,
        run_directory=result.run_directory,
        policy_artifact=policy,
    )
    assert verified["verified"] is True

    with (policy.directory / "policy.json").open("a", encoding="utf-8") as handle:
        handle.write(" ")
    failed = verify_completed_option_pricing_lineage(
        tmp_path,
        run_directory=result.run_directory,
        policy_artifact=policy,
    )
    assert failed["verified"] is False
    assert any("checksum" in reason for reason in failed["errors"])


def test_disk_exhaustion_fails_before_pricing_publication(tmp_path: Path) -> None:
    limits = RuntimeLimits(minimum_free_disk_bytes=10**30)
    with pytest.raises(RuntimeError, match="disk capacity"):
        run_option_pricing_once(
            tmp_path,
            symbols=("NVDA", "GOOG", "MU"),
            run_timestamp="2026-07-06T14:01:00Z",
            runtime_clock=lambda: "2026-07-06T14:01:01Z",
            runtime_limits=limits,
        )
    assert not pricing_pointer_path(tmp_path).exists()


def test_rate_coverage_requires_strictly_prior_observation_for_every_target() -> None:
    targets = (
        "2026-07-06T14:00:00Z",
        "2026-07-06T15:30:00Z",
    )
    missing = rate_coverage_report(None, target_snapshot_fors=targets)
    assert missing["status"] == "NOT_PROVEN"
    covered = rate_coverage_report(
        pd.DataFrame(
            {
                "available_at": ["2026-07-06T13:00:00Z"],
                "risk_free_rate": [0.05],
            }
        ),
        target_snapshot_fors=targets,
    )
    assert covered["status"] == "PASS"
    assert covered["covered_target_count"] == 2


def test_final_lineage_failure_restores_previous_verified_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA", "GOOG", "MU"),
        run_timestamp="2026-07-06T14:01:00Z",
        runtime_clock=lambda: "2026-07-06T14:01:01Z",
    )
    monkeypatch.setattr(
        "ml.option_pricing_runtime.verify_completed_option_pricing_lineage",
        lambda *_args, **_kwargs: {
            "verified": False,
            "errors": ["fixture final verification failure"],
        },
    )
    with pytest.raises(RuntimeError, match="authority was restored"):
        run_option_pricing_once(
            tmp_path,
            symbols=("NVDA", "GOOG", "MU"),
            run_timestamp="2026-07-06T14:16:00Z",
            runtime_clock=lambda: "2026-07-06T14:16:01Z",
        )
    assert (
        read_current_option_pricing_publication(tmp_path).run_directory
        == first.run_directory
    )


def test_forged_production_claim_cannot_be_published(tmp_path: Path) -> None:
    result = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA", "GOOG", "MU"),
        run_timestamp="2026-07-06T14:01:00Z",
        runtime_clock=lambda: "2026-07-06T14:01:01Z",
    )
    report = json.loads(
        (result.eligibility_report_directory / "eligibility-report.json").read_text(
            encoding="utf-8"
        )
    )
    report["gate_status"] = "PRODUCTION_ELIGIBLE"
    with pytest.raises(EligibilityError, match="independently verify|not reproduced"):
        publish_eligibility_report(
            tmp_path,
            report=report,
            pricing_run=result.run_directory,
            published_at="2026-07-06T14:01:02Z",
        )


def test_normal_materializer_inventories_but_never_decodes_lockbox_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "ml" / "option-pricing-evidence" / "opra"
    definitions_dir = evidence / "definitions"
    cbbo_dir = evidence / "cbbo"
    definitions_dir.mkdir(parents=True)
    cbbo_dir.mkdir()
    (definitions_dir / "receipt.json").write_text("{}", encoding="utf-8")
    (cbbo_dir / "receipt.json").write_text("{}", encoding="utf-8")
    requests = []
    outputs = {}
    for index in range(130):
        target = pd.Timestamp("2026-01-02T15:00:00Z") + pd.Timedelta(minutes=index)
        name = f"cbbo-NVDA-{index:03d}.dbn.zst"
        requests.append(
            {
                "output_name": name,
                "purpose": f"SOURCE_BACKWARD_TARGET_FORWARD:NVDA:{target.isoformat()}",
                "symbols": [
                    "NVDA  260220C00100000",
                    "NVDA  260220P00100000",
                ],
            }
        )
        outputs[name] = {"size": 1, "checksum_sha256": f"checksum-{index}"}

    def fake_import(directory: Path, *, datastore_root: Path) -> dict[str, object]:
        if directory.name == "definitions":
            return {
                "manifest": {
                    "phase": "definitions",
                    "outputs": {"definitions.dbn.zst": {}},
                }
            }
        return {
            "manifest": {
                "phase": "cbbo",
                "eligibility_policy": {"policy_hash": "policy-hash"},
                "requests": requests,
                "outputs": outputs,
            }
        }

    decoded: list[str] = []

    def fake_read(path: Path) -> pd.DataFrame:
        decoded.append(path.name)
        if path.name.startswith("cbbo-"):
            raise ValueError("stop after proving decode boundary")
        return pd.DataFrame()

    monkeypatch.setattr(opra_materialization, "read_opra_import", fake_import)
    monkeypatch.setattr(opra_materialization, "_read_dbn", fake_read)
    monkeypatch.setattr(
        opra_materialization,
        "normalize_definition_records",
        lambda frame: pd.DataFrame({"symbol": ["NVDA"]}),
    )
    result = opra_materialization.materialize_committed_opra_history_v2(
        tmp_path,
        symbols=("NVDA",),
        rate_observations=None,
        closed_lockbox_clusters=126,
        eligibility_policy_hash="policy-hash",
    )
    assert result.closed_lockbox.cluster_count == 126
    assert result.closed_lockbox.target_values_read is False
    assert result.closed_lockbox.output_count == 126
    decoded_cbbo = [name for name in decoded if name.startswith("cbbo-")]
    assert decoded_cbbo == [f"cbbo-NVDA-{index:03d}.dbn.zst" for index in range(4)]


def test_corrupt_receipt_fails_and_authorized_rollback_preserves_evidence(
    tmp_path: Path,
) -> None:
    first = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA", "GOOG", "MU"),
        run_timestamp="2026-07-06T14:01:00Z",
        runtime_clock=lambda: "2026-07-06T14:01:01Z",
    )
    second = run_option_pricing_once(
        tmp_path,
        symbols=("NVDA", "GOOG", "MU"),
        run_timestamp="2026-07-06T14:16:00Z",
        runtime_clock=lambda: "2026-07-06T14:16:01Z",
    )
    current = read_current_option_pricing_publication(tmp_path)
    target = current.receipt["previous_publication"]["run_path"]
    authorization = tmp_path / "rollback-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schema_version": ROLLBACK_AUTHORIZATION_VERSION,
                "action": "RESTORE_PREVIOUS_VERIFIED_OPTION_PRICING_POINTER",
                "authorization_id": "rollback-test-1",
                "operator_id": "test-operator",
                "approved_at": "2026-07-06T14:17:00Z",
                "current_run": second.run_directory.relative_to(tmp_path).as_posix(),
                "target_run": target,
            }
        ),
        encoding="utf-8",
    )
    receipt = rollback_option_pricing_pointer(
        tmp_path,
        authorization_path=authorization,
        restored_at="2026-07-06T14:17:01Z",
    )
    assert read_current_option_pricing_publication(tmp_path).run_directory == first.run_directory
    assert second.run_directory.is_dir()
    assert receipt["evidence_deleted"] is False

    with (first.run_directory / "publication.json").open("a", encoding="utf-8") as handle:
        handle.write("tamper")
    with pytest.raises(OptionPricingPublicationError):
        read_current_option_pricing_publication(tmp_path)


def test_health_reports_stale_pointer_and_evidence_stagnation() -> None:
    routes = {
        f"{symbol}/{call_put}": {"partition": {"status": "PASS"}}
        for symbol in ("NVDA", "GOOG", "MU")
        for call_put in ("call", "put")
    }
    health = build_runtime_health(
        pricing_run=Path("C:/test/run"),
        eligibility_report={
            "generated_at": "2026-08-01T00:00:00Z",
            "gate_status": "COLLECTING_PROSPECTIVE_EVIDENCE",
            "routes": routes,
            "gates": [],
        },
        lineage_report={"verified": True},
        route_errors={},
        live_routes={
            symbol: {"status": "TARGET_BAR_NOT_READY"}
            for symbol in ("NVDA", "GOOG", "MU")
        },
        elapsed_seconds=1.0,
        peak_memory_bytes=1_000,
        capacity={"status": "PASS"},
        checked_at="2026-08-07T00:00:00Z",
        previous_prospective_count=0,
        previous_prospective_checked_at="2026-08-01T00:00:00Z",
    )
    kinds = {alert["kind"] for alert in health["alerts"]}
    assert {"STALE_POINTER", "EVIDENCE_COUNT_STAGNATION"}.issubset(kinds)
    assert health["status"] == "FAIL"
    assert health["actionable_exit_code"] == 6


def test_degraded_health_is_actionable() -> None:
    routes = {
        f"{symbol}/{call_put}": {"partition": {"status": "PASS"}}
        for symbol in ("NVDA", "GOOG", "MU")
        for call_put in ("call", "put")
    }
    health = build_runtime_health(
        pricing_run=Path("C:/test/run"),
        eligibility_report={
            "generated_at": "2026-08-07T00:00:00Z",
            "gate_status": "COLLECTING_PROSPECTIVE_EVIDENCE",
            "routes": routes,
            "gates": [],
        },
        lineage_report={"verified": True},
        route_errors={"NVDA": "target quote not yet available"},
        live_routes={symbol: {"status": "READY"} for symbol in ("NVDA", "GOOG", "MU")},
        elapsed_seconds=1.0,
        peak_memory_bytes=1_000,
        capacity={"status": "PASS"},
        checked_at="2026-08-07T00:00:00Z",
    )
    assert health["status"] == "DEGRADED"
    assert health["actionable_exit_code"] == 6


def test_forged_operational_pass_is_reverified(tmp_path: Path) -> None:
    forged = {
        "schema_version": "option-pricing-operational-readiness-v1",
        "status": "PASS",
        "runtime_limits": {
            key: value for key, value in RuntimeLimits().__dict__.items()
        },
        "configuration": {
            "configured_symbols": ["NVDA", "GOOG", "MU"],
        },
        "automated_action_allowed": False,
    }
    published = publish_operational_readiness(tmp_path, report=forged)
    assert published["status"] == "NOT_PROVEN"
    assert published["reverified_before_publication"] is True


def test_strategy_pair_is_exact_fee_aware_and_session_blocked() -> None:
    candidate = {
        "legs_json": json.dumps(
            [
                {
                    "asset": "OPTION",
                    "quantity": 2,
                    "option_type": "CALL",
                    "bid": 1.0,
                    "ask": 1.2,
                    "multiplier": 100.0,
                },
                {
                    "asset": "STOCK",
                    "quantity": 100,
                    "bid": 100.0,
                    "ask": 100.0,
                    "multiplier": 1.0,
                },
            ]
        ),
        "entry_fees": 1.30,
        "expected_net_profit": 0.0,
        "pricing_candidate_edge": 10.0,
        "pricing_edge_to_friction": 10.0 / 41.3,
        "pricing_uncertainty": 5.0,
    }
    pair = strategy_pair_values(
        candidate,
        realized_net_profit_usd=7.4,
        per_contract_fee_usd=0.65,
    )
    assert pair["round_trip_contract_fees_usd"] == pytest.approx(2.6)
    assert pair["bsgp_expected_net_profit_usd"] == pytest.approx(7.4)
    assert pair["paired_improvement_usd"] == pytest.approx(7.4)
    assert pair["uncertainty_contains_outcome"] is True

    observations = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "call_put_routes": call_put,
                "decision_timestamp": pd.Timestamp("2026-01-05T15:00:00Z")
                + pd.Timedelta(days=index // 3, minutes=index % 3),
                "paired_improvement_usd": 5.0,
                "uncertainty_contains_outcome": index < 57,
                "round_trip_contract_fees_usd": 1.30,
                "entry_bid_ask_spread_usd": 10.0,
                "exit_bid_ask_spread_usd": 12.0,
                "total_bid_ask_spread_usd": 22.0,
                "exact_candidate_cohort": True,
            }
            for symbol in ("NVDA", "GOOG", "MU")
            for call_put in ("CALL", "PUT")
            for index in range(60)
        ]
    )
    report = compare_strategy_outcomes(
        observations,
        policy=EligibilityPolicy(),
        exclusions={"missing_exit_contract": 4},
        verified_strategy_run_count=20,
        evaluated_at="2026-08-07T00:00:00Z",
    )
    assert report["status"] == "PASS"
    assert report["paired_candidate_count"] == 360
    assert report["distinct_sessions"] == 20
    assert report["uncertainty_coverage"] == pytest.approx(0.95)
    assert report["lower_confidence_bound_usd"] > 0.0
    assert set(report["routes"]) == {
        "NVDA/call",
        "NVDA/put",
        "GOOG/call",
        "GOOG/put",
        "MU/call",
        "MU/put",
    }
    assert all(route["status"] == "PASS" for route in report["routes"].values())
    assert report["rankings_changed"] is False
    assert report["order_construction_changed"] is False
    assert report["automated_action_allowed"] is False


def test_fabricated_strategy_pass_cannot_be_published(tmp_path: Path) -> None:
    source = tmp_path / "fixture-source.json"
    source.write_text("{}\n", encoding="utf-8")
    observations = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "call_put_routes": "CALL",
                "decision_timestamp": pd.Timestamp("2026-01-05T15:00:00Z"),
                "paired_improvement_usd": 10.0,
                "uncertainty_contains_outcome": True,
                "round_trip_contract_fees_usd": 1.30,
                "entry_bid_ask_spread_usd": 1.0,
                "exit_bid_ask_spread_usd": 1.0,
                "total_bid_ask_spread_usd": 2.0,
                "exact_candidate_cohort": True,
            }
        ]
    )
    forged = {
        "schema_version": "option-pricing-strategy-outcome-evidence-v1",
        "status": "PASS",
        "evidence_kind": "REAL_RECEIPT_PROVEN",
        "evaluated_at": "2026-08-07T00:00:00Z",
        "verified_strategy_run_count": 1,
        "rankings_changed": False,
        "order_construction_changed": False,
        "order_payloads_created": False,
        "automated_action_allowed": False,
    }
    with pytest.raises(StrategyOutcomeError, match="not reproduced"):
        publish_strategy_outcome_evidence(
            tmp_path,
            observations=observations,
            report=forged,
            source_files=(source,),
        )


def test_candidate_freeze_and_failed_one_time_lockbox_are_irreversible(
    tmp_path: Path,
) -> None:
    policy = publish_eligibility_policy(
        tmp_path,
        published_at="2026-07-01T00:00:00Z",
    )
    run = _prepared_candidate_run(tmp_path, policy)
    publication = publish_option_pricing_run(
        tmp_path,
        run_directory=run,
        published_at="2026-07-01T00:00:01Z",
    )
    gates = [
        {"number": number, "name": f"gate-{number}", "status": "PASS", "evidence": {}}
        for number in range(1, 11)
    ]
    lineage = verify_completed_option_pricing_lineage(
        tmp_path,
        run_directory=run,
        policy_artifact=policy,
    )
    assert lineage["verified"] is True
    gates[0]["evidence"] = {"lineage": lineage}
    offline_report = {
        "schema_version": ELIGIBILITY_REPORT_VERSION,
        "gate_status": "NOT_PRODUCTION_ELIGIBLE",
        "eligibility_policy": {
            "policy_hash": policy.policy_hash,
            "path": str(policy.directory),
        },
        "automated_action_allowed": False,
        "gates": gates,
        "closed_lockbox_inventory": _lockbox_inventory(),
    }
    publish_eligibility_report(
        tmp_path,
        report=offline_report,
        pricing_run=run,
        published_at="2026-07-01T00:00:01.500000Z",
    )
    candidate = freeze_candidate(
        tmp_path,
        pricing_run=publication.run_directory,
        policy_artifact=policy,
        eligibility_report=offline_report,
        frozen_at="2026-07-01T00:00:02Z",
    )
    assert candidate["status"] == "FROZEN"
    assert candidate["retraining_allowed"] is False
    assert candidate["source_evidence_kind"] == "FIXTURE_TEST_ONLY"
    assert candidate["production_evidence_eligible"] is False

    prelockbox_report = {
        "schema_version": ELIGIBILITY_REPORT_VERSION,
        "gate_status": "NOT_PRODUCTION_ELIGIBLE",
        "automated_action_allowed": False,
        "eligibility_policy": offline_report["eligibility_policy"],
        "gates": gates,
        "frozen_candidate": {"status": "PASS", "evidence": candidate},
        "closed_lockbox": {"status": "NOT_PROVEN"},
        "operational_promotion": {"status": "PASS"},
    }
    publish_eligibility_report(
        tmp_path,
        report=prelockbox_report,
        pricing_run=run,
        published_at="2026-07-01T00:00:03Z",
    )
    authorization = tmp_path / "lockbox-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schema_version": LOCKBOX_AUTHORIZATION_VERSION,
                "action": "OPEN_AND_SCORE_OPTION_PRICING_LOCKBOX_ONCE",
                "authorization_id": "lockbox-test-1",
                "operator_id": "test-operator",
                "approved_at": "2026-07-01T00:00:04Z",
                "candidate_id": candidate["candidate_id"],
                "eligibility_policy_hash": policy.policy_hash,
                "maximum_score_attempts": 1,
                "automated_action_allowed": False,
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def materializer() -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return pd.DataFrame()

    with pytest.raises(LockboxError, match="permanently invalidated"):
        score_closed_lockbox_once(
            tmp_path,
            candidate_id=str(candidate["candidate_id"]),
            policy_artifact=policy,
            authorization_path=authorization,
            scored_at="2026-07-01T00:00:05Z",
            materializer=materializer,
        )
    assert calls == 1
    invalidated = read_candidate(
        tmp_path / "ml" / "option-pricing-candidates" / str(candidate["candidate_id"]),
        datastore_root=tmp_path,
    )
    assert invalidated["permanently_invalidated"] is True
    reused = verify_fresh_future_lockbox_targets(
        tmp_path,
        target_snapshot_fors=_lockbox_inventory()["target_snapshot_fors"],
    )
    assert reused["status"] == "NOT_PROVEN"
    future_targets = [
        (pd.Timestamp(value) + pd.Timedelta(days=365)).isoformat()
        for value in _lockbox_inventory()["target_snapshot_fors"]
    ]
    assert (
        verify_fresh_future_lockbox_targets(
            tmp_path,
            target_snapshot_fors=future_targets,
        )["status"]
        == "PASS"
    )
    with pytest.raises(LockboxError, match="permanently invalidated"):
        score_closed_lockbox_once(
            tmp_path,
            candidate_id=str(candidate["candidate_id"]),
            policy_artifact=policy,
            authorization_path=authorization,
            materializer=materializer,
        )
    assert calls == 1


def test_declared_runtime_environment_matches_exact_lock() -> None:
    report = dependency_contract_report()
    assert report["status"] == "PASS"
    assert report["databento_declared_directly"] is True
    assert all(value["matches"] for value in report["packages"].values())


def _prepared_candidate_run(tmp_path: Path, policy: object) -> Path:
    run = tmp_path / "ml" / "option-pricing-runs" / "20260701T000000.000000Z"
    run.mkdir(parents=True)
    for name, schema in _OUTPUTS.items():
        write_parquet_with_schema(empty_frame(schema), run / name, schema)
    report = {
        "closed_lockbox_inventory": _lockbox_inventory(),
        "automated_action_allowed": False,
    }
    (run / OPTION_PRICING_REPORT_NAME).write_text(
        json.dumps(report) + "\n", encoding="utf-8"
    )
    model_files: list[str] = []
    for symbol in ("NVDA", "GOOG", "MU"):
        for call_put in ("call", "put"):
            path = run / "model-artifacts" / symbol / call_put / "v1" / "artifact.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"frozen-model")
            model_files.append(path.relative_to(run).as_posix())
    write_manifest(
        run,
        run_timestamp="2026-07-01T00:00:00Z",
        input_files=(policy.directory / "policy.json", policy.directory / "receipt.json"),
        output_files=(*_OUTPUTS, OPTION_PRICING_REPORT_NAME, *model_files),
        configuration={
            "eligibility_policy": {
                "policy_hash": policy.policy_hash,
                "path": policy.directory.relative_to(tmp_path).as_posix(),
                "receipt_checksum_sha256": file_checksum(
                    policy.directory / "receipt.json"
                ),
            },
            "publication_contract": {
                "version": OPTION_PRICING_PUBLICATION_VERSION,
                "authority": "ml/option-pricing-latest/run.json",
                "schema_validation": True,
                "automated_action_allowed": False,
            },
            "partition_config": policy.policy["model_contract"]["partitions"],
            "model_policy": policy.policy["model_contract"]["bsgp"],
            "contract_policy": policy.policy["model_contract"]["contract_selection"],
            "projection_policy": policy.policy["model_contract"]["projection"],
        },
        datastore_root=tmp_path,
    )
    return run


def _lockbox_inventory() -> dict[str, object]:
    targets = [
        (
            pd.Timestamp("2026-01-02T15:00:00Z") + pd.Timedelta(minutes=index)
        ).isoformat()
        for index in range(126)
    ]
    return {
        "status": "CLOSED_UNTOUCHED_UNSCORED",
        "target_values_read": False,
        "target_snapshot_fors": targets,
        "outputs": [],
    }
