"""Offline fixtures for sealed code/dependency selection, with no trader calls."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import threading

import pytest


_PATH = Path(__file__).resolve().parents[1] / "tools/trader_installation.py"
_SPEC = importlib.util.spec_from_file_location("trader_installation_under_test", _PATH)
installation = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installation)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write(path, raw=b"example\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_bytes(raw)
    return path


@pytest.fixture
def fixture(tmp_path):
    source, python, packages = (tmp_path / name for name in ("application", "python", "packages"))
    source_files = {"tools/trader_installation.py": b"# installer v1\n",
                    "tools/trader_worker.py": b"# worker v1\n",
                    "tools/trader_session_host.py": b"# host v1\n",
                    "ml/trader_renewal.py": b"# continuation protocol v1\n",
                    "ml/__init__.py": b"",
                    "ml/behavior.py": b"VALUE = 1\n"}
    for name, raw in source_files.items():
        write(source / name, raw)
    write(python / "python.exe", b"offline-inert-interpreter")
    write(python / "python313.dll", b"offline-inert-runtime")
    write(python / "Lib/encodings/__init__.py", b"# encodings\n")
    write(python / "Lib/site-packages/not_selected.py", b"original-base-package")
    write(packages / "sample_dep/__init__.py", b"VALUE = 1\n")
    write(packages / "__editable___ducketz_finder.py", b"raise RuntimeError('mutable path')\n")
    write(packages / "__editable__.ducketz.pth", b"mutable-path\n")
    write(packages / "sample_dep/__pycache__/cached.pyc", b"cache")
    root = tmp_path / "sealed"
    args = dict(source_root=source, source_inventory={name: digest(raw) for name, raw in source_files.items()},
                python_root=python, site_packages=packages, reviewed=True)
    yield root, args
    # Sealed files intentionally reject ordinary writes. Fixture cleanup owns only
    # this temporary tree and restores writable bits for pytest on Windows.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)


def test_seal_selects_exact_source_and_complete_frozen_runtime(fixture):
    root, args = fixture
    selected = installation.seal(root, **args)
    assert installation.read_current(root) == selected
    assert Path(selected["python"]).read_bytes() == b"offline-inert-interpreter"
    assert Path(selected["site_packages"]).joinpath("sample_dep/__init__.py").read_bytes() == b"VALUE = 1\n"
    assert not list(root.rglob("__editable__*"))
    assert not list(root.rglob("*.pyc"))
    assert not list(root.rglob("not_selected.py"))
    assert selected["protocol"] == 1
    assert selected["manifest"]["source_files"] == args["source_inventory"]


def test_source_change_reuses_runtime_and_changes_next_selected_behavior(fixture):
    root, args = fixture
    first = installation.seal(root, **args)
    write(args["source_root"] / "ml/behavior.py", b"VALUE = 2\n")
    args["source_inventory"]["ml/behavior.py"] = digest(b"VALUE = 2\n")
    second = installation.seal(root, **args)
    assert second["generation"] != first["generation"]
    assert second["runtime_id"] == first["runtime_id"]
    assert Path(first["source_root"]).joinpath("ml/behavior.py").read_bytes() == b"VALUE = 1\n"
    assert Path(second["source_root"]).joinpath("ml/behavior.py").read_bytes() == b"VALUE = 2\n"
    assert len(list((root / "runtimes").iterdir())) == 1


def test_changed_dependency_creates_new_complete_runtime(fixture):
    root, args = fixture
    first = installation.seal(root, **args)
    write(args["site_packages"] / "sample_dep/__init__.py", b"VALUE = 2\n")
    second = installation.seal(root, **args)
    assert first["runtime_id"] != second["runtime_id"]
    assert Path(first["site_packages"]).joinpath("sample_dep/__init__.py").read_bytes() == b"VALUE = 1\n"
    assert Path(second["site_packages"]).joinpath("sample_dep/__init__.py").read_bytes() == b"VALUE = 2\n"


def test_handoff_reads_old_complete_generation_during_staging(fixture, monkeypatch):
    root, args = fixture
    first = installation.seal(root, **args)
    write(args["source_root"] / "ml/behavior.py", b"VALUE = 2\n")
    args["source_inventory"]["ml/behavior.py"] = digest(b"VALUE = 2\n")
    entered, release = threading.Event(), threading.Event()
    original = installation._copy_checked

    def delayed(source, target, expected):
        if source.name == "behavior.py":
            entered.set()
            assert release.wait(10)
        return original(source, target, expected)

    monkeypatch.setattr(installation, "_copy_checked", delayed)
    result, errors = [], []

    def install():
        try:
            result.append(installation.seal(root, **args))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=install)
    thread.start()
    try:
        assert entered.wait(10)
        assert installation.read_current(root)["generation"] == first["generation"]
        with pytest.raises(installation.InstallationBusy):
            installation.seal(root, **args)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert installation.read_current(root)["generation"] == result[0]["generation"]


@pytest.mark.parametrize("phase", ["source_copy", "runtime_copy", "runtime_new_file", "runtime_changed_file"])
def test_unstable_or_failed_install_preserves_previous_pointer(fixture, monkeypatch, phase):
    root, args = fixture
    first = installation.seal(root, **args)
    write(args["source_root"] / "ml/behavior.py", b"VALUE = 2\n")
    args["source_inventory"]["ml/behavior.py"] = digest(b"VALUE = 2\n")
    write(args["site_packages"] / "sample_dep/__init__.py", b"VALUE = 2\n")
    original = installation._copy_checked
    injected = False

    def failure(source, target, expected):
        nonlocal injected
        original(source, target, expected)
        if injected:
            return
        if phase == "source_copy" and source.name == "behavior.py":
            injected = True
            write(source, b"VALUE = 3\n")
        elif phase != "source_copy" and "site-packages" in target.parts:
            injected = True
            if phase == "runtime_copy":
                raise OSError("simulated incomplete copy")
            elif phase == "runtime_new_file":
                write(args["site_packages"] / "new_dependency.py", b"new")
            else:
                write(source, b"changed")

    monkeypatch.setattr(installation, "_copy_checked", failure)
    with pytest.raises((OSError, installation.InstallationError)):
        installation.seal(root, **args)
    assert injected
    assert installation.read_current(root)["generation"] == first["generation"]


@pytest.mark.parametrize("name", [".env", "scratch/private.py", "data/account.json", "datafetching/watchlist.local.txt", "../escape.py", "C:/escape.py"])
def test_private_binding_and_escaping_inventory_rejected(fixture, name):
    root, args = fixture
    args["source_inventory"][name] = "0" * 64
    with pytest.raises(installation.InstallationError):
        installation.seal(root, **args)
    assert not (root / "active.json").exists()


def test_review_hash_and_worker_entrypoint_are_required(fixture):
    root, args = fixture
    args["reviewed"] = False
    with pytest.raises(installation.InstallationError, match="review"):
        installation.seal(root, **args)
    args["reviewed"] = True
    args["source_inventory"]["ml/behavior.py"] = "0" * 64
    with pytest.raises(installation.InstallationError, match="hash mismatch"):
        installation.seal(root, **args)
    args["source_inventory"].pop("tools/trader_worker.py")
    with pytest.raises(installation.InstallationError, match="include worker"):
        installation.seal(root, **args)


@pytest.mark.parametrize("part", ["source", "runtime", "manifest", "pointer", "worker", "interpreter"])
def test_invalid_installation_fails_closed(fixture, part):
    root, args = fixture
    selected = installation.seal(root, **args)
    if part == "source":
        write(Path(selected["source_root"]) / "ml/behavior.py", b"tampered")
    elif part == "runtime":
        write(Path(selected["site_packages"]) / "sample_dep/__init__.py", b"tampered")
    elif part == "manifest":
        write(Path(selected["source_root"]).parent / "manifest.json", b"{}")
    elif part == "pointer":
        write(root / "active.json", b'{"schema":"trader-installation-v1","generation":"../staging"}')
    elif part == "worker":
        write(Path(selected["source_root"]) / "tools/trader_worker.py", b"tampered")
    else:
        write(Path(selected["python"]), b"tampered")
    with pytest.raises(installation.InstallationError):
        installation.read_current(root)
    if part in {"source", "manifest", "pointer", "worker", "interpreter"}:
        with pytest.raises(installation.InstallationError):
            installation.read_current(root, full=False)


def test_controller_update_is_next_generation_and_does_not_mutate_previous(fixture):
    root, args = fixture
    first = installation.seal(root, **args)
    replacement = b"# worker/controller v2\n"
    write(args["source_root"] / "tools/trader_worker.py", replacement)
    args["source_inventory"]["tools/trader_worker.py"] = digest(replacement)
    second = installation.seal(root, **args)
    assert Path(second["source_root"]).joinpath("tools/trader_worker.py").read_bytes() == replacement
    assert Path(first["source_root"]).joinpath("tools/trader_worker.py").read_bytes() == b"# worker v1\n"
    assert installation.read_current(root, full=False)["generation"] == second["generation"]


def test_identical_seal_is_idempotent(fixture):
    root, args = fixture
    first = installation.seal(root, **args)
    assert installation.seal(root, **args)["generation"] == first["generation"]
    assert len(list((root / "generations").iterdir())) == 1


def test_partial_generation_without_published_pointer_is_unavailable(fixture):
    root, args = fixture
    write(root / "generations/.incomplete.staging/source/tools/trader_worker.py", b"partial")
    with pytest.raises(installation.InstallationError, match="Unreadable"):
        installation.read_current(root)


def test_unsupported_protocol_never_selects_worker(fixture):
    root, args = fixture
    installation.seal(root, **args)
    pointer = json.loads((root / "active.json").read_text())
    pointer["protocol"] = 2
    write(root / "active.json", json.dumps(pointer).encode())
    with pytest.raises(installation.InstallationError, match="pointer"):
        installation.read_current(root)


def test_source_case_collision_rejected(fixture):
    root, args = fixture
    args["source_inventory"]["ML/behavior.py"] = args["source_inventory"]["ml/behavior.py"]
    with pytest.raises(installation.InstallationError, match="Case-colliding"):
        installation.seal(root, **args)


def test_frozen_runtime_rejects_virtualenv_redirector(fixture):
    root, args = fixture
    write(args["python_root"] / "pyvenv.cfg", b"home = mutable_python")
    with pytest.raises(installation.InstallationError, match="base interpreter"):
        installation.seal(root, **args)


def test_missing_dependency_root_cannot_be_sealed_as_empty(fixture):
    root, args = fixture
    args["site_packages"] = args["site_packages"].parent / "missing-dependencies"
    with pytest.raises(installation.InstallationError, match="must all exist"):
        installation.seal(root, **args)


def test_missing_source_file_fails_even_lightweight_selection(fixture):
    root, args = fixture
    selected = installation.seal(root, **args)
    target = Path(selected["source_root"]) / "ml/behavior.py"
    target.chmod(target.stat().st_mode | stat.S_IWRITE)
    target.unlink()
    with pytest.raises(installation.InstallationError, match="Missing"):
        installation.read_current(root, full=False)


def test_stdlib_paths_include_only_frozen_archive_and_interpreter_paths(fixture):
    root, args = fixture
    write(args["python_root"] / "python313.zip", b"offline-inert-archive")
    selected = installation.seal(root, **args)
    runtime = Path(selected["runtime_root"])
    assert selected["stdlib_paths"] == [str(runtime / "python/python313.zip"),
        str(runtime / "python/Lib"), str(runtime / "python/DLLs"), str(runtime / "python")]


def test_deliberate_bytecode_only_dependencies_are_retained(fixture):
    root, args = fixture
    write(args["site_packages"] / "bytecode_only.pyc", b"deliberate distribution")
    write(args["site_packages"] / "sample_dep/__init__.pyc", b"redundant cache")
    selected = installation.seal(root, **args)
    packages = Path(selected["site_packages"])
    assert (packages / "bytecode_only.pyc").read_bytes() == b"deliberate distribution"
    assert not (packages / "sample_dep/__init__.pyc").exists()


def test_reparse_inputs_rejected_when_supported(fixture):
    root, args = fixture
    target = args["source_root"] / "ml/real.py"
    write(target, b"VALUE = 1\n")
    linked = args["source_root"] / "ml/link.py"
    try:
        linked.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows user cannot create symbolic links")
    args["source_inventory"]["ml/link.py"] = digest(b"VALUE = 1\n")
    with pytest.raises(installation.InstallationError, match="Symlink/reparse"):
        installation.seal(root, **args)
