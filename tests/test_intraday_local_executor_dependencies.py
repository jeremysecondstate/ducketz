"""Offline dependency isolation and original retry semantics for local effects."""
import builtins
from datetime import datetime, timezone
import importlib.util
from types import SimpleNamespace

import pytest

from ml.intraday import scout_local_execution as execution
from ml.intraday.exact_status import retry_delay
from ml.intraday.prediction import instant


LEGACY = frozenset({
    'ml.intraday.cancellation', 'ml.intraday.scout_books',
    'ml.intraday.scout_execution', 'ml.intraday.scout_runtime_gate',
    'ml.intraday.scout_prestart', 'ml.intraday.scout_authority',
    'ml.intraday.scout_accounting', 'ml.intraday.admission_service',
    'ml.intraday.admission_transport', 'ml.intraday.runtime_compatibility',
})


def block_legacy_imports(monkeypatch):
    original = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = importlib.util.resolve_name('.' * level + name, globals['__package__']) if level else name
        requested = {resolved, *(resolved + '.' + item for item in (fromlist or ()))}
        assert not requested.intersection(LEGACY), 'Local effects imported a legacy authority dependency'
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', guarded)


def test_executor_direct_imports_without_legacy_authority_modules(monkeypatch):
    # Re-execute the caller under the guard; dependency modules may be cached.
    # The separate sealed checks bind every transitive source import by hash.
    block_legacy_imports(monkeypatch)
    spec = importlib.util.spec_from_file_location('ml.intraday._local_executor_dependency_probe', execution.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ScoutLocalAccountExecutor is not None


@pytest.mark.parametrize('value, expected', [
    (None, 3.0), ('0', 3.0), ('-5', 3.0), ('2.9', 3.0),
    ('3', 3.0), ('12.5', 12.5), ('nan', 3.0), ('inf', 3.0),
    ('invalid', 3.0), ('Mon, 12 Oct 2026 17:15:20 GMT', 15.0),
    ('Mon, 12 Oct 2026 17:14:00 GMT', 3.0),
])
@pytest.mark.parametrize('clock', [instant('2026-10-12T17:15:05Z'), '2026-10-12T17:15:05Z'])
def test_retry_after_header_retains_original_floor_and_dates(value, expected, clock):
    error = SimpleNamespace(response=SimpleNamespace(headers={'Retry-After': value}))
    assert retry_delay(error, clock) == expected


def test_explicit_retry_after_precedes_header():
    error = SimpleNamespace(retry_after=8, response=SimpleNamespace(headers={'Retry-After': '20'}))
    assert retry_delay(error, datetime(2026, 10, 12, tzinfo=timezone.utc)) == 8


def test_absent_response_and_headers_use_original_floor():
    assert retry_delay(TimeoutError(), instant('2026-10-12T17:15:05Z')) == 3
    assert retry_delay(SimpleNamespace(response=SimpleNamespace(headers=None)), instant('2026-10-12T17:15:05Z')) == 3
