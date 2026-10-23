"""Run initial-state refresh regressions offline against this exact candidate."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import socket
import sys
import uuid


def _deny_network(*args, **kwargs):
    raise RuntimeError('Network connections are disabled in Gameplan refresh regressions')


def _isolate(candidate):
    originals, retained = set(), []
    for finder in sys.meta_path:
        name = getattr(finder, '__module__', '')
        if name.startswith('__editable___ducketz_') and name.endswith('_finder'):
            mapping = getattr(sys.modules.get(name), 'MAPPING', {})
            originals.update(Path(mapping[p]).resolve().parent for p in ('app', 'datafetching', 'ml') if p in mapping)
        else:
            retained.append(finder)
    sys.meta_path[:] = retained
    originals.discard(candidate)
    paths = []
    for entry in sys.path:
        path = Path(entry or '.').resolve()
        collision = (path.name == 'cross_pc' and path.parent.name == 'tools'
                     and (path / 'test_runner.py').is_file() and (path / 'requests.py').is_file())
        if path not in originals | {candidate} and not collision:
            paths.append(entry)
    sys.path[:] = [str(candidate), *paths]
    return originals


def _assert_imports(candidate, originals):
    for name in ('ml.account_gameplan.execution', 'ml.account_gameplan.authority',
                 'ml.stock_trader.independent_runtime', 'ml.stock_trader.independent_session'):
        if not Path(importlib.import_module(name).__file__).resolve().is_relative_to(candidate):
            raise RuntimeError('Gameplan module imported outside this candidate: ' + name)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if filename:
            source = Path(filename).resolve()
            if (name.split('.')[0] in {'ml', 'app', 'datafetching', 'tests'} or name.startswith('test_')
                    or name == 'conftest') and not source.is_relative_to(candidate):
                raise RuntimeError('Application or fixture import escaped this candidate: ' + str(source))
            if ('.venv' not in source.parts and not source.is_relative_to(candidate)
                    and any(source.is_relative_to(root) for root in originals)):
                raise RuntimeError('Application source escaped this candidate: ' + str(source))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--basetemp', type=Path)
    parser.add_argument('--focused', action='store_true')
    parser.add_argument('--context-only', action='store_true')
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    originals = _isolate(candidate)
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = _deny_network
    socket.socket.sendto = _deny_network
    def offline_boundary(event, args):
        if event in {'socket.connect', 'socket.sendto', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.bind', 'subprocess.Popen', 'os.system', 'os.spawn', 'os.posix_spawn'}:
            raise RuntimeError('Gameplan refresh checks prohibit network/subprocess actions')
    sys.addaudithook(offline_boundary)
    _assert_imports(candidate, originals)
    import pytest
    suites = ['tests/test_gameplan_pre_context_refresh.py', 'tests/test_gameplan_order_evidence_refresh.py',
              'tests/test_gameplan_pre_context_lease_refresh.py']
    if not args.context_only:
        suites += ['tests/test_gameplan_initial_state_refresh.py', 'tests/test_gameplan_pre_reservation_refresh.py']
    if not args.focused and not args.context_only:
        suites += ['tests/test_gameplan_batch_refresh.py', 'tests/test_account_gameplan_execution.py',
                   'tests/test_account_gameplan_authority.py', 'tests/test_gameplan_order_snapshot_coherence.py',
                   'tests/test_independent_stock_runtime.py',
                   'tests/test_gameplan_direction_runtime.py', 'tests/test_gameplan_selftrade_status.py']
    basetemp = (args.basetemp or candidate / 'scratch/gameplan-initial-refresh-tests' / uuid.uuid4().hex).resolve()
    scratch = (candidate / 'scratch').resolve()
    if basetemp == scratch or not basetemp.is_relative_to(scratch):
        raise RuntimeError('Gameplan refresh fixtures require candidate-owned scratch')
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    arguments = ['-q', '-p', 'no:cacheprovider', *[str(candidate / p) for p in suites],
                 '--basetemp', str(basetemp)]
    result = pytest.main(arguments)
    _assert_imports(candidate, originals)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename:
            source = Path(filename).resolve()
            if source.is_relative_to(candidate) and source.suffix == '.py' and '.venv' not in source.parts:
                closure[source.relative_to(candidate).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ('pyproject.toml', 'tests/conftest.py', 'tests/check_isolated_gameplan_initial_state_refresh.py', *suites):
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    print('GAMEPLAN_INITIAL_REFRESH_IMPORT_CLOSURE=' + json.dumps(closure, sort_keys=True))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
