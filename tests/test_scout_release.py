"""Offline exact-byte selection/installation failure cases; no private recipes."""
import json
import errno
import os
from pathlib import Path

import pytest

from tools import scout_release as releases


def make_release(tmp_path, name="a"):
    root = tmp_path / name
    source = root / "source"
    source.mkdir(parents=True)
    (source / "module.py").write_text("VALUE = '" + name + "'\n", encoding="utf-8")
    recipe = root / "recipe.json"
    recipe.write_text(json.dumps({"pc": "Scout", "symbols": [f"SYM{n}" for n in range(11)]}), encoding="utf-8")
    manifest = {"source_root": str(source), "lifecycle_protocol": releases.PROTOCOL,
                "files": {"module.py": releases.sha256((source / "module.py").read_bytes())},
                "recipe_path": str(recipe), "recipe_file_sha256": releases.sha256(recipe.read_bytes())}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root, releases.sha256((root / "manifest.json").read_bytes())


def update_manifest(root, mutate):
    manifest = json.loads((root / "manifest.json").read_bytes())
    mutate(manifest)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return releases.sha256((root / "manifest.json").read_bytes())


def test_atomic_selection_keeps_complete_predecessor_and_rereads_next_generation(tmp_path):
    a, a_hash = make_release(tmp_path)
    b, b_hash = make_release(tmp_path, "b")
    selector = tmp_path / "active.json"
    first = releases.select_release(selector, a, a_hash)
    before = selector.read_bytes()
    second = releases.select_release(selector, b, b_hash, expected_selection_sha256=releases.sha256(before))
    assert first["release_root"] == str(a)
    assert second["release_root"] == str(b)
    assert json.loads(selector.read_bytes())["previous"] == json.loads(before)
    assert releases.resolve_selection(selector)["manifest_sha256"] == b_hash
    assert releases.validate_release(a, a_hash)["manifest_sha256"] == a_hash
    assert (a / "source/module.py").read_text() == "VALUE = 'a'\n"


def test_selection_compare_and_swap_rejects_concurrent_installer(tmp_path):
    a, a_hash = make_release(tmp_path)
    b, b_hash = make_release(tmp_path, "b")
    selector = tmp_path / "active.json"
    releases.select_release(selector, a, a_hash)
    before = selector.read_bytes()
    with pytest.raises(ValueError, match="changed since installer review"):
        releases.select_release(selector, b, b_hash, expected_selection_sha256="wrong")
    assert selector.read_bytes() == before


def test_changing_pointer_during_validation_has_no_unsafe_fallback(tmp_path, monkeypatch):
    a, a_hash = make_release(tmp_path)
    b, b_hash = make_release(tmp_path, "b")
    selector = tmp_path / "active.json"
    releases.select_release(selector, a, a_hash)
    original = releases.validate_release

    def change_after_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        releases.atomic_json(selector, {"schema": releases.SELECTOR_SCHEMA, "release_root": str(b), "manifest_sha256": b_hash})
        return result

    monkeypatch.setattr(releases, "validate_release", change_after_validation)
    with pytest.raises(ValueError, match="selection changed during validation"):
        releases.resolve_selection(selector)


@pytest.mark.parametrize("suffix", [".py", ".pyc", ".pyd"])
def test_unreviewed_importable_source_is_rejected(tmp_path, suffix):
    root, digest = make_release(tmp_path)
    (root / "source" / ("unreviewed" + suffix)).write_bytes(b"not reviewed")
    with pytest.raises(ValueError, match="Unmanifested importable"):
        releases.validate_release(root, digest)


def test_manifested_pyc_is_also_forbidden(tmp_path):
    root, _ = make_release(tmp_path)
    (root / "source/module.pyc").write_bytes(b"cached old code")
    digest = update_manifest(root, lambda m: m["files"].update({"module.pyc": releases.sha256(b"cached old code")}))
    with pytest.raises(ValueError, match="Unmanifested importable"):
        releases.validate_release(root, digest)


@pytest.mark.parametrize("failure", ["manifest_hash", "source_hash", "source_missing", "source_root", "protocol", "empty_inventory", "recipe_hash", "recipe_duplicate", "recipe_scope"])
def test_incomplete_or_incompatible_release_never_replaces_previous(tmp_path, failure):
    a, a_hash = make_release(tmp_path)
    b, b_hash = make_release(tmp_path, "b")
    selector = tmp_path / "active.json"
    releases.select_release(selector, a, a_hash)
    before = selector.read_bytes()
    if failure == "manifest_hash":
        b_hash = "wrong"
    elif failure == "source_hash":
        (b / "source/module.py").write_text("partial update", encoding="utf-8")
    elif failure == "source_missing":
        (b / "source/module.py").unlink()
    elif failure == "source_root":
        b_hash = update_manifest(b, lambda m: m.update(source_root=str(a / "source")))
    elif failure == "protocol":
        b_hash = update_manifest(b, lambda m: m.update(lifecycle_protocol="incompatible"))
    elif failure == "empty_inventory":
        b_hash = update_manifest(b, lambda m: m.update(files={}))
    elif failure == "recipe_hash":
        (b / "recipe.json").write_text("changed", encoding="utf-8")
    elif failure in {"recipe_duplicate", "recipe_scope"}:
        symbols = ["SAME"] * 11 if failure == "recipe_duplicate" else [f"SYM{i}" for i in range(10)]
        (b / "recipe.json").write_text(json.dumps({"pc": "Scout", "symbols": symbols}), encoding="utf-8")
        b_hash = update_manifest(b, lambda m: m.update(recipe_file_sha256=releases.sha256((b / "recipe.json").read_bytes())))
    with pytest.raises((ValueError, FileNotFoundError)):
        releases.select_release(selector, b, b_hash, expected_selection_sha256=releases.sha256(before))
    assert selector.read_bytes() == before
    assert releases.resolve_selection(selector)["manifest_sha256"] == a_hash


def test_bad_selected_release_stops_instead_of_implicitly_using_previous(tmp_path):
    a, a_hash = make_release(tmp_path)
    b, b_hash = make_release(tmp_path, "b")
    selector = tmp_path / "active.json"
    releases.select_release(selector, a, a_hash)
    before = selector.read_bytes()
    releases.select_release(selector, b, b_hash, expected_selection_sha256=releases.sha256(before))
    (b / "source/module.py").write_text("torn", encoding="utf-8")
    with pytest.raises(ValueError, match="source hash differs"):
        releases.resolve_selection(selector)
    assert json.loads(selector.read_bytes())["release_root"] == str(b)
    assert releases.validate_release(a, a_hash)["manifest_sha256"] == a_hash


def test_manifest_cannot_escape_source_root(tmp_path):
    root, _ = make_release(tmp_path)
    outside = root / "outside.py"
    outside.write_bytes(b"outside")
    digest = update_manifest(root, lambda m: m["files"].update({"../outside.py": releases.sha256(outside.read_bytes())}))
    with pytest.raises(ValueError, match="source hash differs"):
        releases.validate_release(root, digest)


def test_atomic_replace_failure_preserves_complete_old_bytes(tmp_path, monkeypatch):
    target = tmp_path / "active.json"
    releases.atomic_json(target, {"old": "whole"})
    before = target.read_bytes()

    def fail_replace(*_):
        raise OSError("fake sharing violation")

    monkeypatch.setattr(releases.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fake sharing violation"):
        releases.atomic_json(target, {"new": "whole"})
    assert target.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_windows_sharing_retry_preserves_old_and_frozen_new_bytes(tmp_path, monkeypatch, winerror):
    target = tmp_path / "active.json"
    releases.atomic_json(target, {"version": "old"})
    before = target.read_bytes()
    real_replace = releases.os.replace
    elapsed = [0.0]
    attempts = []
    value = {"version": "reviewed-new"}
    frozen = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    monkeypatch.setattr(releases.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(releases.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def sharing_then_success(source, destination):
        attempts.append((str(source), Path(source).read_bytes()))
        assert target.read_bytes() == before
        value["version"] = "changed-after-serialization"
        if len(attempts) < 3:
            error = PermissionError("fake transient destination sharing")
            error.winerror = winerror
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(releases.os, "replace", sharing_then_success)
    releases.atomic_json(target, value)
    assert len(attempts) == 3
    assert len({name for name, _ in attempts}) == 1
    assert all(raw == frozen for _, raw in attempts)
    assert target.read_bytes() == frozen
    assert elapsed[0] == pytest.approx(0.02)
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("winerror", [None, 13, 87])
def test_nonsharing_permission_failure_is_immediate(tmp_path, monkeypatch, winerror):
    target = tmp_path / "active.json"
    releases.atomic_json(target, {"old": "stable"})
    before = target.read_bytes()
    attempts, sleeps = [], []
    error = PermissionError("precise nonsharing failure")
    if winerror is not None:
        error.winerror = winerror

    def fail(*args):
        attempts.append(args)
        raise error

    monkeypatch.setattr(releases.os, "replace", fail)
    monkeypatch.setattr(releases.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as caught:
        releases.atomic_json(target, {"new": "never published"})
    assert caught.value is error
    assert len(attempts) == 1 and sleeps == []
    assert target.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_permanently_held_destination_is_bounded_and_preserves_exact_error(tmp_path, monkeypatch, winerror):
    target = tmp_path / "active.json"
    releases.atomic_json(target, {"old": "complete"})
    before = target.read_bytes()
    elapsed, attempts = [0.0], []
    error = PermissionError("destination remains held")
    error.winerror = winerror
    monkeypatch.setattr(releases.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(releases.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def permanently_held(*args):
        attempts.append(args)
        assert target.read_bytes() == before
        raise error

    monkeypatch.setattr(releases.os, "replace", permanently_held)
    with pytest.raises(PermissionError) as caught:
        releases.atomic_json(target, {"new": "unpublished"})
    assert caught.value is error
    assert 2 <= len(attempts) <= 102
    assert 1 <= elapsed[0] <= 1.01
    assert target.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("platform, expected", [("nt", True), ("posix", False)])
def test_errno_only_sharing_denial_is_windows_specific(monkeypatch, platform, expected):
    from types import SimpleNamespace
    error = PermissionError(errno.EACCES, "CRT discarded native Windows error")
    assert getattr(error, "winerror", None) is None
    monkeypatch.setattr(releases, "os", SimpleNamespace(name=platform))
    assert releases._sharing_denial(error) is expected


@pytest.mark.skipif(os.name != "nt", reason="Windows CRT exposes EACCES without winerror")
@pytest.mark.parametrize("permanent", [False, True])
def test_errno_only_windows_reader_retries_with_exact_bytes_and_bounded_failure(tmp_path, monkeypatch, permanent):
    target = tmp_path / "active.json"
    original = b'{"complete": "original evidence"}'
    target.write_bytes(original)
    real_read = Path.read_bytes
    attempts, elapsed = [], [0.0]
    error = PermissionError(errno.EACCES, "read sharing without winerror")
    assert getattr(error, "winerror", None) is None
    monkeypatch.setattr(releases.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(releases.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def read_held(path):
        attempts.append(path)
        if permanent or len(attempts) < 3:
            raise error
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_held)
    if permanent:
        with pytest.raises(PermissionError) as caught:
            releases.read_bytes(target)
        assert caught.value is error
        assert 2 <= len(attempts) <= 102 and 1 <= elapsed[0] <= 1.01
    else:
        assert releases.read_bytes(target) == original
        assert len(attempts) == 3 and elapsed[0] == pytest.approx(0.02)
    assert real_read(target) == original


@pytest.mark.skipif(os.name != "nt", reason="Windows CRT exposes EACCES without winerror")
@pytest.mark.parametrize("permanent", [False, True])
def test_errno_only_windows_writer_preserves_old_bytes_and_bounds_retry(tmp_path, monkeypatch, permanent):
    target = tmp_path / "active.json"
    original = b'{"complete": "original evidence"}'
    target.write_bytes(original)
    real_replace = releases.os.replace
    attempts, elapsed = [], [0.0]
    value = {"complete": "reviewed replacement"}
    frozen = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    error = PermissionError(errno.EACCES, "replace sharing without winerror")
    assert getattr(error, "winerror", None) is None
    monkeypatch.setattr(releases.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(releases.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def replace_held(source, destination):
        attempts.append((str(source), Path(source).read_bytes()))
        assert target.read_bytes() == original
        value["complete"] = "unreviewed mutation after serialization"
        if permanent or len(attempts) < 3:
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(releases.os, "replace", replace_held)
    if permanent:
        with pytest.raises(PermissionError) as caught:
            releases.atomic_json(target, value)
        assert caught.value is error
        assert 2 <= len(attempts) <= 102 and 1 <= elapsed[0] <= 1.01
        assert target.read_bytes() == original
    else:
        releases.atomic_json(target, value)
        assert len(attempts) == 3 and elapsed[0] == pytest.approx(0.02)
        assert target.read_bytes() == frozen
    assert len({name for name, _ in attempts}) == 1
    assert all(raw == frozen for _, raw in attempts)
    assert list(tmp_path.glob("*.tmp")) == []
