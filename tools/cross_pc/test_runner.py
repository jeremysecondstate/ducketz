"""Private subprocess harness: candidate imports and network-free verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import socket
import sys


def main():
    specification = json.loads(Path(sys.argv[1]).read_text())
    root = Path(specification["root"]).resolve()
    original = Path(specification["original_root"]).resolve()
    expected = specification["fingerprints"]
    argv = specification["argv"][1:]
    if argv[0] == "-B":
        argv = argv[1:]
    sys.dont_write_bytecode = True
    # -B prevents writes but still permits stale timestamp-based bytecode reads.
    # A new empty prefix makes every candidate import compile the attested source.
    fresh_cache = Path(specification["result_path"]).with_suffix(".empty-pycache")
    fresh_cache.mkdir(exist_ok=False)
    sys.pycache_prefix = str(fresh_cache)
    sys.path.insert(0, str(root))
    def no_network(*args, **kwargs):
        raise RuntimeError("network disabled for cross-PC offline checks")
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    code = 0
    try:
        if argv[0] == "-m":
            sys.argv = argv[1:]
            runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        else:
            sys.argv = argv
            runpy.run_path(str(root / argv[0]), run_name="__main__")
    except SystemExit as result:
        code = result.code or 0
    finally:
        imported, violations = {}, []
        for module in list(sys.modules.values()):
            filename = getattr(module, "__file__", None)
            if not filename:
                continue
            path = Path(filename).resolve()
            if path == Path(__file__).resolve():
                continue  # The pinned verifier is separate from candidate application imports.
            if path.is_relative_to(root) and path.is_file() and path.suffix == ".py":
                name = path.relative_to(root).as_posix()
                if ".venv" in path.parts:
                    continue
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                imported[name] = sha
                if expected.get(name) != sha:
                    violations.append("unbound candidate dependency: " + name)
            elif original != root and path.is_relative_to(original) and ".venv" not in path.parts:
                violations.append("import from original source checkout: " + path.relative_to(original).as_posix())
        result = {"imported_fingerprints": imported, "violations": sorted(set(violations)), "test_exit_code": code}
        Path(specification["result_path"]).write_text(json.dumps(result, indent=2))
        if violations:
            print("Cross-PC verifier rejected unbound or non-candidate imports:", *sorted(set(violations)), sep="\n", file=sys.stderr)
            code = 86
    return code


if __name__ == "__main__":
    raise SystemExit(main())
