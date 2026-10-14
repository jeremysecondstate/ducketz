"""Offline coverage for continuing frozen, non-trading preparation evidence."""

import json

import pandas as pd
import pytest

from ml.artifacts import file_checksum, write_manifest
from ml.nightly_recovery import (
    VERSION as NIGHTLY_VERSION,
    make_recovery,
    verify_recovery,
    verify_saved_recovery,
)
from ml.preparation_deadline import RECOVERY_VERSION, VERSION, preparation_deadline


REQUESTED = '2026-09-14T11:20:00Z'
AFTER_CLOSE = '2026-09-16T01:00:00Z'
ORIGINAL = '2026-09-14T11:00:00Z'


def save_json(path, payload):
    path.write_text(json.dumps(payload), encoding='utf-8')


@pytest.fixture
def recovery_case(tmp_path):
    payload = make_recovery({'actor': 'Scout', 'datastore': tmp_path}, REQUESTED,
                            reason='Continue interrupted preparation')
    path = tmp_path / 'recovery.json'
    save_json(path, payload)
    return tmp_path, path, payload


@pytest.mark.parametrize('reason', [None, '', '   ', 'Provider interruption'])
def test_recovery_creation_does_not_require_operator_reason(tmp_path, reason):
    kwargs = {} if reason is None else {'reason': reason}
    payload = make_recovery({'actor': 'Scout', 'datastore': tmp_path}, REQUESTED, **kwargs)
    assert payload['schema_version'] == NIGHTLY_VERSION
    assert payload['requested_at'] == pd.Timestamp(REQUESTED).isoformat()
    assert payload['original_deadline_at'] == pd.Timestamp(ORIGINAL).isoformat()
    assert payload['expires_at'] == '2026-09-14T18:20:00+00:00'
    assert payload['training_information_cutoff'] == '2026-09-14T10:59:59.999999+00:00'
    assert payload['orders_authorized'] is False
    assert 'operator_authorized' in payload and 'authorization_reason' in payload
    path = tmp_path / 'recovery.json'
    save_json(path, payload)
    assert verify_recovery(tmp_path, path, AFTER_CLOSE)['authorization'] == payload


@pytest.mark.parametrize('actor', ['Scout', 'Atlas'])
def test_frozen_recovery_survives_expiry_and_action_close_without_renewal(recovery_case, actor):
    root, path, payload = recovery_case
    payload['actor'] = actor
    save_json(path, payload)
    original_bytes = path.read_bytes()
    evidence = verify_recovery(root, path, REQUESTED, action_date='2026-09-14')
    for observed in (payload['expires_at'], payload['session_close_at'], AFTER_CLOSE):
        assert verify_recovery(root, path, observed, action_date='2026-09-14') == evidence
        assert verify_saved_recovery(root, evidence, observed, action_date='2026-09-14') == evidence
    assert path.read_bytes() == original_bytes
    assert evidence['authorization'] == payload


@pytest.mark.parametrize('legacy_authority', ['absent', 'false', 'empty'])
def test_legacy_recovery_authority_fields_are_informational(recovery_case, legacy_authority):
    root, path, payload = recovery_case
    if legacy_authority == 'absent':
        payload.pop('operator_authorized')
        payload.pop('authorization_reason')
    else:
        payload['operator_authorized'] = False
        payload['authorization_reason'] = '' if legacy_authority == 'empty' else 'Historical note'
    save_json(path, payload)
    assert verify_recovery(root, path, AFTER_CLOSE)['authorization'] == payload


@pytest.mark.parametrize(('field', 'value'), [
    ('schema_version', 'unknown-recovery'),
    ('actor', 'Other'),
    ('datastore', 'different-datastore'),
    ('source_session', '2026-09-10'),
    ('action_date', '2026-09-15'),
    ('original_deadline_at', '2026-09-14T11:01:00+00:00'),
    ('session_close_at', '2026-09-15T01:00:00+00:00'),
    ('training_information_cutoff', ORIGINAL),
    ('orders_authorized', True),
    ('orders_authorized', None),
    ('expires_at', REQUESTED),
    ('expires_at', '2026-09-14T11:19:00Z'),
    ('expires_at', '2026-09-14T18:20:01Z'),
])
def test_recovery_retains_scope_and_structural_constraints(recovery_case, field, value):
    root, path, payload = recovery_case
    payload[field] = value
    save_json(path, payload)
    with pytest.raises(ValueError):
        verify_recovery(root, path, AFTER_CLOSE)


def test_recovery_checks_requested_time_and_callers_action_session(recovery_case):
    root, path, _ = recovery_case
    with pytest.raises(ValueError):
        verify_recovery(root, path, '2026-09-14T11:19:59Z')
    with pytest.raises(ValueError):
        verify_recovery(root, path, pd.NaT)
    with pytest.raises(ValueError):
        verify_recovery(root, path, AFTER_CLOSE, action_date='2026-09-15')


def test_recovery_window_is_clamped_to_original_action_close(tmp_path):
    requested = '2026-09-14T23:00:00Z'
    payload = make_recovery({'actor': 'Scout', 'datastore': tmp_path}, requested)
    assert payload['expires_at'] == payload['session_close_at'] == '2026-09-15T00:00:00+00:00'
    path = tmp_path / 'recovery.json'
    save_json(path, payload)
    assert verify_recovery(tmp_path, path, AFTER_CLOSE)['authorization'] == payload
    payload['expires_at'] = '2026-09-15T00:00:01+00:00'
    save_json(path, payload)
    with pytest.raises(ValueError):
        verify_recovery(tmp_path, path, AFTER_CLOSE)


@pytest.mark.parametrize('field', [
    'requested_at', 'expires_at', 'original_deadline_at',
    'training_information_cutoff', 'session_close_at',
])
@pytest.mark.parametrize('value', ['2026-09-14T11:20:00', 'NaT'])
def test_recovery_rejects_naive_or_missing_timestamps(recovery_case, field, value):
    root, path, payload = recovery_case
    payload[field] = value
    save_json(path, payload)
    with pytest.raises(ValueError):
        verify_recovery(root, path, AFTER_CLOSE)


@pytest.mark.parametrize('change', ['bytes-only', 'informational-field'])
def test_saved_recovery_requires_exact_original_bytes(recovery_case, change):
    root, path, payload = recovery_case
    evidence = verify_recovery(root, path, REQUESTED)
    if change == 'bytes-only':
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    else:
        payload['authorization_reason'] = 'Another historical note'
        save_json(path, payload)
    assert verify_recovery(root, path, AFTER_CLOSE)['sha256'] != evidence['sha256']
    with pytest.raises(ValueError, match='changed since this run'):
        verify_saved_recovery(root, evidence, AFTER_CLOSE)


@pytest.fixture(params=[VERSION, RECOVERY_VERSION])
def tail_case(tmp_path, request):
    source = tmp_path / 'ml/nightly-gameplan-runs/frozen'
    source.mkdir(parents=True)
    save_json(source / 'receipt.json', {'action_date': '2026-09-14'})
    original = ORIGINAL if request.param == VERSION else '2026-09-14T18:20:00Z'
    approved = pd.Timestamp(original) + pd.Timedelta(minutes=1)
    payload = {
        'schema_version': request.param,
        'scope': 'PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY',
        'operator_authorized': False,
        'orders_authorized': False,
        'action_date': '2026-09-14',
        'gameplan_run': source.relative_to(tmp_path).as_posix(),
        'gameplan_receipt_sha256': file_checksum(source / 'receipt.json'),
        'original_deadline_at': original,
        'approved_at': approved.isoformat(),
        'expires_at': (approved + pd.Timedelta(hours=1)).isoformat(),
    }
    if request.param == RECOVERY_VERSION:
        payload['original_session_deadline_at'] = ORIGINAL
    path = tmp_path / 'continuation.json'
    save_json(path, payload)
    return tmp_path, source, path, payload


def test_tail_continuation_preserves_both_deadlines_after_close(tail_case):
    root, source, path, payload = tail_case
    originals = {p: p.read_bytes() for p in (path, source / 'receipt.json')}
    before = preparation_deadline(root, source, payload['original_deadline_at'],
                                  payload['approved_at'], path)
    after = preparation_deadline(root, source, payload['original_deadline_at'], AFTER_CLOSE, path)
    assert after == before
    assert after[0] == pd.Timestamp(payload['expires_at'])
    assert after[1]['authorization'] == payload
    assert after[1]['sha256'] == file_checksum(path)
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize(('field', 'value'), [
    ('schema_version', 'unknown-tail'),
    ('scope', 'TRADING'),
    ('orders_authorized', True),
    ('action_date', '2026-09-15'),
    ('gameplan_run', 'ml/nightly-gameplan-runs/another'),
    ('gameplan_receipt_sha256', 'changed'),
    ('original_deadline_at', '2026-09-14T11:01:00Z'),
    ('expires_at', '2026-09-15T00:00:01Z'),
])
def test_tail_retains_source_session_and_nontrading_constraints(tail_case, field, value):
    root, source, path, payload = tail_case
    original = payload['original_deadline_at']
    payload[field] = value
    save_json(path, payload)
    with pytest.raises(ValueError):
        preparation_deadline(root, source, original, AFTER_CLOSE, path)


@pytest.mark.parametrize('damage', ['future-approval', 'before-original', 'empty-window', 'reversed-window'])
def test_tail_retains_time_ordering_after_expiry(tail_case, damage):
    root, source, path, payload = tail_case
    observed = AFTER_CLOSE
    if damage == 'future-approval':
        observed = payload['original_deadline_at']
    elif damage == 'before-original':
        payload['approved_at'] = (pd.Timestamp(payload['original_deadline_at']) - pd.Timedelta(seconds=1)).isoformat()
    elif damage == 'empty-window':
        payload['expires_at'] = payload['approved_at']
    else:
        payload['expires_at'] = payload['original_deadline_at']
    save_json(path, payload)
    with pytest.raises(ValueError):
        preparation_deadline(root, source, payload['original_deadline_at'], observed, path)


@pytest.mark.parametrize('field', ['approved_at', 'expires_at', 'original_deadline_at'])
def test_tail_rejects_naive_timestamps(tail_case, field):
    root, source, path, payload = tail_case
    original = payload['original_deadline_at']
    payload[field] = pd.Timestamp(payload[field]).tz_localize(None).isoformat()
    save_json(path, payload)
    with pytest.raises(ValueError, match='timezone aware'):
        preparation_deadline(root, source, original, AFTER_CLOSE, path)


@pytest.mark.parametrize('damage', ['original-session', 'recovery-at-opening', 'recovery-at-close'])
def test_recovery_tail_must_retain_session_and_recovery_deadlines(tmp_path, damage):
    source = tmp_path / 'ml/nightly-gameplan-runs/frozen'
    source.mkdir(parents=True)
    save_json(source / 'receipt.json', {'action_date': '2026-09-14'})
    original = '2026-09-14T18:20:00Z'
    session_deadline = ORIGINAL
    if damage == 'original-session':
        session_deadline = '2026-09-14T11:01:00Z'
    else:
        original = ORIGINAL if damage == 'recovery-at-opening' else '2026-09-15T00:00:00Z'
    payload = {
        'schema_version': RECOVERY_VERSION,
        'approved_at': '2026-09-14T18:21:00Z',
        'expires_at': '2026-09-14T19:21:00Z',
        'original_session_deadline_at': session_deadline,
    }
    path = tmp_path / 'continuation.json'
    save_json(path, payload)
    with pytest.raises(ValueError, match='preserve both original deadlines'):
        preparation_deadline(tmp_path, source, original, AFTER_CLOSE, path)


@pytest.fixture
def frozen_gameplan(recovery_case):
    root, path, payload = recovery_case
    source = root / 'ml/nightly-gameplan-runs/frozen'
    source.mkdir(parents=True)
    (source / 'plan.json').write_text('{"symbols":[]}', encoding='utf-8')
    evidence = verify_recovery(root, path, REQUESTED)
    manifest = write_manifest(source, run_timestamp=REQUESTED, input_files=[],
                              output_files=['plan.json'], configuration={'late_preparation': evidence})
    save_json(source / 'receipt.json', {
        'action_date': payload['action_date'],
        'manifest_checksum_sha256': file_checksum(manifest),
    })
    return root, source, path, payload, evidence


def test_frozen_gameplan_uses_identical_recovery_after_close(frozen_gameplan):
    root, source, path, payload, evidence = frozen_gameplan
    originals = {p: p.read_bytes() for p in [path, *source.iterdir()]}
    deadline, actual = preparation_deadline(root, source, ORIGINAL, AFTER_CLOSE, path)
    assert deadline == pd.Timestamp(payload['expires_at'])
    assert actual == evidence
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize('damage', [
    'manifest-evidence', 'manifest-checksum', 'output-bytes', 'recovery-bytes',
    'receipt-session', 'original-deadline',
])
def test_frozen_gameplan_retains_manifest_and_checksum_binding(frozen_gameplan, damage):
    root, source, path, payload, _ = frozen_gameplan
    original = ORIGINAL
    if damage == 'manifest-evidence':
        manifest_path = source / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['configuration']['late_preparation']['authorization']['authorization_reason'] = 'Changed'
        save_json(manifest_path, manifest)
        receipt = json.loads((source / 'receipt.json').read_text(encoding='utf-8'))
        receipt['manifest_checksum_sha256'] = file_checksum(manifest_path)
        save_json(source / 'receipt.json', receipt)
    elif damage in {'manifest-checksum', 'receipt-session'}:
        receipt = json.loads((source / 'receipt.json').read_text(encoding='utf-8'))
        receipt['manifest_checksum_sha256' if damage == 'manifest-checksum' else 'action_date'] = (
            'changed' if damage == 'manifest-checksum' else '2026-09-15')
        save_json(source / 'receipt.json', receipt)
    elif damage == 'output-bytes':
        (source / 'plan.json').write_text('{"symbols":{}}', encoding='utf-8')
    elif damage == 'recovery-bytes':
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    else:
        original = '2026-09-14T11:01:00Z'
    with pytest.raises((ValueError, RuntimeError)):
        preparation_deadline(root, source, original, AFTER_CLOSE, path)
