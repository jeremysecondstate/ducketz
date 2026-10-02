"""Synthetic pytest lifecycle checks; never import application modules or read real .env."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROPOSED = Path(__file__).with_name("conftest.py")


@pytest.mark.parametrize("mode", ["existing_override", "absent_override", "test_failure", "default_unchanged", "missing", "directory"])
def test_watchlist_binding_and_restore_through_real_pytest_lifecycle(tmp_path, mode):
    root = tmp_path / "candidate"
    tests = root / "tests"
    data = root / "datafetching"
    tests.mkdir(parents=True)
    data.mkdir()
    (tests / "conftest.py").write_bytes(PROPOSED.read_bytes())
    if mode == "directory":
        (data / "watchlist.txt").mkdir()
    elif mode != "missing":
        (data / "watchlist.txt").write_text("AAA\n")
    (data / "watchlist.local.txt").write_text("BBB\n")
    ambient = tmp_path / "ambient.txt"
    ambient.write_text("CCC\n")
    expected = str(ambient) if mode == "default_unchanged" else str(data / "watchlist.txt")
    (tests / "test_collection.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "BOUND_DURING_COLLECTION = os.environ.get('DUCKETS_PRODUCTION_WATCHLIST')\n"
        "Path('collection-marker').write_text('collected')\n"
        "def test_input():\n"
        f"    assert BOUND_DURING_COLLECTION == {expected!r}\n"
        + ("    assert False, 'synthetic test failure'\n" if mode == "test_failure" else "")
    )
    expected_code = 4 if mode in {"missing", "directory"} else 1 if mode == "test_failure" else 0
    enabled = mode != "default_unchanged"
    original = None if mode == "absent_override" else str(ambient)
    script = "\n".join([
        "import os, pytest",
        f"original = {original!r}",
        "if original is None: os.environ.pop('DUCKETS_PRODUCTION_WATCHLIST', None)",
        "else: os.environ['DUCKETS_PRODUCTION_WATCHLIST'] = original",
        f"code = pytest.main({['-q', '-p', 'no:cacheprovider', *(['--cross-pc-shared-watchlist'] if enabled else []), 'tests']!r})",
        f"assert code == {expected_code}, code",
        "assert os.environ.get('DUCKETS_PRODUCTION_WATCHLIST') == original",
    ])
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(PYTHONSAFEPATH="1", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "collection-marker").exists() == (mode not in {"missing", "directory"})
