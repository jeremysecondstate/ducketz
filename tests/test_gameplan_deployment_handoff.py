"""Exercise the handoff with real immutable files and mocked native model readers.

Publication/model correctness is tested by their native suites. These fixtures
avoid model fitting while preserving real manifests, hashes, pointers and clocks.
"""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum, verify_manifest, write_manifest
from ml import gameplan_deployment as deployment
from ml.independent_stock_targets import stock_target_windows
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET


DAY = "2026-09-18"
DEADLINE = "2026-09-18T11:00:00+00:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    state = {"root": root}
    tick = iter(pd.date_range("2026-09-18T08:00Z", periods=100, freq="s"))
    monkeypatch.setattr(deployment, "_before_open", lambda day: next(tick))

    def publication(name, raw=False):
        run = root / "ml/nightly-gameplan-runs" / name
        run.mkdir(parents=True)
        rows = pd.DataFrame(stock_target_windows(date.fromisoformat(DAY))).assign(
            symbol="AAPL", id=lambda f: "AAPL:" + f.route, calibrated_probability=.6 if raw else .4,
            direction="BULLISH" if raw else "BEARISH", model_status="PROMOTED")
        rows.to_parquet(run / "forecasts.parquet", index=False)
        config = {"symbols": ["AAPL"], "action_date": DAY,
                  "target_contract_version": "independent-stock-targets-v1",
                  "target_price_source_contract": "xnas-itch-archive-v1"}
        if raw:
            config.update(probability_target_contract=RAW_DIRECTION_TARGET, gameplan_variant="YG")
        write_manifest(run, run_timestamp="2026-09-18T07:00Z", input_files=(),
                       output_files=("forecasts.parquet",), configuration=config)
        write(run / "receipt.json", {"action_date": DAY, "orders_placed": 0,
              "broker_orders_enabled": False, "run_path": run.relative_to(root).as_posix()})
        return SimpleNamespace(run_directory=run, manifest=verify_manifest(run),
                               receipt=json.loads((run / "receipt.json").read_text()))

    state["og"], state["yg"] = publication("og"), publication("yg", True)
    state['publication'] = publication
    state["current"] = state["og"]
    gameplan_pointer = root / "ml/nightly-gameplan-latest/run.json"
    state["gameplan_pointer"] = gameplan_pointer
    write(gameplan_pointer, {"current": {"run_path": state["og"].run_directory.relative_to(root).as_posix()}})

    def current(_):
        return state["current"]

    def saved(_, run):
        item = state["og"] if Path(run) == state["og"].run_directory else state["yg"]
        verify_manifest(item.run_directory)
        return item

    monkeypatch.setattr("ml.nightly_gameplan.read_current_gameplan", current)
    monkeypatch.setattr("ml.nightly_gameplan.read_gameplan_run", saved)

    def trade(name, source):
        run = root / "ml/gameplan-trade-plan-runs" / name
        run.mkdir(parents=True)
        (run / "Gameplan.md").write_text("immutable fixture\n")
        write_manifest(run, run_timestamp="2026-09-18T07:00Z", input_files=(), output_files=("Gameplan.md",))
        write(run / "receipt.json", {"status": "COMPLETE", "action_date": DAY,
              "source_gameplan_run": source.run_directory.relative_to(root).as_posix(),
              "source_receipt_sha256": file_checksum(source.run_directory / "receipt.json"),
              "manifest_sha256": file_checksum(run / "manifest.json"), "forecast_rows": 24,
              "orders_placed": 0, "broker_orders_enabled": False})
        write(root / "ml/gameplan-trade-plan-latest/run.json", {"current": {
              "run_path": run.relative_to(root).as_posix(), "receipt_sha256": file_checksum(run / "receipt.json")}})
        return run

    state["trade"] = trade
    state["og_trade"] = trade("og", state["og"])
    state["og_bytes"] = (state["og"].run_directory / "receipt.json").read_bytes()
    deployment.prepare_deployment(root, action_date=DAY)
    state["current"] = state["yg"]
    write(gameplan_pointer, {"current": {"run_path": state["yg"].run_directory.relative_to(root).as_posix()}})
    state["yg_trade"] = trade("yg", state["yg"])
    enrichment = root / "ml/stock-trader-model-runs/yg-enrichment"
    source_path = state["yg"].run_directory.relative_to(root).as_posix()
    source = {"source_gameplan_run": source_path, "source_files": {
        source_path + "/receipt.json": file_checksum(state["yg"].run_directory / "receipt.json")}}
    write(enrichment / "model.json", {"source_publication": source})
    write(enrichment / "training-report.json", {**source, "orders_placed": 0, "broker_orders_enabled": False})
    write_manifest(enrichment, run_timestamp="2026-09-18T07:12Z", input_files=(),
                   output_files=("model.json", "training-report.json"))
    write(enrichment / "receipt.json", {"schema_version": "stock-trader-enrichment-training-receipt-v1",
        "run_path": enrichment.relative_to(root).as_posix(), "manifest_sha256": file_checksum(enrichment / "manifest.json"),
        "model_sha256": file_checksum(enrichment / "model.json"),
        "training_report_sha256": file_checksum(enrichment / "training-report.json")})
    write(root / "ml/stock-trader-model-latest/run.json", {"run_path": enrichment.relative_to(root).as_posix(),
        "receipt_sha256": file_checksum(enrichment / "receipt.json")})
    # Fitted payload semantics have a dedicated native suite; the handoff still
    # verifies these real source files, manifests, receipts and log bindings.
    monkeypatch.setattr("ml.stock_trader.model.load_current_enrichment_model", lambda root: None)
    state["enrichment"] = enrichment
    native = root / "ml/overnight-runs/yg-native"
    native.mkdir(parents=True)
    stages = ["gameplan_publication", "stock_enrichment_training", "gameplan_trade_planning"]
    report = {"schema_version": "supervised-overnight-gameplan-runtime-v2",
              "status": "COMPLETE", "action_date": DAY, "deadline_at": DEADLINE,
              "stock_only": True, "independent_stock_horizons": True,
              "stock_price_source": "xnas-itch-archive-v1", "probability_target_contract": RAW_DIRECTION_TARGET,
              "gameplan_variant": "YG", "resumed_from": None,
              "completed_stages_from_previous_attempt": [],
              "orders_placed": 0, "broker_orders_enabled": False,
              "enrichment_gameplan": {"run_path": state["yg"].run_directory.relative_to(root).as_posix(), "action_date": DAY,
                  "receipt_sha256": file_checksum(state["yg"].run_directory / "receipt.json")},
              "stages": [{"stage": stage, "status": "COMPLETE", "exit_code": 0,
                          "started_at": f"2026-09-18T07:{index*10:02d}:00+00:00",
                          "finished_at": f"2026-09-18T07:{index*10+5:02d}:00+00:00",
                          "log_path": stage + ".log"} for index, stage in enumerate(stages)]}
    logs = {}
    for stage in stages:
        log = native / (stage + ".log")
        log.write_text(json.dumps({"orders_placed": 0, **({"status": "PUBLISHED", "run_path": str(enrichment)}
                       if stage == "stock_enrichment_training" else {})}) + "\n")
        logs[log.name] = {"checksum_sha256": file_checksum(log), "size": log.stat().st_size}
    state["native"], state["report"] = native, report

    def bind_native():
        write(native / "stage-report.json", report)
        write(native / "receipt.json", {"schema_version": "supervised-overnight-gameplan-runtime-v2",
              "status": report["status"], "orders_placed": 0,
              "broker_orders_enabled": False, "run_path": native.relative_to(root).as_posix(),
              "stage_report_checksum_sha256": file_checksum(native / "stage-report.json"),
              "stage_report_size": (native / "stage-report.json").stat().st_size, "logs": logs})

    state["bind_native"] = bind_native
    bind_native()
    return state


def activate(state):
    return deployment.activate_deployment(state["root"], action_date=DAY, overnight_run=state["native"])


def test_prepare_and_activate_bind_complete_native_tail_and_preserve_frozen_og(handoff):
    root = handoff["root"]
    assert deployment.read_deployment(root, DAY)["status"] == "PREPARING"
    run = activate(handoff)
    saved = deployment.read_deployment(root, DAY)
    assert saved["status"] == "ACTIVE" and saved["active_variant"] == "YG"
    assert saved["OG_execution_allowed"] is False
    assert pd.Timestamp(saved["original_deadline"]) == pd.Timestamp(DEADLINE)
    assert saved["orders_placed"] == 0 and saved["broker_orders_enabled"] is False
    assert (handoff["og"].run_directory / "receipt.json").read_bytes() == handoff["og_bytes"]
    pairs = pd.read_parquet(run / "forecast-comparison.parquet")
    assert len(pairs) == 24 and pairs.direction_og.ne(pairs.direction_yg).all()
    assert "not accuracy improvements" in (run / "Comparison.md").read_text()
    deployment.assert_execution_gameplan(root, handoff["yg"])
    with pytest.raises(ValueError, match="NOT_EXECUTION_SOURCE"):
        deployment.assert_execution_gameplan(root, handoff["og"])


@pytest.mark.parametrize("stage", ["gameplan_publication", "stock_enrichment_training", "gameplan_trade_planning"])
def test_activation_requires_each_native_tail_stage_complete(handoff, stage):
    for item in handoff["report"]["stages"]:
        if item["stage"] == stage:
            item["status"] = "FAILED"
    handoff["bind_native"]()
    with pytest.raises(ValueError, match="stage incomplete"):
        activate(handoff)
    assert deployment.read_deployment(handoff["root"], DAY)["status"] == "PREPARING"


@pytest.mark.parametrize("change", ["deadline", "native_orders", "source", "window", "universe", "date", "trade", "og_tamper", "log_tamper"])
def test_activation_rejects_mismatched_or_changed_frozen_evidence(handoff, change):
    if change == "deadline":
        handoff["report"]["deadline_at"] = "2026-09-18T12:00Z"
    elif change == "native_orders":
        handoff["report"]["orders_placed"] = 1
    elif change == "source":
        handoff["report"]["enrichment_gameplan"]["receipt_sha256"] = "wrong"
    elif change == "window":
        path = handoff["yg"].run_directory / "forecasts.parquet"
        frame = pd.read_parquet(path)
        frame.loc[0, "target_window_end"] += pd.Timedelta(minutes=1)
        frame.to_parquet(path, index=False)
    elif change == "universe":
        handoff["yg"].manifest["configuration"]["symbols"] = ["AAPL", "COST"]
    elif change == "date":
        handoff["yg"].receipt["action_date"] = "2026-09-21"
    elif change == "trade":
        handoff["trade"]("wrong", handoff["og"])
    elif change == "og_tamper":
        path = handoff["og"].run_directory / "receipt.json"
        path.write_text(path.read_text() + " ")
    else:
        (handoff["native"] / "stock_enrichment_training.log").write_text("tampered")
    handoff["bind_native"]()
    with pytest.raises((ValueError, RuntimeError)):
        activate(handoff)


def test_independently_advanced_deployment_pointer_is_not_overwritten(handoff, monkeypatch):
    pointer = deployment._pointer(handoff["root"], DAY)
    original = deployment._publish
    advanced = {"schema_version": "independently-advanced", "current": {}}

    def race(root, report, **kwargs):
        write(pointer, advanced)
        return original(root, report, **kwargs)

    monkeypatch.setattr(deployment, "_publish", race)
    with pytest.raises(ValueError, match="advanced independently"):
        activate(handoff)
    assert json.loads(pointer.read_text()) == advanced


@pytest.mark.parametrize("name", ["ml/nightly-gameplan-latest/run.json", "ml/gameplan-trade-plan-latest/run.json",
                                  "ml/stock-trader-model-latest/run.json", "ml/overnight-runs/yg-native/receipt.json"])
def test_independently_advanced_preparation_reference_blocks_handoff(handoff, monkeypatch, name):
    path = handoff["root"] / name
    original = deployment._publish
    before = deployment._pointer(handoff["root"], DAY).read_bytes()

    def race(root, report, **kwargs):
        path.write_text(path.read_text() + " ")
        return original(root, report, **kwargs)

    monkeypatch.setattr(deployment, "_publish", race)
    with pytest.raises(ValueError, match="source advanced independently"):
        activate(handoff)
    assert deployment._pointer(handoff["root"], DAY).read_bytes() == before


@pytest.mark.parametrize("change", ["native_target", "native_price_source", "report_size", "log_size",
                                    "enrichment_receipt", "enrichment_model", "trade_orders"])
def test_activation_requires_native_target_and_complete_enrichment_integrity(handoff, change):
    if change == "native_target":
        handoff["report"]["probability_target_contract"] = "cost-adjusted-positive-return-v1"
        handoff["bind_native"]()
    elif change == "native_price_source":
        handoff["report"]["stock_price_source"] = "canonical-equity-minute-v1"
        handoff["bind_native"]()
    elif change in {"report_size", "log_size"}:
        path = handoff["native"] / "receipt.json"
        receipt = json.loads(path.read_text())
        if change == "report_size":
            receipt["stage_report_size"] += 1
        else:
            receipt["logs"]["stock_enrichment_training.log"]["size"] += 1
        write(path, receipt)
    elif change.startswith("enrichment_"):
        path = handoff["enrichment"] / ("receipt.json" if change == "enrichment_receipt" else "model.json")
        path.write_text(path.read_text() + " ")
    else:
        path = handoff["yg_trade"] / "receipt.json"
        receipt = json.loads(path.read_text())
        receipt["orders_placed"] = 1
        write(path, receipt)
        write(handoff["root"] / "ml/gameplan-trade-plan-latest/run.json", {"current": {
            "run_path": handoff["yg_trade"].relative_to(handoff["root"]).as_posix(),
            "receipt_sha256": file_checksum(path)}})
    with pytest.raises((ValueError, RuntimeError)):
        activate(handoff)


def resumed_tail(state):
    import copy
    original = state["native"]
    report = state["report"]
    report["status"] = "FAILED"
    report["stages"][-1]["status"] = "FAILED"
    state["bind_native"]()
    child = state["root"] / "ml/overnight-runs/yg-resume"
    child.mkdir()
    child_report = copy.deepcopy(report)
    child_report.update(status="COMPLETE", resumed_from=str(original),
        completed_stages_from_previous_attempt=["gameplan_publication", "stock_enrichment_training"])
    child_report["stages"] = [{**report["stages"][-1], "status": "COMPLETE",
                              "started_at": "2026-09-18T07:26:00+00:00", "finished_at": "2026-09-18T07:30:00+00:00"}]
    log = child / "gameplan_trade_planning.log"
    log.write_text('{"orders_placed": 0}\n')
    write(child / "stage-report.json", child_report)
    write(child / "receipt.json", {"schema_version": "supervised-overnight-gameplan-runtime-v2",
        "status": "COMPLETE", "orders_placed": 0, "broker_orders_enabled": False,
        "run_path": child.relative_to(state["root"]).as_posix(),
        "stage_report_checksum_sha256": file_checksum(child / "stage-report.json"),
        "stage_report_size": (child / "stage-report.json").stat().st_size,
        "logs": {log.name: {"checksum_sha256": file_checksum(log), "size": log.stat().st_size}}})
    state["native"] = child
    return original


def test_verified_failed_tail_resume_can_activate_without_repeating_successful_stages(handoff):
    original = resumed_tail(handoff)
    run = activate(handoff)
    report = deployment.read_deployment(handoff["root"], DAY)
    assert report["status"] == "ACTIVE"
    assert report["native_preparation"]["run_path"].endswith("yg-resume")
    bound = {str(item["path"]).replace("\\", "/") for item in verify_manifest(run)["input_files"]}
    assert (original / "receipt.json").relative_to(handoff["root"]).as_posix() in bound
    assert (handoff["og"].run_directory / "receipt.json").read_bytes() == handoff["og_bytes"]


def test_resume_cannot_use_name_only_claim_after_ancestor_changed(handoff):
    original = resumed_tail(handoff)
    path = original / "stage-report.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="native preparation receipt"):
        activate(handoff)


def prepare_revision(state, reason='Operator requested all four YG directional models to pass'):
    return deployment.prepare_revision_deployment(state['root'], action_date=DAY, reason=reason)


def revision_candidate(state, monkeypatch, *, failure=None):
    """A new immutable candidate with real numeric gates; native fit verification is mocked."""
    import copy
    import shutil
    from ml.gameplan_promotion import build_promotion_gate
    state['yg'] = state['publication']('yg-revision', True)
    state['current'] = state['yg']
    yg = state['yg']
    frame = pd.read_parquet(yg.run_directory/'forecasts.parquet')
    frame['symbol_fitted_target_rows'] = 100
    frame['symbol_route_fitted_target_rows'] = 10
    reports = {}
    for group in ('1h', '4h', '1d', '1w'):
        assessment = {'brier_score': .24, 'log_loss': .68, 'expected_calibration_error_10_bin': .04}
        baseline = {'brier_score': .25, 'log_loss': .69}
        diagnostics = {'information_available': True, 'calibrated_probability_range': [.4, .7],
            'assessment_probability_range': [.4, .7], 'nondecreasing_constraint_active': False,
            'calibration_positive_rate': .5}
        if failure == 'flat' and group == '1h':
            diagnostics.update(information_available=False, calibrated_probability_range=[.5, .5])
        if failure == 'assessment' and group == '1w':
            assessment.update(brier_score=.28, log_loss=.74)
        support = {'fitted_rows': 100, 'fitted_rows_by_route': {
            route: 10 for route in frame.loc[frame.model_group.eq(group), 'route']}}
        reports[group] = {'assessment': assessment, 'training_base_rate_assessment': baseline,
            'calibration_diagnostics': diagnostics, 'partition_decision_clusters': {'assessment': 20},
            'target_support_by_symbol': {'AAPL': support},
            'promotion_gate': build_promotion_gate(assessment, baseline, diagnostics, 20)}
    if failure == 'row_status':
        frame.loc[0, 'model_status'] = 'RESEARCH_NOT_PROMOTED'
    if failure == 'unsupported':
        row = frame.iloc[0]
        reports[row.model_group]['target_support_by_symbol']['AAPL']['fitted_rows_by_route'][row.route] = 0
    if failure == 'support_count':
        frame.loc[0, 'symbol_route_fitted_target_rows'] = 9
    frame.to_parquet(yg.run_directory/'forecasts.parquet', index=False)
    write(yg.run_directory/'model-reports.json', reports)
    write_manifest(yg.run_directory, run_timestamp='2026-09-18T08:10Z', input_files=(),
        output_files=('forecasts.parquet', 'model-reports.json'), configuration=yg.manifest['configuration'])
    yg.manifest = verify_manifest(yg.run_directory)
    monkeypatch.setattr('ml.stock_trader.independent_signals.verified_promoted_model_groups',
                        lambda publication: frozenset(('1h', '4h', '1d', '1w')))
    write(state['gameplan_pointer'], {'current': {'run_path': yg.run_directory.relative_to(state['root']).as_posix()}})
    state['yg_trade'] = state['trade']('yg-revision', yg)
    enrichment = state['root']/'ml/stock-trader-model-runs/yg-revision'
    shutil.copytree(state['enrichment'], enrichment)
    source_path = yg.run_directory.relative_to(state['root']).as_posix()
    source = {'source_gameplan_run': source_path, 'source_files': {
        source_path+'/receipt.json': file_checksum(yg.run_directory/'receipt.json')}}
    write(enrichment/'model.json', {'source_publication': source})
    # Research enrichment is deliberately permitted for the manual strategy.
    write(enrichment/'training-report.json', {**source, 'orders_placed': 0,
        'broker_orders_enabled': False, 'qualified_scope_count': 0})
    write_manifest(enrichment, run_timestamp='2026-09-18T08:12Z', input_files=(),
        output_files=('model.json', 'training-report.json'))
    write(enrichment/'receipt.json', {'schema_version': 'stock-trader-enrichment-training-receipt-v1',
        'run_path': enrichment.relative_to(state['root']).as_posix(),
        'manifest_sha256': file_checksum(enrichment/'manifest.json'),
        'model_sha256': file_checksum(enrichment/'model.json'),
        'training_report_sha256': file_checksum(enrichment/'training-report.json')})
    write(state['root']/'ml/stock-trader-model-latest/run.json', {
        'run_path': enrichment.relative_to(state['root']).as_posix(),
        'receipt_sha256': file_checksum(enrichment/'receipt.json')})
    native = state['root']/'ml/overnight-runs/yg-revision'
    shutil.copytree(state['native'], native)
    report = copy.deepcopy(state['report'])
    report['enrichment_gameplan'] = {**deployment._ref(state['root'], yg.run_directory), 'action_date': DAY}
    log = native/'stock_enrichment_training.log'
    log.write_text(json.dumps({'status': 'PUBLISHED', 'run_path': str(enrichment), 'orders_placed': 0})+'\n')
    write(native/'stage-report.json', report)
    receipt = json.loads((native/'receipt.json').read_text())
    receipt.update(run_path=native.relative_to(state['root']).as_posix(),
        stage_report_checksum_sha256=file_checksum(native/'stage-report.json'),
        stage_report_size=(native/'stage-report.json').stat().st_size)
    receipt['logs'][log.name] = {'checksum_sha256': file_checksum(log), 'size': log.stat().st_size}
    write(native/'receipt.json', receipt)
    state['native'], state['enrichment'] = native, enrichment


def test_active_revision_retains_og_prior_yg_and_registry_while_blocking_entries(handoff):
    active_run = activate(handoff)
    old = deployment.read_deployment(handoff['root'], DAY)
    old_bytes = {p: p.read_bytes() for p in [active_run/'receipt.json', active_run/'deployment.json',
        handoff['yg'].run_directory/'receipt.json', handoff['og'].run_directory/'receipt.json']}
    prepared = prepare_revision(handoff)
    report = deployment.read_deployment(handoff['root'], DAY)
    assert report['status'] == 'PREPARING' and report['require_all_directional_models_promoted'] is True
    assert report['OG'] == old['OG'] and report['OG_trade_plan'] == old['OG_trade_plan']
    assert report['revision']['previous_deployment'] == deployment._ref(handoff['root'], active_run)
    assert report['revision']['prior_active'] == {key: old[key] for key in deployment.PINNED_REFERENCE_FOLDERS}
    assert pd.Timestamp(report['original_deadline']) == pd.Timestamp(DEADLINE)
    assert report['YG'] is None and report['OG_execution_allowed'] is False
    for source in (handoff['og'], handoff['yg']):
        with pytest.raises(ValueError, match='NOT_ACTIVE'):
            deployment.assert_execution_gameplan(handoff['root'], source)
    assert all(p.read_bytes() == value for p, value in old_bytes.items())
    assert prepared != active_run


def test_revision_activates_all_promoted_new_yg_without_enrichment_qualification(handoff, monkeypatch):
    active_run = activate(handoff)
    original = deployment.read_deployment(handoff['root'], DAY)
    old_yg = handoff['yg']
    prepare_revision(handoff)
    revision_candidate(handoff, monkeypatch)
    new_run = activate(handoff)
    report = deployment.read_deployment(handoff['root'], DAY)
    assert report['status'] == 'ACTIVE' and new_run != active_run
    assert report['YG'] != original['YG'] and report['OG'] == original['OG']
    assert report['revision']['prior_active']['YG'] == original['YG']
    assert report['require_all_directional_models_promoted'] is True
    deployment.assert_execution_gameplan(handoff['root'], handoff['yg'])
    with pytest.raises(ValueError, match='NOT_EXECUTION_SOURCE'):
        deployment.assert_execution_gameplan(handoff['root'], old_yg)
    assert json.loads((handoff['enrichment']/'training-report.json').read_text())['qualified_scope_count'] == 0


@pytest.mark.parametrize('failure', ['flat', 'assessment', 'row_status', 'unsupported', 'support_count'])
def test_quality_revision_cannot_activate_failed_models_or_unsupported_rows(handoff, monkeypatch, failure):
    activate(handoff)
    prepare_revision(handoff)
    revision_candidate(handoff, monkeypatch, failure=failure)
    before = deployment._pointer(handoff['root'], DAY).read_bytes()
    with pytest.raises(ValueError, match='promot|support'):
        activate(handoff)
    assert deployment._pointer(handoff['root'], DAY).read_bytes() == before


def test_revision_rejects_reusing_prior_yg(handoff):
    activate(handoff)
    prepare_revision(handoff)
    with pytest.raises(ValueError, match='new immutable publication'):
        activate(handoff)


@pytest.mark.parametrize('case', ['not_active', 'empty_reason', 'late'])
def test_prepare_revision_requires_active_explicit_reason_and_original_deadline(handoff, monkeypatch, case):
    if case != 'not_active':
        activate(handoff)
    if case == 'late':
        def late(_):
            raise ValueError('YG_HANDOFF_DEADLINE_PASSED')
        monkeypatch.setattr(deployment, '_before_open', late)
    with pytest.raises(ValueError):
        prepare_revision(handoff, '' if case == 'empty_reason' else 'Explicit quality repair')


@pytest.mark.parametrize('name', ['registry', *deployment.SOURCE_POINTERS])
def test_prepare_revision_cas_preserves_independently_advanced_pointers(handoff, monkeypatch, name):
    activate(handoff)
    registry = deployment._pointer(handoff['root'], DAY)
    path = registry if name == 'registry' else handoff['root']/name
    before = registry.read_bytes()
    original = deployment._publish
    def race(root, report, **kwargs):
        path.write_text(path.read_text()+' ')
        return original(root, report, **kwargs)
    monkeypatch.setattr(deployment, '_publish', race)
    with pytest.raises(ValueError, match='advanced independently'):
        prepare_revision(handoff)
    assert registry.read_bytes() == before + (b' ' if name == 'registry' else b'')


@pytest.mark.parametrize('name', deployment.SOURCE_POINTERS)
def test_prepare_revision_refuses_already_advanced_selected_source(handoff, name):
    activate(handoff)
    path = handoff['root']/name
    payload = json.loads(path.read_text())
    current = payload.get('current', payload)
    current['run_path'] += '-advanced'
    write(path, payload)
    # The mocked publication reader must reflect the changed Gameplan pointer.
    if name == deployment.SOURCE_POINTERS[0]:
        handoff['current'] = handoff['og']
    with pytest.raises(ValueError, match='advanced independently'):
        prepare_revision(handoff)


def test_revision_activation_rejects_changed_previous_registry_receipt(handoff, monkeypatch):
    active_run = activate(handoff)
    prepare_revision(handoff)
    revision_candidate(handoff, monkeypatch)
    path = active_run/'receipt.json'
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError, match='previous|Previous'):
        activate(handoff)


def test_prepare_revision_cli_requires_reason(handoff):
    with pytest.raises(SystemExit) as exc:
        deployment.main(['prepare-revision', '--datastore', str(handoff['root']), '--action-date', DAY])
    assert exc.value.code == 2


@pytest.mark.parametrize('name', ['stage-report.json', 'stock_enrichment_training.log'])
def test_revision_activation_rejects_changed_retained_native_evidence(handoff, monkeypatch, name):
    activate(handoff)
    prior_native = handoff['native']
    prepare_revision(handoff)
    revision_candidate(handoff, monkeypatch)
    path = prior_native/name
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError, match='Retained native preparation'):
        activate(handoff)


def test_revision_gate_requires_verified_fitted_model_artifacts(handoff, monkeypatch):
    activate(handoff)
    prepare_revision(handoff)
    revision_candidate(handoff, monkeypatch)
    monkeypatch.setattr('ml.stock_trader.independent_signals.verified_promoted_model_groups',
                        lambda publication: frozenset(('4h', '1d', '1w')))
    with pytest.raises(ValueError, match='all four fitted'):
        activate(handoff)
