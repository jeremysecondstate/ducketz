"""Run exact-candidate order-view regressions without network or live state."""
from pathlib import Path
import argparse
import hashlib
import importlib
import json
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_isolated_gameplan_session import _isolate_ducketz_imports


def _assert_imports(candidate, original):
    worker = importlib.import_module('ml.stock_trader.independent_session')
    if not Path(worker.__file__).resolve().is_relative_to(candidate):
        raise RuntimeError('Gameplan worker imported outside candidate')
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename:
            path = Path(filename).resolve()
            if (not path.is_relative_to(candidate) and '.venv' not in path.parts
                    and any(path.is_relative_to(root) for root in original)):
                raise RuntimeError('Ducketz application import escaped candidate: '+str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--basetemp', type=Path)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--focused', action='store_true')
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original = _isolate_ducketz_imports(candidate)
    def no_network(*args, **kwargs):
        raise RuntimeError('Network disabled for Gameplan order coherence checks')
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    _assert_imports(candidate, original)
    import pytest
    files = ['test_gameplan_order_snapshot_coherence.py']
    if not (args.focused or args.baseline):
        files += ['test_account_gameplan_authority.py','test_account_gameplan_execution.py',
                  'test_gameplan_authority_capacity.py','test_stock_horizon_broker.py',
                  'test_oct19_direction_execution.py']
    arguments = ['-q','-p','no:cacheprovider',*[str(candidate/'tests'/name) for name in files]]
    if args.baseline:
        arguments += ['-k','recaptures_complete_pair']
    if args.basetemp:
        arguments += ['--basetemp',str(args.basetemp)]
    result = pytest.main(arguments)
    _assert_imports(candidate, original)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module,'__file__',None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(candidate) and path.suffix == '.py' and '.venv' not in path.parts:
                closure[path.relative_to(candidate).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ['pyproject.toml','datafetching/watchlist.txt','tests/conftest.py',
                 'tests/check_isolated_gameplan_order_coherence.py']:
        closure[name] = hashlib.sha256((candidate/name).read_bytes()).hexdigest()
    print('GAMEPLAN_ORDER_COHERENCE_IMPORT_CLOSURE='+json.dumps(closure,sort_keys=True))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
