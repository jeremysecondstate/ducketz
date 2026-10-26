"""Offline final-scoring verification against exactly one candidate checkout."""
from pathlib import Path
import argparse
import hashlib
import importlib
import json
import socket
import sys


def isolate(candidate):
    original_roots, retained_finders = set(), []
    for finder in sys.meta_path:
        name = getattr(finder, '__module__', '')
        if name.startswith('__editable___ducketz_') and name.endswith('_finder'):
            mapping = getattr(sys.modules.get(name), 'MAPPING', {})
            original_roots.update(Path(mapping[p]).resolve().parent
                                  for p in ('app','datafetching','ml','technicals') if p in mapping)
        else:
            retained_finders.append(finder)
    sys.meta_path[:] = retained_finders
    original_roots.discard(candidate)
    paths = []
    for entry in sys.path:
        path = Path(entry or '.').resolve()
        collision = (path.name == 'cross_pc' and path.parent.name == 'tools'
                     and (path/'test_runner.py').is_file() and (path/'requests.py').is_file())
        if path not in original_roots | {candidate} and not collision:
            paths.append(entry)
    sys.path[:] = [str(candidate), *paths]
    return original_roots


def assert_candidate(candidate, originals):
    for name in ('ml.intraday.store','ml.intraday.final_scoring'):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(candidate):
            raise RuntimeError('Final scorer imported from a different checkout')
    for name, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if (name.split('.')[0] in {'ml','app','datafetching','technicals','tests'}
                and not path.is_relative_to(candidate)):
            raise RuntimeError('Application or fixture import escaped candidate')
        if (not path.is_relative_to(candidate) and '.venv' not in path.parts
                and any(path.is_relative_to(root) for root in originals)):
            raise RuntimeError('Application import escaped candidate')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--focused', action='store_true')
    parser.add_argument('--basetemp', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    originals = isolate(root)
    def denied(*args, **kwargs):
        raise RuntimeError('Final-scoring checks forbid service and subprocess calls')
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = socket.create_connection = denied
    def offline(event, args):
        if event in {'socket.connect','socket.sendto','socket.getaddrinfo','socket.gethostbyname',
                     'socket.gethostbyaddr','subprocess.Popen','os.system','os.spawn','os.posix_spawn'}:
            denied()
    sys.addaudithook(offline)
    assert_candidate(root, originals)
    import pytest
    targets = ['tests/test_intraday_final_scoring_indexed.py']
    if not args.focused:
        targets += ['tests/test_intraday_final_scoring.py','tests/test_intraday_final_scoring_concurrency.py',
                    'tests/test_intraday_scoring_revision_cache.py','tests/test_intraday_retained_target_priority.py',
                    'tests/test_intraday_store.py','tests/test_intraday_live_session.py',
                    'tests/test_intraday_execution_forecast_projection.py','tests/test_intraday_execution_revision_cache.py',
                    'tests/test_intraday_durable_forecast_headers.py']
    options = ['-q','-p','no:cacheprovider',*[str(root/p)for p in targets]]
    if args.basetemp:
        options += ['--basetemp',str(args.basetemp)]
    result = pytest.main(options)
    assert_candidate(root, originals)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module,'__file__',None)
        if filename:
            source = Path(filename).resolve()
            if source.is_relative_to(root) and source.suffix == '.py' and '.venv' not in source.parts:
                closure[source.relative_to(root).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    for p in ['pyproject.toml','tests/conftest.py','tests/check_isolated_intraday_final_scoring.py']:
        closure[p] = hashlib.sha256((root/p).read_bytes()).hexdigest()
    print('INTRADAY_FINAL_SCORING_IMPORT_CLOSURE='+json.dumps(closure,sort_keys=True))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
