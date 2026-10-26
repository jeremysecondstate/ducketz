"""Offline callback contracts for a single immutable receipt join."""
import pytest

from ml.intraday.market_join_receipts import ReceiptJoinProofs


def fixture_proofs():
    calls, identities = [], []
    revisions = {'first': 1, 'second': 1}

    def verify(value):
        calls.append(value)
        return {'verified': value}

    def identity(value):
        identities.append(value)
        return (value, revisions[value])

    return ReceiptJoinProofs(verify, identity, (OSError, ValueError)), calls, identities, revisions


def test_repeated_tuple_verified_once_then_reverified_at_final_fence():
    proofs, calls, identities, _ = fixture_proofs()
    results = [proofs.verify('key', 'first', uses=100) for _ in range(100)]
    assert results == [{'verified': 'first'}] * 100
    assert calls == ['first']
    assert proofs.fences_unchanged()
    assert calls == ['first', 'first']
    assert identities == ['first'] * 4


@pytest.mark.parametrize('uses', [1, 2])
def test_singleton_and_doubleton_use_original_verifier_without_identity_work(uses):
    proofs, calls, identities, _ = fixture_proofs()
    for _ in range(uses):
        assert proofs.verify('key', 'first', uses=uses) == {'verified': 'first'}
    assert proofs.fences_unchanged()
    assert calls == ['first'] * uses
    assert identities == []


def test_distinct_catalog_keys_do_not_share_a_receipt_proof():
    calls = []

    def verify(value):
        calls.append(value)
        if value['features'] != {'last': 10}:
            raise ValueError('catalog differs from envelope')
        return value

    proofs = ReceiptJoinProofs(verify, lambda value: (value['id'], 1), (ValueError,))
    good = {'id': 'same-envelope', 'features': {'last': 10}}
    bad = {'id': 'same-envelope', 'features': {'last': 999}}
    assert proofs.verify('catalog-row-1', good, uses=3) == good
    with pytest.raises(ValueError, match='catalog differs'):
        proofs.verify('catalog-row-2', bad, uses=3)
    assert len(calls) == 2


def test_failed_verification_is_not_cached_and_later_row_can_recover():
    calls = []

    def verify(value):
        calls.append(value)
        if len(calls) == 1:
            raise OSError('temporary read')
        return value

    proofs = ReceiptJoinProofs(verify, lambda value: (value, 1), (OSError,))
    with pytest.raises(OSError):
        proofs.verify('key', 'first', uses=3)
    assert proofs.verify('key', 'first', uses=3) == 'first'
    assert proofs.verify('key', 'first', uses=3) == 'first'
    assert proofs.fences_unchanged()
    assert len(calls) == 3


@pytest.mark.parametrize('failed_identity_call', [1, 2])
def test_initial_identity_error_requests_fallback_without_skipping_verifier(failed_identity_call):
    counts = {'identity': 0, 'verify': 0}

    def identity(value):
        counts['identity'] += 1
        if counts['identity'] == failed_identity_call:
            raise OSError('metadata unavailable')
        return 1

    def verify(value):
        counts['verify'] += 1
        return value

    proofs = ReceiptJoinProofs(verify, identity, (OSError,))
    assert proofs.verify('key', 'first', uses=3) == 'first'
    assert not proofs.fences_unchanged()
    assert counts['verify'] == 1


def test_change_during_initial_verification_requests_bounded_fallback():
    revision = [1]

    def verify(value):
        revision[0] += 1
        return value

    proofs = ReceiptJoinProofs(verify, lambda value: revision[0], (OSError,))
    assert proofs.verify('key', 'first', uses=3) == 'first'
    assert not proofs.fences_unchanged()


def test_changed_final_identity_rejects_reused_proof_without_new_retry():
    proofs, calls, _, revisions = fixture_proofs()
    proofs.verify('key', 'first', uses=10)
    revisions['first'] = 2
    assert not proofs.fences_unchanged()
    assert calls == ['first']


def test_change_during_final_reverification_is_rejected():
    revision, calls = [1], []

    def verify(value):
        calls.append(value)
        if len(calls) == 2:
            revision[0] += 1
        return value

    proofs = ReceiptJoinProofs(verify, lambda value: revision[0], (OSError,))
    proofs.verify('key', 'first', uses=10)
    assert not proofs.fences_unchanged()
    assert len(calls) == 2


@pytest.mark.parametrize('error', [OSError('deleted'), ValueError('corrupt')])
def test_expected_final_reverification_error_requests_fallback(error):
    calls = []

    def verify(value):
        calls.append(value)
        if len(calls) == 2:
            raise error
        return value

    proofs = ReceiptJoinProofs(verify, lambda value: 1, (OSError, ValueError))
    proofs.verify('key', 'first', uses=3)
    assert not proofs.fences_unchanged()
    assert len(calls) == 2


@pytest.mark.parametrize('during_final', [False, True])
def test_unexpected_verifier_exception_propagates(during_final):
    calls = []

    def verify(value):
        calls.append(value)
        if len(calls) == (2 if during_final else 1):
            raise RuntimeError('programming failure')
        return value

    proofs = ReceiptJoinProofs(verify, lambda value: 1, (OSError, ValueError))
    if during_final:
        proofs.verify('key', 'first', uses=3)
    with pytest.raises(RuntimeError, match='programming failure'):
        proofs.fences_unchanged() if during_final else proofs.verify('key', 'first', uses=3)


def test_proofs_never_carry_between_joins():
    proofs, calls, _, _ = fixture_proofs()
    proofs.verify('key', 'first', uses=3)
    assert proofs.fences_unchanged()
    second = ReceiptJoinProofs(lambda value: calls.append(value) or value,
                               lambda value: 1, (OSError,))
    assert second.verify('key', 'first', uses=3) == 'first'
    assert len(calls) == 3
