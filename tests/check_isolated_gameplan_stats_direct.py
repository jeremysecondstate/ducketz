"""Run native direct Stats and v1 regressions on this exact offline candidate."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))


def offline(event, args):
    if event in {'socket.connect', 'socket.sendto', 'subprocess.Popen', 'os.system', 'os.startfile'}:
        raise RuntimeError('Direct Stats isolated checks forbid network and external processes')


sys.addaudithook(offline)
import pytest

with tempfile.TemporaryDirectory(prefix='ducketz-stats-observations-') as temporary:
    temporary = Path(temporary).resolve()
    assert temporary.is_relative_to(Path(tempfile.gettempdir()).resolve())
    code = pytest.main([str(ROOT/'tests/test_gameplan_stats_direct_observations.py'),
        str(ROOT/'tests/test_gameplan_stats_outcome_revision.py'), '-q', '-p', 'no:cacheprovider',
        '--basetemp', str(temporary/'pytest')])

imports = {}
for name, module in tuple(sys.modules.items()):
    raw = getattr(module, '__file__', None)
    if not raw:
        continue
    path = Path(raw).resolve()
    if name.startswith(('ml.', 'app.', 'datafetching.', 'test_gameplan_stats_', 'test_direction_only_joint_plan')):
        assert path.is_relative_to(ROOT), (name, 'Imported code outside the isolated candidate')
    if path.is_relative_to(ROOT) and path.suffix == '.py' and path.is_file():
        imports[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(dict(status='PASS' if code == 0 else 'FAIL', imported_candidate_files=imports,
    network_calls=0, external_processes=0, trader_controls=0), sort_keys=True))
raise SystemExit(code)
