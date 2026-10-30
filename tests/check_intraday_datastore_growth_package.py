"""Exact portable growth patch checks; no providers, fitting or runtime entrypoints."""
from __future__ import annotations
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
from types import ModuleType

MANIFEST = "docs/development/patches/intraday-datastore-growth.json"
TARGETS = {"ml/intraday/runtime.py", "ml/intraday/live_session.py",
           "ml/intraday/snapshot_references.py", "ml/intraday/split_execution.py"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reconstruct(raw):
    lines, index, result = raw.splitlines(keepends=True), 0, {}
    while index < len(lines):
        if not lines[index].startswith(b"--- a/"):
            raise ValueError("Unexpected growth patch header")
        name = lines[index][6:].rstrip(b"\n").decode()
        if name not in TARGETS or name in result:
            raise ValueError("Unexpected growth patch target")
        if lines[index+1].rstrip(b"\n") != ("+++ b/"+name).encode():
            raise ValueError("Renamed growth patch path")
        match = re.fullmatch(rb"@@ -1,(\d+) \+1,(\d+) @@\n", lines[index+2])
        if not match:
            raise ValueError("Exact complete before/after context required")
        old_count, new_count = map(int, match.groups())
        old, new = [], []
        index += 3
        while len(old) < old_count or len(new) < new_count:
            line = lines[index]
            if line[:1] not in {b" ", b"+", b"-"}:
                raise ValueError("Invalid growth patch marker")
            if line[:1] != b"+": old.append(line[1:])
            if line[:1] != b"-": new.append(line[1:])
            index += 1
        if (len(old), len(new)) != (old_count, new_count):
            raise ValueError("Growth patch counts differ")
        result[name] = b"".join(old), b"".join(new)
    if set(result) != TARGETS:
        raise ValueError("Growth patch inventory differs")
    return result


def owned_only(name, old, new):
    before, after = ast.parse(old), ast.parse(new)
    added = {
        "ml/intraday/snapshot_references.py": {"model_market_input_metadata", "resolve_model_market_input_metadata", "MARKET_TRAINING_REFERENCE"},
        "ml/intraday/split_execution.py": {"read_model_event", "MODEL_EVENT_REFERENCE"},
    }.get(name, set())
    def key(node):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)): return node.name
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
    found = {key(node) for node in after.body} & added
    if found != added:
        raise ValueError("Missing owned growth functions")
    after.body = [node for node in after.body if key(node) not in added]
    class Normalize(ast.NodeTransformer):
        stores = 0
        metadata = 0
        imports = 0
        def visit_ImportFrom(self, node):
            if name == "ml/intraday/runtime.py" and node.module == "snapshot_references" and [a.name for a in node.names] == ["model_market_input_metadata"]:
                self.imports += 1
                return None
            return node
        def visit_Call(self, node):
            node = self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id == "ShadowStore" and name in {"ml/intraday/runtime.py", "ml/intraday/live_session.py"}:
                remove = [k for k in node.keywords if k.arg in {"snapshot_directory", "chunked_snapshots"}]
                if remove:
                    if {k.arg for k in remove} != {"snapshot_directory", "chunked_snapshots"}:
                        raise ValueError("Incomplete explicit snapshot binding")
                    chunked = next(k.value for k in remove if k.arg == "chunked_snapshots")
                    directory = next(k.value for k in remove if k.arg == "snapshot_directory")
                    if not isinstance(chunked, ast.Constant) or chunked.value is not True:
                        raise ValueError("Snapshot sharing must remain chunked")
                    if not isinstance(directory, ast.BinOp) or not isinstance(directory.right, ast.Constant) or directory.right.value != "intraday/system-1a/ready/snapshots":
                        raise ValueError("Snapshot binding changed canonical directory")
                    node.keywords = [k for k in node.keywords if k not in remove]
                    self.stores += 1
            if name == "ml/intraday/runtime.py" and isinstance(node.func, ast.Name) and node.func.id == "model_market_input_metadata":
                if [ast.dump(a) for a in node.args] != ["Name(id='snapshot', ctx=Load())", "Name(id='snapshot_hash', ctx=Load())"] or node.keywords:
                    raise ValueError("Model metadata is not bound to frozen input identity")
                self.metadata += 1
                return ast.Call(func=ast.Attribute(value=ast.Name(id="snapshot", ctx=ast.Load()), attr="get", ctx=ast.Load()), args=[ast.Constant(value="market_inputs")], keywords=[])
            return node
    normalize = Normalize()
    after = normalize.visit(after)
    expected = (1, 1, 1) if name.endswith("runtime.py") else (2, 0, 0) if name.endswith("live_session.py") else (0, 0, 0)
    if (normalize.stores, normalize.metadata, normalize.imports) != expected:
        raise ValueError("Owned source replacement count differs")
    if ast.dump(before, include_attributes=False) != ast.dump(after, include_attributes=False):
        raise ValueError("Unowned source syntax changed: " + name)


def exercise(root, files):
    package = ModuleType("growth_checked")
    package.__path__ = []
    sys.modules[package.__name__] = package
    prediction = ModuleType("growth_checked.prediction")
    prediction.__package__ = package.__name__
    sys.modules[prediction.__name__] = prediction
    exec(compile((root / "ml/intraday/prediction.py").read_bytes(), "reviewed-prediction.py", "exec"), prediction.__dict__)
    loaded = {}
    for module_name in ("snapshot_references", "split_execution"):
        module = ModuleType("growth_checked."+module_name)
        module.__package__ = package.__name__
        sys.modules[module.__name__] = module
        exec(compile(files["ml/intraday/"+module_name+".py"][1], "reconstructed-"+module_name+".py", "exec"), module.__dict__)
        loaded[module_name] = module
    refs, events = loaded["snapshot_references"], loaded["split_execution"]
    snapshot = {"market_inputs": {"training": [{"original_receipt": "x" * 300} for _ in range(512)],
        "inference": {"original": True}, "training_rows": 512, "receipt_rule": "original-clock"}}
    original = prediction.encoded(snapshot)
    compact = refs.model_market_input_metadata(snapshot, prediction.digest(snapshot))
    assert len(prediction.encoded(compact)) < len(original) / 100
    assert refs.resolve_model_market_input_metadata(compact, snapshot) == snapshot["market_inputs"]
    assert prediction.encoded(snapshot) == original
    for field, value in (("snapshot_hash", "0" * 64), ("rows", True), ("field", "X"), ("sha256", "f" * 64)):
        damaged = copy.deepcopy(compact)
        damaged["training_reference"][field] = value
        try: refs.resolve_model_market_input_metadata(damaged, snapshot)
        except prediction.ContractError: pass
        else: raise AssertionError("Changed provenance accepted")
    with tempfile.TemporaryDirectory() as temporary:
        result = {"probability_not_down": .5001, "outputs": [{"original": [1, 2, 3]}]}
        original_event = {"attempt": "unchanged", "result": result}
        raw = prediction.encoded(original_event).encode()
        ref = events.seal_evidence(temporary, {"fitted": result})
        envelope = {"schema": events.MODEL_EVENT_REFERENCE, "attempt": "unchanged",
            "original_sha256": sha(raw), "original_bytes": len(raw), "full_models": ref}
        assert prediction.encoded(events.read_model_event(prediction.encoded(envelope), evidence_root=temporary)).encode() == raw
        assert events.read_model_event(raw, evidence_root=temporary) == original_event
        for changes in ({"original_bytes": True}, {"attempt": "changed"}, {"original_sha256": "0" * 64}):
            try: events.read_model_event(prediction.encoded({**envelope, **changes}), evidence_root=temporary)
            except prediction.ContractError: pass
            else: raise AssertionError("Changed model event accepted")
    return 12


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / MANIFEST).read_bytes())
    raw = (root / manifest["patch"]).read_bytes()
    if sha(raw) != manifest["patch_sha256"]:
        raise ValueError("Patch bytes changed")
    files = reconstruct(raw)
    if {r["path"] for r in manifest["files"]} != TARGETS:
        raise ValueError("Manifest inventory changed")
    for item in manifest["files"]:
        old, new = files[item["path"]]
        if sha(old) != item["before_sha256"] or sha(new) != item["after_sha256"]:
            raise ValueError("Reconstructed source hash differs")
        owned_only(item["path"], old, new)
    for name, expected in manifest["direct_files"].items():
        if sha((root/name).read_bytes()) != expected:
            raise ValueError("Direct portable source changed")
    network = []
    def denied(*a, **k):
        network.append(True)
        raise AssertionError("Offline source checks cannot contact providers")
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = denied
    count = exercise(root, files)
    assert not network
    print("DATASTORE_GROWTH_PACKAGE="+json.dumps({"checks":count,"targets":len(files),"network_attempts":0,"runtime_or_data_mutations":0,"source_delta_verified":True},sort_keys=True))


if __name__ == "__main__":
    main()
