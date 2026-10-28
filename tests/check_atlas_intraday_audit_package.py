"""Check exact audit package bytes and run portable offline regressions."""
from pathlib import Path
import hashlib
import importlib.util
import json
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("audit_package_checker", root / "tests/check_atlas_intraday_split_package.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    checker.MANIFEST = "docs/development/patches/atlas-intraday-audit.json"
    support = checker.helper(root)
    evidence, targets, portable = checker.check_package(root, support)
    metadata = json.loads((root / checker.MANIFEST).read_bytes())
    for entry in metadata["extracts"].values():
        if hashlib.sha256(entry["source"].encode()).hexdigest() != entry["sha256"]:
            raise ValueError("Audit worker extract changed")
    print("ATLAS_AUDIT_ADAPTER_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    originals = support.isolate(root)
    import pytest
    (root / "scratch/checks").mkdir(parents=True, exist_ok=True)
    result = pytest.main(["--noconftest", "-q", "-rs", "-p", "no:cacheprovider",
                         "--basetemp", str(root / "scratch/checks/atlas-audit"), *targets])
    support.check_imports(root, originals)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(root) and path.suffix == ".py" and ".venv" not in path.parts:
                closure[path.relative_to(root).as_posix()] = support.sha(path.read_bytes())
    closure["tests/check_atlas_intraday_split_package.py"] = support.sha((root / "tests/check_atlas_intraday_split_package.py").read_bytes())
    print("ATLAS_AUDIT_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
