"""Exercise the unchanged verifier as a real script with its collision present."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("violation", [None, "unbound", "original"])
def test_pinned_style_script_imports_external_requests_and_keeps_import_audit(tmp_path, violation):
    source = Path(__file__).resolve().parents[1]
    runner_dir = tmp_path / "release/tools/cross_pc"
    runner_dir.mkdir(parents=True)
    runner = runner_dir / "test_runner.py"
    # Copy the candidate's fingerprinted, unchanged verifier to a pinned-style
    # script location. No installed release or live checkout is modified.
    runner.write_bytes((source / "tools/cross_pc/test_runner.py").read_bytes())
    (runner_dir / "requests.py").write_text("raise RuntimeError('internal helper shadowed requests')\n")
    candidate, original = tmp_path / "candidate", tmp_path / "original"
    tests = candidate / "tests"
    tests.mkdir(parents=True)
    original.mkdir()
    (tests / "conftest.py").write_bytes((source / "tests/conftest.py").read_bytes())
    package = candidate / "ml"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "probe.py").write_text("VALUE = 'candidate'\n")
    foreign = original / "foreign_probe.py"
    foreign.write_text("VALUE = 'original'\n")
    foreign_import = ""
    if violation == "original":
        foreign_import = (
            "    spec = importlib.util.spec_from_file_location('foreign_probe', " + repr(str(foreign)) + ")\n"
            "    module = importlib.util.module_from_spec(spec)\n"
            "    sys.modules['foreign_probe'] = module\n"
            "    spec.loader.exec_module(module)\n"
            "    assert module.VALUE == 'original'\n"
        )
    (tests / "test_probe.py").write_text(
        "import importlib.metadata\nimport importlib.util\nfrom pathlib import Path\n"
        "import socket\nimport sys\nimport pytest\nimport requests\nfrom ml import probe\n\n"
        "def test_external_package_and_candidate_import():\n"
        "    expected = importlib.metadata.distribution('requests').locate_file('requests/__init__.py')\n"
        "    assert Path(requests.__file__).resolve() == Path(expected).resolve()\n"
        "    assert probe.VALUE == 'candidate'\n"
        "    with pytest.raises(RuntimeError, match='network disabled for cross-PC offline checks'):\n"
        "        socket.create_connection(('example.invalid', 443))\n" + foreign_import
    )
    expected = {path.relative_to(candidate).as_posix(): sha256(path.read_bytes()).hexdigest()
                for path in candidate.rglob("*.py")}
    if violation == "unbound":
        expected.pop("ml/probe.py")
    result_path = tmp_path / "imports.json"
    specification = tmp_path / "runner.json"
    specification.write_text(json.dumps({
        "root": str(candidate), "original_root": str(original), "fingerprints": expected,
        "argv": [sys.executable, "-B", "-m", "pytest", "tests/test_probe.py", "-q", "-p", "no:cacheprovider"],
        "result_path": str(result_path),
    }))
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run([sys.executable, "-B", str(runner), str(specification)],
                               cwd=candidate, env=environment, capture_output=True, text=True, timeout=60)
    evidence = json.loads(result_path.read_text())
    assert evidence["test_exit_code"] == 0, completed.stdout + completed.stderr
    assert evidence["imported_fingerprints"]["tests/conftest.py"] == expected["tests/conftest.py"]
    if violation is None:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert evidence["violations"] == []
        assert evidence["imported_fingerprints"]["ml/probe.py"] == expected["ml/probe.py"]
    else:
        assert completed.returncode == 86
        wanted = ("unbound candidate dependency: ml/probe.py" if violation == "unbound"
                  else "import from original source checkout: foreign_probe.py")
        assert wanted in evidence["violations"]
