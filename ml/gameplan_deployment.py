"""Explicit, immutable OG/YG handoffs; this module has no order authority."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

import pandas as pd

from ml.artifacts import create_timestamp_directory, file_checksum, verify_manifest, write_manifest

VERSION = 'gameplan-variant-deployment-v1'
RAW_TARGET = 'raw-price-direction-v1'
SOURCE_POINTERS = ('ml/nightly-gameplan-latest/run.json',
    'ml/gameplan-trade-plan-latest/run.json', 'ml/stock-trader-model-latest/run.json')
PINNED_REFERENCE_FOLDERS = {'OG': 'ml/nightly-gameplan-runs',
    'OG_trade_plan': 'ml/gameplan-trade-plan-runs', 'YG': 'ml/nightly-gameplan-runs',
    'YG_trade_plan': 'ml/gameplan-trade-plan-runs', 'YG_enrichment': 'ml/stock-trader-model-runs',
    'native_preparation': 'ml/overnight-runs'}


def _json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid Gameplan deployment metadata')
    return value


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f'.tmp-{os.getpid()}')
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str)+'\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _pointer(root, action_date):
    session = date.fromisoformat(str(action_date)).isoformat()
    return Path(root)/'ml/gameplan-deployment-by-date'/session/'run.json'


def _path(root, relative, folder):
    run = (Path(root)/str(relative)).resolve()
    if run.parent != (Path(root)/folder).resolve():
        raise ValueError('Gameplan deployment reference escapes its run root')
    return run


def read_deployment(root, action_date):
    root = Path(root).resolve()
    pointer = _pointer(root, action_date)
    if not pointer.exists():
        return None
    saved = _json(pointer)
    if saved.get('schema_version') != VERSION:
        raise ValueError('Unknown Gameplan deployment pointer')
    current = saved.get('current', {})
    run = _path(root, current.get('run_path'), 'ml/gameplan-deployment-runs')
    if file_checksum(run/'receipt.json') != current.get('receipt_sha256'):
        raise ValueError('Gameplan deployment receipt changed')
    receipt = _json(run/'receipt.json')
    if (receipt.get('schema_version') != VERSION or receipt.get('action_date') != str(action_date)
            or receipt.get('run_path') != run.relative_to(root).as_posix()
            or receipt.get('manifest_sha256') != file_checksum(run/'manifest.json')):
        raise ValueError('Gameplan deployment receipt is invalid')
    verify_manifest(run)
    report = _json(run/'deployment.json')
    if (report.get('schema_version') != VERSION or report.get('action_date') != str(action_date)
            or report.get('status') not in ('PREPARING', 'ACTIVE')
            or report.get('status') != receipt.get('status') or report.get('orders_placed') != 0):
        raise ValueError('Gameplan deployment report is invalid')
    return report


def assert_execution_gameplan(datastore_root, publication=None, *, action_date=None):
    """Check the *loaded* entry source; never replace it with the latest source.

    Owned expiry exits do not use this guard. No deployment leaves the historical
    reader contract unchanged. A registered handoff fails closed until ACTIVE.
    """
    root = Path(datastore_root).resolve()
    run = getattr(publication, 'run_directory', publication)
    run = Path(run).resolve() if run is not None else None
    receipt = None
    if action_date is None:
        if run is None or not (run/'receipt.json').is_file():
            # Unresolved identity cannot bypass an already registered handoff.
            if any((root/'ml/gameplan-deployment-by-date').glob('*/run.json')):
                raise ValueError('YG_EXECUTION_SOURCE_UNRESOLVED')
            return
        receipt = _json(run/'receipt.json')
        action_date = receipt.get('action_date')
    deployment = read_deployment(root, action_date)
    if deployment is None:
        return
    if deployment['status'] != 'ACTIVE':
        raise ValueError('YG_DEPLOYMENT_NOT_ACTIVE_OG_EVALUATION_ONLY')
    expected = deployment['YG']
    expected_run = _path(root, expected['run_path'], 'ml/nightly-gameplan-runs')
    if run != expected_run:
        raise ValueError('OG_OR_UNSELECTED_GAMEPLAN_IS_NOT_EXECUTION_SOURCE')
    receipt = receipt or _json(run/'receipt.json')
    if (receipt.get('action_date') != str(action_date)
            or file_checksum(run/'receipt.json') != expected['receipt_sha256']):
        raise ValueError('YG_EXECUTION_SOURCE_CHANGED')


def _ref(root, run):
    return {'run_path': run.relative_to(root).as_posix(),
            'receipt_sha256': file_checksum(run/'receipt.json')}


def _before_open(action_date):
    now = pd.Timestamp.now(tz='UTC')
    opening = pd.Timestamp(str(action_date), tz='America/Los_Angeles')+pd.Timedelta(hours=4)
    if now >= opening:
        raise ValueError('YG_HANDOFF_DEADLINE_PASSED')
    return now


def _publish(root, report, *, inputs, comparison=None, expected_pointer=None, expected_files=None):
    def sources_unchanged():
        for path, checksum in (expected_files or {}).items():
            if not Path(path).is_file() or file_checksum(path) != checksum:
                raise ValueError('Gameplan preparation source advanced independently')
    sources_unchanged()
    pointer = _pointer(root, report['action_date'])
    current_hash = file_checksum(pointer) if pointer.exists() else None
    if current_hash != expected_pointer:
        raise ValueError('Gameplan deployment advanced independently')
    now = _before_open(report['action_date'])
    run = create_timestamp_directory(root/'ml/gameplan-deployment-runs', timestamp=now)
    report = {**report, 'updated_at': now.isoformat(), 'orders_placed': 0,
              'broker_orders_enabled': False, 'execution_authority': 'SOURCE_SELECTION_ONLY'}
    _write(run/'deployment.json', report)
    outputs = ['deployment.json']
    if comparison is not None:
        comparison.to_parquet(run/'forecast-comparison.parquet', index=False)
        lines = [f"# OG versus Yung Gameplan (YG): {report['action_date']}", '',
            'YG is the selected execution source. OG is retained for evaluation only.', '',
            'OG probabilities estimate positive return after assumed costs; YG probabilities estimate raw price increases. Probability differences therefore compare different events and are not accuracy improvements.', '',
            'Observed same-window outcomes will be evaluated separately for each frozen target contract. No future results or broker P/L are inferred.', '',
            '| Horizon | Paired rows | Direction changed | OG bullish | YG bullish |',
            '|---|---:|---:|---:|---:|']
        for group, frame in comparison.groupby('model_group', sort=False):
            lines.append(f"| {group} | {len(frame)} | {int(frame.direction_og.ne(frame.direction_yg).sum())} | {int(frame.direction_og.eq('BULLISH').sum())} | {int(frame.direction_yg.eq('BULLISH').sum())} |")
        lines += ['', f"OG saved plan: {root/report['OG']['run_path']}", f"YG saved plan: {root/report['YG']['run_path']}", '']
        (run/'Comparison.md').write_text('\n'.join(lines), encoding='utf-8')
        outputs += ['forecast-comparison.parquet', 'Comparison.md']
    write_manifest(run, run_timestamp=now, input_files=inputs, output_files=outputs,
        configuration={'schema_version': VERSION, 'action_date': report['action_date'],
                       'status': report['status'], 'orders_placed': 0}, datastore_root=root)
    verify_manifest(run)
    _write(run/'receipt.json', {'schema_version': VERSION, 'action_date': report['action_date'],
        'status': report['status'], 'run_path': run.relative_to(root).as_posix(),
        'manifest_sha256': file_checksum(run/'manifest.json'), 'orders_placed': 0})
    _before_open(report['action_date'])
    sources_unchanged()
    if (file_checksum(pointer) if pointer.exists() else None) != expected_pointer:
        raise ValueError('Gameplan deployment advanced independently')
    _write(pointer, {'schema_version': VERSION, 'current': _ref(root, run)})
    return run


def _native_preparation_chain(root, native, *, yg, deadline):
    """Read completed stages from hash-verified attempts, never trust a name-only list."""
    required = ('gameplan_publication', 'stock_enrichment_training', 'gameplan_trade_planning')
    selected, inputs, checksums, seen, chain = {}, [], {}, set(), []
    first = True
    while native is not None:
        native = _path(root, native, 'ml/overnight-runs')
        if native in seen:
            raise ValueError('YG native resume ancestry contains a cycle')
        seen.add(native)
        receipt, report = _json(native/'receipt.json'), _json(native/'stage-report.json')
        statuses = ('COMPLETE',) if first else ('FAILED', 'CANCELLED')
        pin = report.get('enrichment_gameplan', {})
        rows = report.get('stages', [])
        rows = list(rows.values()) if isinstance(rows, dict) else rows
        if (receipt.get('schema_version') != 'supervised-overnight-gameplan-runtime-v2'
                or report.get('schema_version') != receipt.get('schema_version')
                or receipt.get('run_path') != native.relative_to(root).as_posix()
                or receipt.get('status') not in statuses or report.get('status') != receipt.get('status')
                or receipt.get('stage_report_checksum_sha256') != file_checksum(native/'stage-report.json')
                or receipt.get('stage_report_size') != (native/'stage-report.json').stat().st_size
                or receipt.get('orders_placed') != 0 or receipt.get('broker_orders_enabled') is not False
                or report.get('orders_placed') != 0 or report.get('broker_orders_enabled') is not False
                or report.get('stock_only') is not True or report.get('independent_stock_horizons') is not True
                or report.get('stock_price_source') != yg.manifest['configuration'].get('target_price_source_contract')
                or report.get('probability_target_contract') != RAW_TARGET or report.get('gameplan_variant') != 'YG'
                or pd.Timestamp(report['deadline_at']) != pd.Timestamp(deadline)
                or pd.Timestamp(report.get('effective_deadline_at', report['deadline_at'])) != pd.Timestamp(deadline)
                or report.get('deadline_exception') is not None):
            raise ValueError('YG native preparation receipt is incomplete or source-mismatched')
        complete = {row['stage'] for row in rows if row.get('status') == 'COMPLETE'}
        if first or complete.intersection(required):
            if (pin.get('run_path') != yg.run_directory.relative_to(root).as_posix()
                    or pin.get('receipt_sha256') != file_checksum(yg.run_directory/'receipt.json')
                    or pin.get('action_date') != yg.receipt['action_date']):
                raise ValueError('YG native preparation pin differs from its selected publication')
        for path in (native/'receipt.json', native/'stage-report.json'):
            inputs.append(path)
            checksums[path] = file_checksum(path)
        logs = receipt.get('logs', {})
        for log_name, evidence in logs.items():
            path = native/log_name
            if (Path(log_name).name != log_name or path.resolve().parent != native
                    or file_checksum(path) != evidence.get('checksum_sha256')
                    or path.stat().st_size != evidence.get('size')):
                raise ValueError('YG native log differs from its receipt')
            inputs.append(path)
            checksums[path] = file_checksum(path)
        for row in rows:
            stage = row.get('stage')
            if stage not in required or row.get('status') != 'COMPLETE':
                continue
            start, end = pd.Timestamp(row.get('started_at')), pd.Timestamp(row.get('finished_at'))
            if (row.get('exit_code') != 0 or row.get('log_path') not in logs
                    or pd.isna(start) or pd.isna(end) or start.tzinfo is None or end.tzinfo is None
                    or not start <= end < pd.Timestamp(deadline)):
                raise ValueError('YG completed native stage has invalid time or log evidence')
            if stage in selected:
                raise ValueError('YG native chain repeats a successful preparation stage')
            selected[stage] = (native/row['log_path'], start, end)
        chain.append((report, complete))
        first = False
        native = report.get('resumed_from')
    for index, (report, _) in enumerate(chain):
        inherited = set().union(*(complete for _, complete in chain[index + 1:]))
        if set(report.get('completed_stages_from_previous_attempt', [])) != inherited:
            raise ValueError('YG native resume completion names differ from verified ancestry')
    for stage in required:
        if stage not in selected:
            raise ValueError(f'YG native stage incomplete: {stage}')
    if any(selected[left][2] > selected[right][1] for left, right in zip(required, required[1:])):
        raise ValueError('YG native preparation stages are not chronological')
    return selected, inputs, checksums


def _verified_enrichment(root, yg, log):
    from ml.stock_trader.model import load_current_enrichment_model
    pointer_path = root/'ml/stock-trader-model-latest/run.json'
    pointer = _json(pointer_path)
    run = _path(root, pointer.get('run_path'), 'ml/stock-trader-model-runs')
    published = []
    for line in log.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('status') == 'PUBLISHED':
            published.append(row)
    if (len(published) != 1 or _path(root, published[0].get('run_path'), 'ml/stock-trader-model-runs') != run
            or published[0].get('orders_placed') != 0):
        raise ValueError('YG enrichment log does not bind the current native model')
    load_current_enrichment_model(root)  # Native model, source cohorts and assessment verification; no fit.
    manifest = verify_manifest(run)
    receipt, report, payload = (_json(run/name) for name in ('receipt.json', 'training-report.json', 'model.json'))
    source = payload.get('source_publication', {})
    source_path = yg.run_directory.relative_to(root).as_posix()
    if (receipt.get('run_path') != run.relative_to(root).as_posix()
            or receipt.get('schema_version') != 'stock-trader-enrichment-training-receipt-v1'
            or receipt.get('manifest_sha256') != file_checksum(run/'manifest.json')
            or receipt.get('model_sha256') != file_checksum(run/'model.json')
            or receipt.get('training_report_sha256') != file_checksum(run/'training-report.json')
            or pointer.get('receipt_sha256') != file_checksum(run/'receipt.json')
            or source.get('source_gameplan_run') != source_path or report.get('source_gameplan_run') != source_path
            or source.get('source_files', {}).get(source_path+'/receipt.json') != file_checksum(yg.run_directory/'receipt.json')
            or report.get('source_files') != source.get('source_files')
            or report.get('orders_placed') != 0 or report.get('broker_orders_enabled') is not False):
        raise ValueError('YG enrichment evidence differs from its frozen Gameplan')
    return run, [run/name for name in ('receipt.json', 'manifest.json', 'training-report.json', 'model.json')]


def prepare_deployment(root, *, action_date):
    from ml.nightly_gameplan import read_current_gameplan
    root = Path(root).resolve()
    _before_open(action_date)
    if read_deployment(root, action_date) is not None:
        raise ValueError('A Gameplan deployment already exists for this session')
    original = read_current_gameplan(root)
    config = original.manifest['configuration']
    if (original.receipt['action_date'] != action_date
            or config.get('probability_target_contract') == RAW_TARGET):
        raise ValueError('OG baseline must be the current original cost-target Gameplan')
    trade_pointer = _json(root/'ml/gameplan-trade-plan-latest/run.json')
    trade = _path(root, trade_pointer['current']['run_path'], 'ml/gameplan-trade-plan-runs')
    verify_manifest(trade)
    receipt = _json(trade/'receipt.json')
    if (receipt.get('status') != 'COMPLETE' or receipt.get('action_date') != action_date
            or receipt.get('source_receipt_sha256') != file_checksum(original.run_directory/'receipt.json')
            or file_checksum(trade/'receipt.json') != trade_pointer['current'].get('receipt_sha256')):
        raise ValueError('OG trade plan does not match the original frozen Gameplan')
    report = {'schema_version': VERSION, 'action_date': action_date, 'status': 'PREPARING',
        'required_variant': 'YG', 'OG': _ref(root, original.run_directory),
        'OG_trade_plan': _ref(root, trade), 'YG': None,
        'authorization': 'Operator selected Yung Gameplan for September 18; original OG evaluation only.',
        'original_deadline': (pd.Timestamp(action_date, tz='America/Los_Angeles')+pd.Timedelta(hours=4)).isoformat()}
    return _publish(root, report, inputs=[original.run_directory/'receipt.json', trade/'receipt.json'])


def _pinned_reference_files(root, report):
    """Validate retained identities without changing any selected pointer."""
    files = {}
    for key, folder in PINNED_REFERENCE_FOLDERS.items():
        ref = report.get(key, {})
        run = _path(root, ref.get('run_path'), folder)
        path = run/'receipt.json'
        if file_checksum(path) != ref.get('receipt_sha256'):
            raise ValueError(f'Retained {key} receipt changed')
        files[path] = ref['receipt_sha256']
        if key == 'native_preparation':
            receipt = _json(path)
            stage_report = run/'stage-report.json'
            if (file_checksum(stage_report) != receipt.get('stage_report_checksum_sha256')
                    or stage_report.stat().st_size != receipt.get('stage_report_size')):
                raise ValueError('Retained native preparation report changed')
            files[stage_report] = receipt['stage_report_checksum_sha256']
            for name, evidence in receipt.get('logs', {}).items():
                log = run/name
                if (Path(name).name != name or log.resolve().parent != run
                        or file_checksum(log) != evidence.get('checksum_sha256')
                        or log.stat().st_size != evidence.get('size')):
                    raise ValueError('Retained native preparation log changed')
                files[log] = evidence['checksum_sha256']
        else:
            verify_manifest(run)
            receipt = _json(path)
            manifest_hash = receipt.get('manifest_sha256', receipt.get('manifest_checksum_sha256'))
            if manifest_hash is not None and manifest_hash != file_checksum(run/'manifest.json'):
                raise ValueError(f'Retained {key} manifest changed')
            files[run/'manifest.json'] = file_checksum(run/'manifest.json')
    return files


def prepare_revision_deployment(root, *, action_date, reason):
    """Freeze an ACTIVE YG as history and block entries during its quality revision."""
    from ml.nightly_gameplan import read_current_gameplan
    root = Path(root).resolve()
    _before_open(action_date)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('YG revision requires an explicit operator reason')
    pointer = _pointer(root, action_date)
    expected_pointer = file_checksum(pointer) if pointer.exists() else None
    expected_files = {root/name: file_checksum(root/name) for name in SOURCE_POINTERS}
    active = read_deployment(root, action_date)
    if active is None or active['status'] != 'ACTIVE':
        raise ValueError('YG revision requires an ACTIVE deployment')
    previous_ref = _json(pointer)['current']
    previous = _path(root, previous_ref['run_path'], 'ml/gameplan-deployment-runs')
    if file_checksum(previous/'receipt.json') != previous_ref['receipt_sha256']:
        raise ValueError('Previous deployment receipt changed')
    expected_files.update(_pinned_reference_files(root, active))
    expected_files.update({previous/name: file_checksum(previous/name)
                          for name in ('receipt.json', 'manifest.json', 'deployment.json')})
    yg = read_current_gameplan(root)
    if _ref(root, yg.run_directory) != active['YG']:
        raise ValueError('Current YG advanced independently of its ACTIVE deployment')
    trade_current = _json(root/SOURCE_POINTERS[1])['current']
    enrichment_current = _json(root/SOURCE_POINTERS[2])
    for key, current in (('YG_trade_plan', trade_current), ('YG_enrichment', enrichment_current)):
        if any(current.get(field) != active[key][field] for field in ('run_path', 'receipt_sha256')):
            raise ValueError(f'Current {key} advanced independently of its ACTIVE deployment')
    native = _path(root, active['native_preparation']['run_path'], 'ml/overnight-runs')
    stages, native_inputs, native_files = _native_preparation_chain(
        root, native, yg=yg, deadline=active['original_deadline'])
    enrichment, enrichment_inputs = _verified_enrichment(root, yg, stages['stock_enrichment_training'][0])
    if _ref(root, enrichment) != active['YG_enrichment']:
        raise ValueError('Current enrichment differs from the ACTIVE deployment')
    expected_files.update(native_files)
    expected_files.update({path: file_checksum(path) for path in enrichment_inputs})
    prior = {key: active[key] for key in PINNED_REFERENCE_FOLDERS}
    report = {**active, 'status': 'PREPARING', 'active_variant': None,
        'YG': None, 'YG_trade_plan': None, 'YG_enrichment': None, 'native_preparation': None,
        'OG_execution_allowed': False, 'require_all_directional_models_promoted': True,
        'revision': {'reason': reason.strip(), 'previous_deployment': previous_ref,
                     'prior_active': prior}}
    inputs = list(dict.fromkeys([*expected_files, *native_inputs, *enrichment_inputs]))
    return _publish(root, report, inputs=inputs, expected_pointer=expected_pointer,
                    expected_files=expected_files)


def _revision_history(root, report):
    revision = report.get('revision')
    if revision is None:
        return {}, None
    if report.get('require_all_directional_models_promoted') is not True:
        raise ValueError('YG revision cannot drop its all-model promotion requirement')
    ref = revision.get('previous_deployment', {})
    previous = _path(root, ref.get('run_path'), 'ml/gameplan-deployment-runs')
    if file_checksum(previous/'receipt.json') != ref.get('receipt_sha256'):
        raise ValueError('Previous ACTIVE deployment receipt changed')
    receipt = _json(previous/'receipt.json')
    verify_manifest(previous)
    prior = _json(previous/'deployment.json')
    if (receipt.get('manifest_sha256') != file_checksum(previous/'manifest.json')
            or receipt.get('status') != 'ACTIVE' or prior.get('status') != 'ACTIVE'
            or prior.get('action_date') != report['action_date']
            or {key: prior.get(key) for key in PINNED_REFERENCE_FOLDERS} != revision.get('prior_active')
            or any(prior.get(key) != report.get(key) for key in ('OG', 'OG_trade_plan', 'original_deadline'))):
        raise ValueError('YG revision differs from its retained ACTIVE deployment')
    files = _pinned_reference_files(root, prior)
    files.update({previous/name: file_checksum(previous/name)
                  for name in ('receipt.json', 'manifest.json', 'deployment.json')})
    return files, prior


def _require_all_directional_models_promoted(yg):
    """The revision's quality promise covers every model and exact forecast route."""
    from ml.gameplan_promotion import validate_promoted_report
    from ml.stock_trader.independent_signals import verified_promoted_model_groups
    reports = _json(yg.run_directory/'model-reports.json')
    groups = {'1h', '4h', '1d', '1w'}
    if set(reports) != groups:
        raise ValueError('YG revision requires exactly four directional model reports')
    for report in reports.values():
        validate_promoted_report(report)
    if verified_promoted_model_groups(yg) != groups:
        raise ValueError('YG revision requires all four fitted directional models promoted')
    rows = pd.read_parquet(yg.run_directory/'forecasts.parquet')
    symbols = yg.manifest['configuration']['symbols']
    if (len(rows) != 24*len(symbols) or not rows.model_status.eq('PROMOTED').all()
            or rows.id.isna().any() or rows.id.duplicated().any()
            or set(rows.symbol) != set(symbols) or not rows.groupby('symbol').size().eq(24).all()):
        raise ValueError('YG revision requires all 24 forecasts per symbol promoted')
    for row in rows.to_dict('records'):
        counts = reports[row['model_group']]['target_support_by_symbol'].get(row['symbol'], {})
        fitted = counts.get('fitted_rows', 0)
        route_fitted = counts.get('fitted_rows_by_route', {}).get(row['route'], 0)
        if (fitted <= 0 or route_fitted <= 0 or row.get('symbol_fitted_target_rows') != fitted
                or row.get('symbol_route_fitted_target_rows') != route_fitted):
            raise ValueError('YG revision forecast lacks matching exact-symbol/route fitted support')


def activate_deployment(root, *, action_date, overnight_run):
    from ml.nightly_gameplan import read_current_gameplan, read_gameplan_run
    root = Path(root).resolve()
    source_pointers = [root/name for name in SOURCE_POINTERS]
    expected_files = {path: file_checksum(path) for path in source_pointers}
    pointer = _pointer(root, action_date)
    expected_pointer = file_checksum(pointer)
    report = read_deployment(root, action_date)
    if report is None or report['status'] != 'PREPARING':
        raise ValueError('YG activation requires its original PREPARING handoff')
    _before_open(action_date)
    revision_files, prior_active = _revision_history(root, report)
    expected_files.update(revision_files)
    original_run = _path(root, report['OG']['run_path'], 'ml/nightly-gameplan-runs')
    original = read_gameplan_run(root, original_run)
    expected_files[original_run/'receipt.json'] = report['OG']['receipt_sha256']
    if file_checksum(original_run/'receipt.json') != report['OG']['receipt_sha256']:
        raise ValueError('OG baseline changed')
    yg = read_current_gameplan(root)
    expected_files[yg.run_directory/'receipt.json'] = file_checksum(yg.run_directory/'receipt.json')
    expected_files[yg.run_directory/'manifest.json'] = file_checksum(yg.run_directory/'manifest.json')
    config = yg.manifest['configuration']
    if prior_active is not None and yg.run_directory.relative_to(root).as_posix() == prior_active['YG']['run_path']:
        raise ValueError('YG quality revision requires a new immutable publication')
    if (yg.receipt['action_date'] != action_date or config.get('probability_target_contract') != RAW_TARGET
            or config.get('gameplan_variant') != 'YG' or yg.run_directory == original_run
            or config.get('symbols') != original.manifest['configuration'].get('symbols')
            or config.get('target_price_source_contract') != original.manifest['configuration'].get('target_price_source_contract')):
        raise ValueError('YG publication differs from its required target, session or universe')
    trade_pointer = _json(root/'ml/gameplan-trade-plan-latest/run.json')
    trade = _path(root, trade_pointer['current']['run_path'], 'ml/gameplan-trade-plan-runs')
    expected_files[trade/'receipt.json'] = trade_pointer['current'].get('receipt_sha256')
    verify_manifest(trade)
    trade_receipt = _json(trade/'receipt.json')
    if (trade_receipt.get('status') != 'COMPLETE' or trade_receipt.get('action_date') != action_date
            or trade_receipt.get('source_receipt_sha256') != file_checksum(yg.run_directory/'receipt.json')
            or trade_receipt.get('source_gameplan_run') != yg.run_directory.relative_to(root).as_posix()
            or trade_receipt.get('manifest_sha256') != file_checksum(trade/'manifest.json')
            or trade_receipt.get('forecast_rows') != 24*len(config['symbols'])
            or trade_receipt.get('orders_placed') != 0 or trade_receipt.get('broker_orders_enabled') is not False
            or file_checksum(trade/'receipt.json') != trade_pointer['current'].get('receipt_sha256')):
        raise ValueError('YG trade plan is incomplete or source-mismatched')
    native = _path(root, overnight_run, 'ml/overnight-runs')
    stages, native_inputs, native_checksums = _native_preparation_chain(
        root, native, yg=yg, deadline=report['original_deadline'])
    enrichment, enrichment_inputs = _verified_enrichment(root, yg, stages['stock_enrichment_training'][0])
    if prior_active is not None:
        for key, run in (('YG_trade_plan', trade), ('YG_enrichment', enrichment), ('native_preparation', native)):
            if run.relative_to(root).as_posix() == prior_active[key]['run_path']:
                raise ValueError('YG quality revision requires newly bound preparation outputs')
    if report.get('require_all_directional_models_promoted') is True:
        _require_all_directional_models_promoted(yg)
    expected_files.update(native_checksums)
    expected_files.update({path: file_checksum(path) for path in enrichment_inputs})
    og_rows, yg_rows = pd.read_parquet(original_run/'forecasts.parquet'), pd.read_parquet(yg.run_directory/'forecasts.parquet')
    keys = ['symbol', 'model_group', 'route', 'target_window_start', 'target_window_end']
    columns = [*keys, 'id', 'calibrated_probability', 'direction', 'model_status']
    paired = og_rows[columns].merge(yg_rows[columns], on=keys, how='outer', suffixes=('_og','_yg'), validate='one_to_one', indicator=True)
    if not paired['_merge'].eq('both').all() or len(paired) != 24*len(config['symbols']):
        raise ValueError('OG and YG forecast windows do not pair exactly')
    report.update(status='ACTIVE', YG=_ref(root, yg.run_directory), YG_trade_plan=_ref(root, trade),
        YG_enrichment=_ref(root, enrichment),
        native_preparation=_ref(root, native), active_variant='YG', OG_execution_allowed=False)
    inputs = [*revision_files, original_run/'receipt.json', yg.run_directory/'receipt.json', trade/'receipt.json',
              trade/'manifest.json', *native_inputs, *enrichment_inputs]
    for path in inputs:
        expected_files.setdefault(path, file_checksum(path))
    return _publish(root, report, inputs=inputs, comparison=paired.drop(columns='_merge'),
                    expected_pointer=expected_pointer, expected_files=expected_files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'prepare-revision', 'activate'))
    parser.add_argument('--datastore', type=Path, default=Path('C:/DATASTORE'))
    parser.add_argument('--action-date', required=True)
    parser.add_argument('--overnight-run', type=Path)
    parser.add_argument('--reason', help='Explicit operator reason for a pre-open YG quality revision')
    args = parser.parse_args(argv)
    from datafetching.runtime_lock import exclusive_runtime_lock
    with exclusive_runtime_lock(args.datastore/'state/gameplan-deployment.lock', process_name='Gameplan source selection'):
        if args.operation == 'prepare':
            run = prepare_deployment(args.datastore, action_date=args.action_date)
        elif args.operation == 'prepare-revision':
            if not args.reason or not args.reason.strip():
                parser.error('--reason is required for prepare-revision')
            run = prepare_revision_deployment(args.datastore, action_date=args.action_date, reason=args.reason)
        else:
            if args.overnight_run is None:
                parser.error('--overnight-run is required for activation')
            run = activate_deployment(args.datastore, action_date=args.action_date, overnight_run=args.overnight_run)
    print(json.dumps({'status': 'ACTIVE' if args.operation == 'activate' else 'PREPARING', 'run_path': str(run), 'orders_placed': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
