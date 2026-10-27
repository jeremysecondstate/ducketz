"""Verify the portable UI patch and exercise its exact production method bodies.

No application imports, Tk windows, providers, operating stores or traders are used.
SQLite verification uses synthetic in-memory fixtures only.
Public bases with different complete consumers receive explicit projection-only
provenance; full installed behavior still requires separate local integration.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import queue
import re
import sqlite3
import threading
from types import SimpleNamespace
import textwrap

MANIFEST = "docs/development/patches/ui-responsiveness.json"
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SHA = re.compile(r"[0-9a-f]{64}\Z")

def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Package paths must be portable and relative")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("Invalid package path")
    if set(part.lower() for part in path.parts) & {".git", ".env", "scratch", ".venv", "data", "artifacts"}:
        raise ValueError("Private operating data cannot be part of the package")
    return value


def file_header(line, prefix):
    value = line.removeprefix(prefix).rstrip(b"\r\n").decode("utf-8")
    if value == "/dev/null":
        return None
    if not value.startswith(("a/", "b/")):
        raise ValueError("Patch needs portable a/ and b/ paths")
    return relative(value[2:])


def parse_patch(raw):
    lines, offset, files = raw.splitlines(keepends=True), 0, {}
    while offset < len(lines):
        if lines[offset].startswith((b"diff --git ", b"index ", b"new file mode ", b"deleted file mode ")):
            offset += 1
            continue
        if not lines[offset].startswith(b"--- ") or offset + 1 >= len(lines) or not lines[offset + 1].startswith(b"+++ "):
            raise ValueError("Unexpected unified-patch content")
        before = file_header(lines[offset], b"--- ")
        after = file_header(lines[offset + 1], b"+++ ")
        name = before or after
        if not name or (before and after and before != after) or name in files:
            raise ValueError("Duplicate, renamed or missing patch path")
        offset += 2
        hunks = []
        while offset < len(lines) and lines[offset].startswith(b"@@ "):
            match = HUNK.match(lines[offset])
            if not match:
                raise ValueError("Malformed unified hunk header")
            old_start, old_count, new_start, new_count = [int(value) for value in (
                match[1], match[2] or b"1", match[3], match[4] or b"1")]
            old_lines, new_lines, previous = [], [], None
            offset += 1
            while len(old_lines) < old_count or len(new_lines) < new_count:
                if offset >= len(lines):
                    raise ValueError("Truncated patch hunk")
                line = lines[offset]
                marker, body = line[:1], line[1:]
                if marker not in {b" ", b"-", b"+"}:
                    raise ValueError("Unexpected patch hunk line")
                if marker != b"+":
                    old_lines.append(body)
                if marker != b"-":
                    new_lines.append(body)
                previous = marker
                offset += 1
                if offset < len(lines) and lines[offset].startswith(b"\\ No newline at end of file"):
                    if previous != b"+":
                        old_lines[-1] = old_lines[-1].removesuffix(b"\n").removesuffix(b"\r")
                    if previous != b"-":
                        new_lines[-1] = new_lines[-1].removesuffix(b"\n").removesuffix(b"\r")
                    offset += 1
            if len(old_lines) != old_count or len(new_lines) != new_count:
                raise ValueError("Patch line count mismatch")
            hunks.append({"old_start": old_start, "old_count": old_count, "new_start": new_start,
                          "new_count": new_count, "old_lines": old_lines, "new_lines": new_lines})
        if not hunks:
            raise ValueError("Empty patch file entry")
        files[name] = {"before": before, "after": after, "hunks": hunks}
    return files


def apply_hunks(original, hunks):
    lines, cursor, output = original.splitlines(keepends=True), 0, []
    for hunk in hunks:
        start = hunk["old_start"] - (1 if hunk["old_count"] else 0)
        if start < cursor or start > len(lines) or lines[start:start + hunk["old_count"]] != hunk["old_lines"]:
            raise ValueError("Hunk does not match the exact preimage")
        output.extend(lines[cursor:start])
        expected_start = len(output) + (1 if hunk["new_count"] else 0)
        if hunk["new_start"] != expected_start:
            raise ValueError("Hunk target offset changed unrelated line positions")
        output.extend(hunk["new_lines"])
        cursor = start + hunk["old_count"]
    output.extend(lines[cursor:])
    return b"".join(output)


def check_projection(hunks):
    original = []
    for hunk in hunks:
        start = hunk["old_start"] - (1 if hunk["old_count"] else 0)
        if start < len(original):
            raise ValueError("Overlapping patch hunks")
        original.extend([b"# unchanged context omitted from the public patch\n"] * (start - len(original)))
        original.extend(hunk["old_lines"])
    return apply_hunks(b"".join(original), hunks)


def check_package(candidate):
    manifest = json.loads((candidate / MANIFEST).read_text("utf-8"))
    if manifest.get("schema") != "ui-responsiveness-patches-v1":
        raise ValueError("Unsupported UI patch manifest")
    raw = (candidate / relative(manifest["patch"])).read_bytes()
    if sha(raw) != manifest["patch_sha256"]:
        raise ValueError("UI patch bytes changed")
    parsed = parse_patch(raw)
    entries = {relative(item["path"]): item for item in manifest["files"]}
    if len(entries) != len(manifest["files"]) or set(entries) != set(parsed):
        raise ValueError("Patch inventory differs from manifest")
    evidence = {}
    for name, patch in parsed.items():
        entry = entries[name]
        if patch["before"] != name or patch["after"] != name or entry["operation"] != "modify":
            raise ValueError("This package must preserve existing consumer files")
        if any(not SHA.fullmatch(entry[key]) for key in ("before_sha256", "after_sha256")):
            raise ValueError("Invalid consumer hash")
        check_projection(patch["hunks"])
        actual = (candidate / name).read_bytes() if (candidate / name).is_file() else None
        if actual is not None and sha(actual) == entry["before_sha256"]:
            updated = apply_hunks(actual, patch["hunks"])
            if sha(updated) != entry["after_sha256"]:
                raise ValueError("Exact consumer postimage mismatch: " + name)
            ast.parse(updated.decode("utf-8-sig"), filename=name)
            evidence[name] = "exact_preimage_reconstruction"
        elif actual is not None and sha(actual) == entry["after_sha256"]:
            reverse = [dict(old_start=h["new_start"], old_count=h["new_count"],
                            new_start=h["old_start"], new_count=h["old_count"],
                            old_lines=h["new_lines"], new_lines=h["old_lines"])
                       for h in patch["hunks"]]
            if sha(apply_hunks(actual, reverse)) != entry["before_sha256"]:
                raise ValueError("Exact consumer reverse reconstruction mismatch: " + name)
            ast.parse(actual.decode("utf-8-sig"), filename=name)
            evidence[name] = "exact_postimage_reverse_reconstruction"
        else:
            evidence[name] = "hunk_projection_only_local_exact_preimage_required"
    direct = manifest["direct_files"]
    if not isinstance(direct, dict) or not direct:
        raise ValueError("Direct test/source inventory is required")
    for name, expected in direct.items():
        path = candidate / relative(name)
        if not SHA.fullmatch(expected) or sha(path.read_bytes()) != expected:
            raise ValueError("Direct package bytes changed: " + name)
        if path.suffix == ".py":
            ast.parse(path.read_text("utf-8-sig"), filename=name)
    for name, classes in manifest["behavior_methods"].items():
        name = relative(name)
        # Executed snippets are literal postimage lines in the reviewed patch.
        # Their surrounding original file is deliberately not invented.
        contexts = [b"".join(h["new_lines"]).decode("utf-8-sig").replace("\r\n", "\n")
                    for h in parsed[name]["hunks"]]
        for class_name, methods in classes.items():
            if not class_name.isidentifier() or not methods:
                raise ValueError("Invalid behavior class")
            for method_name, body in methods.items():
                if not any(body in context for context in contexts):
                    raise ValueError("Behavior body not bound to literal patch: " + name + ":" + method_name)
                syntax = ast.parse(textwrap.dedent(body))
                if len(syntax.body) != 1 or not isinstance(syntax.body[0], ast.FunctionDef) or syntax.body[0].name != method_name:
                    raise ValueError("Expected one exact production method")
    for name, definitions in manifest["behavior_functions"].items():
        name = relative(name)
        contexts = [b"".join(h["new_lines"]).decode("utf-8-sig").replace("\r\n", "\n")
                    for h in parsed[name]["hunks"]]
        for symbol, body in definitions.items():
            if not any(body in context for context in contexts):
                raise ValueError("Behavior function not bound to literal patch: " + name + ":" + symbol)
            syntax = ast.parse(body)
            if len(syntax.body) != 1:
                raise ValueError("Expected one production definition")
            node = syntax.body[0]
            if isinstance(node, ast.FunctionDef):
                if node.name != symbol:
                    raise ValueError("Function identity mismatch")
            elif isinstance(node, ast.Assign):
                if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name) or node.targets[0].id != symbol:
                    raise ValueError("Constant identity mismatch")
                ast.literal_eval(node.value)
            else:
                raise ValueError("Unexpected production definition")
    return manifest, evidence


def production_class(manifest, filename, class_name, namespace):
    methods = manifest["behavior_methods"][filename][class_name]
    body = [ast.parse(textwrap.dedent(source)).body[0] for source in methods.values()]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                             ast.ClassDef(name=class_name, bases=[], keywords=[], body=body, decorator_list=[], type_params=[])],
                        type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, filename, "exec"), namespace)
    return namespace[class_name]


class Scheduler:
    def __init__(self):
        self.pending = {}
        self.sequence = 0

    def after(self, delay, callback):
        self.sequence += 1
        key = "job-" + str(self.sequence)
        self.pending[key] = (delay, callback)
        return key

    def after_idle(self, callback):
        return self.after("idle", callback)

    def after_cancel(self, key):
        del self.pending[key]

    def run_one(self):
        key = next(iter(self.pending))
        _, callback = self.pending.pop(key)
        callback()


def check_lazy_workspaces(manifest):
    mounted, history, frames = [], [], []
    scheduler = Scheduler()
    class Frame:
        def __init__(self, parent):
            self.children = []
            frames.append(self)
        def winfo_children(self):
            return self.children
    class Notebook:
        def __init__(self, root):
            self.tabs = []
            self.selected = None
        def pack(self, **kwargs):
            pass
        def add(self, frame, *, text):
            self.tabs.append((frame, text))
            if self.selected is None:
                self.selected = str(frame)
        def bind(self, event, callback, **kwargs):
            self.callback = callback
        def select(self):
            return self.selected
    namespace = dict(ttk=SimpleNamespace(Notebook=Notebook, Frame=Frame), tk=SimpleNamespace(BOTH="both"))
    names = ["RollingForecastTab", "OptionsStrategiesTab", "SchwabDucketsTab", "HyperliquidDucketsTab",
             "HyperWorkspace", "GameplanStatsTab", "GameplanTab", "IntradayTab"]
    app = None
    def factory(name):
        def mount(**kwargs):
            mounted.append((name, kwargs))
            app._show_selected_workspace()  # Reentrant selection must not construct twice.
            return object()
        return mount
    for name in names:
        namespace[name] = factory(name)
    def account_history():
        value = object()
        history.append(value)
        return value
    namespace["HyperliquidAccountHistoryService"] = account_history
    cls = production_class(manifest, "app/ui/ducket_bucket.py", "DucketBucketApp", namespace)
    app = cls.__new__(cls)
    app.root = scheduler
    app._build_layout()
    assert len(app.notebook.tabs) == 8 and not mounted and not history
    scheduler.run_one()
    assert [name for name, _ in mounted] == [names[0]], "Hidden tabs ran during launch"
    app._show_selected_workspace()
    assert len(mounted) == 1
    for index in (3, 4, 7, 7, 0):
        app.notebook.selected = str(frames[index])
        app._show_selected_workspace()
    assert [name for name, _ in mounted] == [names[i] for i in (0, 3, 4, 7)]
    assert len(history) == 1
    assert mounted[1][1]["account_history"] is mounted[2][1]["account_history"] is history[0]
    # Failed construction clears partial widgets and permits an explicit retry.
    app.notebook.selected = str(frames[2])
    destroyed = []
    frames[2].children = [SimpleNamespace(destroy=lambda: destroyed.append(True))]
    def failed(**kwargs):
        raise RuntimeError("synthetic constructor failure")
    app._workspace_factories[app.notebook.selected] = (frames[2], failed, False)
    try:
        app._show_selected_workspace()
    except RuntimeError as exc:
        assert str(exc) == "synthetic constructor failure"
    else:
        raise AssertionError("Constructor failure was swallowed")
    assert destroyed == [True] and app.notebook.selected not in app._workspaces
    app._workspace_factories[app.notebook.selected] = (frames[2], factory(names[2]), False)
    app._show_selected_workspace()
    assert mounted[-1][0] == names[2]
    return "lazy_tabs_single_mount_reentrancy_shared_history_failure_retry"


def check_scroll_coalescing(manifest):
    cls = production_class(manifest, "app/ui/rolling_forecasts.py", "RollingForecastTab", {})
    tab = cls.__new__(cls)
    tab.root, tab._scroll_region_job = Scheduler(), None
    updates, widths = [], []
    tab._update_scroll_region = lambda: updates.append(True)
    tab.canvas = SimpleNamespace(itemconfigure=lambda item, **kwargs: widths.append(kwargs["width"]))
    tab.canvas_window = 7
    tab.content_frame = SimpleNamespace(update_idletasks=lambda: (_ for _ in ()).throw(AssertionError("Nested idle drain")))
    for width in range(100):
        tab._on_canvas_resize(SimpleNamespace(width=width))
    assert len(tab.root.pending) == 1 and not updates and widths[-1] == 99
    tab.root.run_one()
    assert updates == [True] and tab._scroll_region_job is None
    tab._refresh_scroll_region()
    assert len(tab.root.pending) == 1
    tab.root.run_one()
    assert len(updates) == 2
    return "resize_coalescing_without_recursive_idle_drain"


def check_async_intraday(manifest):
    workers, read_calls, render_calls, snapshots = [], [], [], []
    worker_active = False
    class Variable:
        def __init__(self, value="All"):
            self.value = value
        def get(self):
            assert not worker_active, "Worker read a Tk variable"
            return self.value
        def set(self, value):
            assert not worker_active, "Worker wrote a Tk variable"
            self.value = value
    class DeferredThread:
        def __init__(self, *, target, name, daemon):
            assert daemon
            self.target = target
        def start(self):
            workers.append(self.target)
    def load_research(directory, *, actor):
        assert worker_active
        read_calls.append((directory, actor))
        return {"research": "fixture"}
    saved = SimpleNamespace(forecasts=tuple(range(101)), sources=({"status": "read"},), warnings=())
    def load_snapshot(paths, *, local_pc, stopped):
        assert worker_active and not stopped()
        read_calls.append((paths, local_pc))
        return saved
    def prepare(snapshot, selection, paths):
        assert worker_active, "Expensive metrics ran in the UI callback"
        return {"selection": selection, "fixture": snapshot}
    namespace = dict(threading=SimpleNamespace(Thread=DeferredThread), queue=queue, POLL_MS=40,
                     _prepare_metrics=prepare, load_model_research=load_research, load_intraday_snapshot=load_snapshot)
    cls = production_class(manifest, "app/ui/intraday.py", "IntradayTab", namespace)
    tab = cls.__new__(cls)
    tab.parent = Scheduler()
    tab._closed = tab._loading = tab._render_loading = tab._render_again = False
    tab._auto_enabled = False
    tab._poll_job = tab._auto_job = None
    tab._view_revision = 1
    tab._messages, tab._stopped = queue.Queue(), threading.Event()
    tab.status = Variable("retained status")
    tab.model_research_directory, tab.local_pc, tab.database_paths = "synthetic research", "Atlas", {"Local": "synthetic store"}
    tab.snapshot, tab.model_research = saved, {"old": True}
    for key in ("pc", "symbol", "session", "mode", "reference_slot", "source_dataset", "metric_scope", "metric_window"):
        setattr(tab, key, Variable())
    def set_snapshot(snapshot, *, _prepared):
        assert not worker_active
        snapshots.append((snapshot, _prepared))
        tab.snapshot = snapshot
    tab.set_snapshot = set_snapshot
    def run_worker():
        nonlocal worker_active
        worker_active = True
        try:
            workers.pop(0)()
        finally:
            worker_active = False
    def poll():
        # Model the regular UI timer lifecycle instead of stacking test timers.
        if tab._poll_job is not None:
            tab.parent.after_cancel(tab._poll_job)
        tab._poll()
    tab.refresh()
    tab.refresh()
    assert len(workers) == 1 and not read_calls and not snapshots
    assert tab._loading
    tab.database_paths["Local"] = "changed after scheduling"
    run_worker()
    assert read_calls[1][0] == {"Local": "synthetic store"}
    assert not snapshots, "Worker applied UI state before the UI poll"
    poll()
    assert len(snapshots) == 1 and snapshots[0][0] is saved and not tab._loading
    # Keep the last successful snapshot on ordinary loader failures.
    namespace["load_intraday_snapshot"] = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture read failure"))
    tab.refresh()
    run_worker()
    poll()
    assert not tab._loading and len(snapshots) == 1
    assert "fixture read failure" in tab.status.value
    # Repeated filter changes admit only one expensive metrics worker.
    tab.render()
    tab.symbol.value = "fixture-new-filter"
    tab.render()
    assert len(workers) == 1 and tab._render_again
    run_worker()
    tab.render = lambda **kwargs: render_calls.append(kwargs)
    poll()
    assert render_calls == [{}], "Stale filter output must not be painted"
    assert not tab._render_loading and not tab._render_again
    # An old revision is discarded even when selections coincide.
    tab._messages.put(("rendered", (0, {"selection": tab._selection()})))
    poll()
    assert render_calls == [{}, {}]
    tab._messages.put(("rendered", (tab._view_revision, {"selection": tab._selection()})))
    poll()
    assert "_prepared" in render_calls[-1]
    # Child destruction is not window destruction; real closure cancels timers.
    tab._destroy(SimpleNamespace(widget=object()))
    assert not tab._closed
    tab._auto_job = tab.parent.after(900000, lambda: None)
    namespace["load_intraday_snapshot"] = load_snapshot
    tab.refresh()
    assert len(workers) == 1
    tab._destroy(SimpleNamespace(widget=tab.parent))
    assert tab._closed and tab._stopped.is_set() and not tab.parent.pending
    run_worker()
    assert tab._messages.empty(), "Closed view retained a worker result"
    tab.refresh()
    tab._poll()
    assert not workers and not tab.parent.pending
    return "async_read_single_worker_errors_stale_metrics_and_close"


def check_sqlite_projection(manifest):
    definitions = manifest["behavior_functions"]["app/ui/intraday_data.py"]
    namespace = {"json": json}
    # These are complete literal production functions/constants from the patch.
    source = "from __future__ import annotations\n" + "\n".join(definitions.values())
    exec(compile(source, "app/ui/intraday_data.py", "exec"), namespace)
    large = {"training_tree": ["synthetic-unused-evidence" * 1000] * 60}
    forecast_fields = namespace["DISPLAY_FORECAST_FIELDS"]
    model_fields = namespace["DISPLAY_MODEL_FIELDS"]
    original = {field: {"saved": field} for field in forecast_fields}
    original.update(probability_not_down=0.5001, direction="BUY", target_version="retained-science-v1",
                    training_metadata=large,
                    models=[{**{field: {"saved": field} for field in model_fields},
                             "name": "fixture-model", "probability_not_down": 0.4999,
                             "metadata": large}])
    payload = json.dumps(original)
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE forecasts(id TEXT,pc TEXT,symbol TEXT,cycle_id TEXT,reference_at TEXT,issued_at TEXT,payload TEXT)")
        for identity, issued in (("late", "2026-01-02T00:00:00Z"), ("early", "2026-01-01T00:00:00Z")):
            connection.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (identity, "Atlas", "FIXTURE", "cycle", issued, issued, payload))
        connection.commit()
        connection.execute("PRAGMA query_only=ON")
        before = connection.execute("SELECT payload FROM forecasts ORDER BY id").fetchall()
        rows = list(namespace["_display_forecasts"](connection))
        assert [row[0]["id"] for row in rows] == ["early", "late"], "Projection changed issue ordering or dropped history"
        for _, saved in rows:
            assert {key: saved[key] for key in forecast_fields} == {key: original[key] for key in forecast_fields}
            assert {key: saved["models"][0][key] for key in model_fields} == {key: original["models"][0][key] for key in model_fields}
            assert "training_metadata" not in saved and "metadata" not in saved["models"][0]
            assert len(json.dumps(saved)) < len(payload) / 50, "Python received the large scientific metadata"
        assert [tuple(row) for row in before] == [tuple(row) for row in connection.execute("SELECT payload FROM forecasts ORDER BY id")]
        for field_set in ("DISPLAY_INTENT_FIELDS", "DISPLAY_CYCLE_FIELDS"):
            fields = namespace[field_set]
            record = {field: {"original": field} for field in fields}
            record["unused_large_evidence"] = large
            query = "SELECT " + namespace["_json_projection"]("?", fields)
            raw = connection.execute(query, (json.dumps(record),)).fetchone()[0]
            assert namespace["_display_payload"](raw, fields) == {field: record[field] for field in fields}
        # Presence-sensitive evidence must retain null separately from absence,
        # and SQLite's booleans must not become integer 0/1 in Python.
        fields = ("present_null", "missing", "yes", "no", "nested")
        record = dict(present_null=None, yes=True, no=False, nested={"value": None})
        raw = connection.execute("SELECT " + namespace["_json_projection"]("?", fields),
                                 (json.dumps(record),)).fetchone()[0]
        selected = namespace["_display_payload"](raw, fields)
        assert selected == record and "missing" not in selected
        assert selected["yes"] is True and selected["no"] is False
        for malformed in ('{"unexpected_field":1}', "[1]", "null"):
            try:
                namespace["_display_payload"](malformed, forecast_fields)
            except ValueError:
                pass
            else:
                raise AssertionError("Malformed projection accepted")
    return "sqlite_compact_projection_retains_fields_order_and_originals"


def run_behavior_checks(manifest):
    return [check_lazy_workspaces(manifest), check_scroll_coalescing(manifest),
            check_async_intraday(manifest), check_sqlite_projection(manifest)]


def main():
    candidate = Path(__file__).resolve().parents[1]
    manifest, evidence = check_package(candidate)
    checks = run_behavior_checks(manifest)
    print("UI_PATCH_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    print("UI_METHOD_BEHAVIOR_CHECKS=" + json.dumps(checks))
    print("Local full-file integration and Tk responsiveness checks are separate evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


