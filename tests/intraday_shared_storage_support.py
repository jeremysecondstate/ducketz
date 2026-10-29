"""Keep local adapter integration fixtures explicit on an older public base."""
import hashlib
import json
from pathlib import Path


def require_composed_storage():
    """Skip known uninstalled adapters; reject unexpected source drift."""
    import pytest
    root = Path(__file__).resolve().parents[1]
    meta = json.loads((root / "docs/development/patches/intraday-shared-storage.json").read_bytes())
    missing = []
    for name, item in meta["targets"].items():
        path = root / name
        raw = path.read_bytes() if path.is_file() else None
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        normalized = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() if raw is not None else None
        if digest == item["after_raw_sha256"] or normalized == item["after_sha256"]:
            continue
        known = {item["before_raw_sha256"], item["before_sha256"], item["public_base_sha256"]}
        assert digest in known or normalized in known, "Unreviewed storage adapter bytes: " + name
        missing.append(name)
    if missing:
        pytest.skip("Full local shared-storage adapters are not installed on this reviewed public base: "
                    + ", ".join(missing), allow_module_level=True)
