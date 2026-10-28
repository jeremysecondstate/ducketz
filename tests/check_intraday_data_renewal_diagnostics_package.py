"""Confined offline data-renewal diagnostic fixtures for a reviewed candidate."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import uuid


def main():
    candidate = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    original_roots = set()
    finders = []
    for finder in sys.meta_path:
        name = getattr(finder, '__module__', '')
        if name.startswith('__editable___ducketz_') and name.endswith('_finder'):
            mapping = getattr(sys.modules.get(name), 'MAPPING', {})
            original_roots.update(Path(p).resolve().parent for p in mapping.values())
        else:
            finders.append(finder)
    sys.meta_path[:] = finders
    original_roots.discard(candidate)
    paths = []
    for entry in sys.path:
        path = Path(entry or '.').resolve()
        collision = (path.name == 'cross_pc' and (path / 'test_runner.py').is_file()
                     and (path / 'requests.py').is_file())
        if path not in original_roots | {candidate} and not collision:
            paths.append(entry)
    sys.path[:] = [str(candidate), *paths]
    attempts = []

    def denied(*args, **kwargs):
        attempts.append('network')
        raise RuntimeError('Data-renewal diagnostic fixtures are offline')

    socket.create_connection = denied
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = denied

    def boundary(event, args):
        if event in {'socket.connect', 'socket.sendto', 'socket.getaddrinfo',
                     'socket.gethostbyname', 'socket.gethostbyaddr', 'subprocess.Popen',
                     'os.system', 'os.spawn', 'os.posix_spawn'}:
            attempts.append(event)
            raise RuntimeError('Data-renewal diagnostic fixtures prohibit external operations')

    sys.addaudithook(boundary)
    scratch = candidate / 'scratch' / 'checks'
    scratch.mkdir(parents=True, exist_ok=True)
    basetemp = scratch / ('data-renewal-diagnostics-' + uuid.uuid4().hex)
    import pytest
    result = pytest.main(['-q', '--noconftest', '-p', 'no:cacheprovider',
                          '--rootdir', str(candidate), '--basetemp', str(basetemp),
                          str(candidate / 'tests/test_intraday_data_renewal_diagnostics.py')])
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(candidate) and path.suffix == '.py' and '.venv' not in path.parts:
            closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if '.venv' not in path.parts and any(path.is_relative_to(root) for root in original_roots):
            raise RuntimeError('Application import escaped the candidate')
    for name in ('tools/__init__.py', 'tools/intraday_data_renewal.py',
                 'tests/test_intraday_data_renewal_diagnostics.py',
                 'tests/check_intraday_data_renewal_diagnostics_package.py'):
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    print('DATA_RENEWAL_DIAGNOSTIC_CHECK=' + json.dumps(dict(
        exit_code=result, network_attempts=len(attempts), import_closure=closure,
        scope='Fake data-only processes; no initial admission, runtime operation or error retry'),
        sort_keys=True))
    if attempts:
        raise RuntimeError('Forbidden external operation attempted')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
