"""Run the bounded own-order/account recapture checks without external IO."""
from pathlib import Path
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
before = {str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest()
          for group in [ROOT/'app', ROOT/'ml', ROOT/'datafetching', ROOT/'tests', ROOT/'technicals', ROOT/'tools']
          if group.is_dir() for p in group.rglob('*.py')}
before.update({str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')})
started = datetime.now(timezone.utc).isoformat()
def deny(*args, **kwargs):
    raise RuntimeError('Offline recapture checks deny network and subprocess IO')
socket.socket.connect = deny
socket.create_connection = deny
subprocess.Popen = deny
import pytest
with tempfile.TemporaryDirectory(prefix='ducketz-order-recapture-') as temporary:
    result = pytest.main([str(ROOT/'tests'/name) for name in [
        'test_intraday_capture_order_recapture.py',
        'test_intraday_main_account_execution.py',
        'test_intraday_main_executor_graph.py',
        'test_intraday_main_ordinary_preflight.py']]
        + ['--basetemp', str(Path(temporary)/'pytest'), '-q', '-p', 'no:cacheprovider'])
closure = {}
for name, module in tuple(sys.modules.items()):
    path = getattr(module, '__file__', None)
    if not path:
        continue
    source = Path(path).resolve()
    if name.split('.', 1)[0] in {'app', 'ml', 'datafetching', 'technicals', 'tools'}:
        assert source.is_relative_to(ROOT), (name, str(source), 'project import escaped isolated tree')
    if source.suffix == '.py' and source.is_relative_to(ROOT):
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        assert str(source) in before, (str(source), 'unbound imported source')
        assert before[str(source)] == actual, (str(source), 'source changed during checks')
        closure[source.relative_to(ROOT).as_posix()] = actual
assert Path(sys.modules['ml.intraday.account_execution'].__file__).resolve() == ROOT/'ml/intraday/account_execution.py'
print('INTRADAY_ORDER_RECAPTURE_CHECK='+json.dumps(dict(started_at=started,
    finished_at=datetime.now(timezone.utc).isoformat(),exit_code=int(result),
    network_subprocess_denied=True,all_imported_bytes_unchanged=True,fingerprints=closure),sort_keys=True))
raise SystemExit(result)
