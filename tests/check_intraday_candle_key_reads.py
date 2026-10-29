"""Run confined offline candle-read and exact integration-hunk regressions."""
from pathlib import Path
import hashlib
import json
import os
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    # The tests load the candidate helper by absolute file path. Editable
    # application finders and unrelated operating conftests are never used.
    sys.meta_path[:] = [f for f in sys.meta_path
                       if not getattr(f, '__module__', '').startswith('__editable___ducketz_')]
    attempts = []

    def boundary(event, args):
        if event in {'socket.connect', 'socket.sendto', 'socket.getaddrinfo',
                     'subprocess.Popen', 'os.system', 'os.spawn', 'os.posix_spawn'}:
            attempts.append(event)
            raise RuntimeError('Candle-read regressions prohibit external operations')
        if event == 'open' and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0]).replace('\\', '/').lower()
            if name.startswith(('c:/datastore/', 'i:/shared drives/')) or name.endswith('/.env'):
                attempts.append('private-open')
                raise RuntimeError('Candle-read regressions prohibit private operating inputs')

    sys.addaudithook(boundary)
    (root / 'scratch/checks').mkdir(parents=True, exist_ok=True)
    import pytest
    result = pytest.main(['-q', '--noconftest', '-p', 'no:cacheprovider',
                         '--rootdir', str(root), '--basetemp',
                         str(root / 'scratch/checks/candle-key-reads'),
                         str(root / 'tests/test_intraday_candle_key_reads.py')])
    names = ['ml/intraday/candle_reads.py', 'tests/test_intraday_candle_key_reads.py',
             'tests/check_intraday_candle_key_reads.py',
             'docs/development/patches/candle-key-reads.patch']
    print('CANDLE_KEY_READ_CHECK=' + json.dumps({
        'exit_code': int(result), 'external_attempts': attempts,
        'fingerprints': {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names},
        'scope': 'Real SQLite synthetic reads and exact published method hunk; full installed source checked separately.'
    }, sort_keys=True))
    return int(result) if not attempts else 1


if __name__ == '__main__':
    raise SystemExit(main())
