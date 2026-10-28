"""Execute the Windows wrapper with a recording fake interpreter, never a trader."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.scout_service_install import manual_launcher_source


@pytest.mark.skipif(os.name != "nt", reason="Native Windows batch argument semantics")
@pytest.mark.parametrize("arguments,expected", [
    ([], ["--live", "--hold-console"]),
    (["--check"], ["--check"]),
    (["--stop"], ["--stop"]),
    (["--live", "--unknown-recipe-option"], ["--live", "--unknown-recipe-option"]),
])
def test_manual_entrypoint_preserves_explicit_control_mode(tmp_path, arguments, expected):
    # The fake bootstrap only records argv; no application/provider imports.
    bootstrap = tmp_path / "path with spaces" / "launch.py"
    bootstrap.parent.mkdir()
    bootstrap.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\nraise SystemExit(17)\n")
    wrapper = tmp_path / "Start Trader.cmd"
    wrapper.write_bytes(manual_launcher_source(python_executable=sys.executable, bootstrap=bootstrap))
    result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(wrapper), *arguments],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 17, result.stderr
    assert json.loads(result.stdout.strip()) == expected


@pytest.mark.parametrize("value", ['bad\npath', 'bad"path', 'bad%path', 'bad!path'])
def test_launcher_rejects_expandable_bindings(value):
    with pytest.raises(ValueError, match="Unsafe"):
        manual_launcher_source(python_executable=value, bootstrap="launch.py")
