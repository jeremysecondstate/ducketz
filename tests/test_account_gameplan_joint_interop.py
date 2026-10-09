"""Offline interoperability with the unchanged canonical Scout composer."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from ml import joint_capital_plan as scout
from ml.account_gameplan.joint_bridge import owner_package, joint_snapshot
from ml.account_gameplan.planner import (account_forecast_id, project_account_plan,
                                        publish_account_plan, read_account_plan, sha, encoded)
from test_account_gameplan_planner import inputs, reseal_fixture


def test_canonical_composer_is_only_projection_and_source_hashes_are_native(inputs, monkeypatch):
    sources, state = inputs
    calls, ledger_calls = [], []
    compose, project = scout.compose_joint_plan, scout.project_direction_trades
    def tracked_project(*args, **kwargs):
        ledger_calls.append(True)
        return project(*args, **kwargs)
    def tracked_compose(*args, **kwargs):
        calls.append((deepcopy(args), kwargs))
        return compose(*args, **kwargs)
    monkeypatch.setattr(scout, "project_direction_trades", tracked_project)
    monkeypatch.setattr(scout, "compose_joint_plan", tracked_compose)
    rows, report, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
    assert len(calls) == len(ledger_calls) == 1
    assert calls[0][1]["maximum_snapshot_age_seconds"] == 60
    joint = report["joint_composition"]
    assert joint["ledger"] is report["direction_based_projection"]
    assert joint["account_snapshot_sha256"] == scout.content_sha256(joint_snapshot(state))
    assert joint["sole_executor_owner"] == "pc-original"
    for source in sources:
        owner = source.metadata["producer_id"]
        binding = joint["input_bindings"][owner]
        assert binding["source_revision_status"] == "UNRECORDED" and binding["source_revision"] is None
        assert binding["source_hashes"]["forecasts_sha256"] == sha((source.path / "forecasts.parquet").read_bytes())
        assert binding["source_hashes"]["price_path_sha256"] == sha((source.path / "planning-price-path.json").read_bytes())
    assert set(rows.id) == {row["id"] for row in joint["forecasts"]}
    assert all(row.id == account_forecast_id(row.producer_id, row.owner_run_id, row.original_forecast_id)
               for row in rows.itertuples())
    # The separately displayed opportunity capacity never enters Scout's ledger.
    assert all("projected_trade_quantity" not in row for package in calls[0][0][0] for row in package["forecasts"])


@pytest.mark.parametrize("field", ["forecasts", "prices", "metadata"])
def test_mutated_memory_cannot_claim_original_native_hashes(inputs, tmp_path, field):
    sources, state = inputs
    source = sources[0]
    if field == "forecasts":
        source.forecasts.loc[0, "calibrated_probability"] = .7
    elif field == "prices":
        source.price_path["points"][next(iter(source.price_path["points"]))]["reason"] = "changed"
    else:
        source.metadata["deployment_status"] = "changed"
    for operation in (lambda: project_account_plan(sources, state, observed_at=state["observed_at"]),
                      lambda: publish_account_plan(tmp_path / "no-publication", sources, state,
                          observed_at=state["observed_at"], clock=lambda: state["observed_at"])):
        with pytest.raises(ValueError, match="In-memory source"):
            operation()
    assert not (tmp_path / "no-publication").exists()


def test_legacy_exporter_holding_attestation_is_not_new_planning_authority(inputs):
    sources, _ = inputs
    sources[0].metadata["holding_policy_proof"] = "EXPORTER_ATTESTED_LEGACY"
    with pytest.raises(ValueError, match="NATIVE_MANIFEST"):
        owner_package(sources[0])


def test_saved_bearish_probability_abstention_survives_joint_projection(inputs):
    sources, state = inputs
    frame = sources[0].forecasts.copy()
    frame["calibrated_probability"] = .49
    frame["direction"] = "NO_EDGE"
    frame["model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    frame["symbol_fitted_target_rows"] = 0
    frame["symbol_route_fitted_target_rows"] = 0
    sources[0] = reseal_fixture(sources[0], forecasts=frame)
    rows, report, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
    retained = rows.loc[rows.producer_id.eq("pc-original")]
    assert retained.direction.eq("NO_EDGE").all() and retained.calibrated_probability.eq(.49).all()
    assert retained.loc[retained.execution_eligible, "direction_based_trade_quantity"].eq(0).all()
    assert not any(event["symbol"] == "AAPL" for event in report["direction_based_projection"]["events"])


@pytest.mark.parametrize("tamper", [None, "hourly", "ending_positions", "row_cash", "status"])
def test_unavailable_prices_keep_joint_ids_without_fabricating_joint_result(inputs, tmp_path, tamper):
    sources, state = inputs
    prices = deepcopy(sources[0].price_path)
    point = next(iter(prices["points"].values()))
    point.update(status="UNAVAILABLE_REFERENCE_PRICE", reference_price=None,
                 planned_price_low=None, planned_price_mid=None, planned_price_high=None)
    sources[0] = reseal_fixture(sources[0], prices=prices)
    plan = publish_account_plan(tmp_path / "unavailable", sources, state,
        observed_at=state["observed_at"], clock=lambda: state["observed_at"])
    assert plan.report["joint_composition"] is None
    assert plan.report["direction_projection_status"] == "UNAVAILABLE_PRICE_REFERENCES"
    assert plan.rows.projected_cash_after_low.isna().all()
    assert plan.ledger["events"] == [] and plan.ledger["summary"] == {}
    assert all(row.id == account_forecast_id(row.producer_id, "frozen", row.original_forecast_id)
               for row in plan.rows.itertuples())
    if tamper is not None:
        report = deepcopy(plan.report)
        if tamper == "row_cash":
            rows = plan.rows.copy()
            rows.loc[0, "projected_cash_after_low"] = 123
            rows.to_parquet(plan.path / "trade-plan.parquet", index=False)
        elif tamper == "status":
            report["direction_projection_status"] = "OTHER"
        else:
            report["direction_based_projection"][tamper] = {"invented": 123}
        (plan.path / "report.json").write_bytes(encoded(report))
        (plan.path / "direction-ledger.json").write_bytes(encoded(report["direction_based_projection"]))
        pin = repin_plan(plan.path)
        with pytest.raises(ValueError, match="Unavailable joint projection"):
            read_account_plan(plan.path, expected_manifest_sha256=pin)


def test_reader_rejects_rows_detached_from_valid_canonical_projection(inputs, tmp_path):
    sources, state = inputs
    plan = publish_account_plan(tmp_path / "bound", sources, state,
        observed_at=state["observed_at"], clock=lambda: state["observed_at"])
    rows = plan.rows.copy()
    rows.loc[0, "projected_cash_after_low"] += 1
    rows.to_parquet(plan.path / "trade-plan.parquet", index=False)
    pin = repin_plan(plan.path)
    with pytest.raises(ValueError, match="canonical joint composition"):
        read_account_plan(plan.path, expected_manifest_sha256=pin)


def repin_plan(path):
    manifest = json.loads((path / "manifest.json").read_bytes())
    for name in manifest["output_files"]:
        manifest["output_files"][name] = sha((path / name).read_bytes())
    raw = encoded(manifest)
    (path / "manifest.json").write_bytes(raw)
    receipt = json.loads((path / "receipt.json").read_bytes())
    receipt["manifest_sha256"] = sha(raw)
    (path / "receipt.json").write_bytes(encoded(receipt))
    return sha(raw)


@pytest.mark.parametrize("field,value", [("holding_policy", "other"), ("cross_horizon_fallback_policy", None)])
def test_wrapper_cannot_select_policy_different_from_canonical_ledger(inputs, tmp_path, field, value):
    sources, state = inputs
    plan = publish_account_plan(tmp_path / "policy", sources, state,
        observed_at=state["observed_at"], clock=lambda: state["observed_at"])
    report = deepcopy(plan.report)
    report[field] = value
    (plan.path / "report.json").write_bytes(encoded(report))
    with pytest.raises(ValueError, match="Canonical joint composition differs"):
        read_account_plan(plan.path, expected_manifest_sha256=repin_plan(plan.path))


@pytest.mark.parametrize("config,receipt", [("a" * 40, None), (None, "a" * 40),
                                          ("a" * 40, "b" * 40), ("a" * 40, "a" * 40)])
def test_revision_provenance_requires_matching_native_manifest_and_receipt(inputs, config, receipt):
    sources, _ = inputs
    source = reseal_fixture(sources[0], config_revision=config, receipt_revision=receipt)
    if config != receipt:
        with pytest.raises(ValueError, match="Recorded source revisions disagree"):
            owner_package(source)
    else:
        package = owner_package(source)
        assert package["source_revision"] == config and package["source_revision_status"] == "RECORDED"
        assert package["source_reference"]["reference_sha256"] == source.metadata["source_manifest_sha256"]
