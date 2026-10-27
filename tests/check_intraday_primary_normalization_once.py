"""Offline exact-source primary wrapper tests with explicit stdlib interface doubles."""
from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import total_ordering
import hashlib
import json
import math
from pathlib import Path
import re
import socket
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/development/patches/intraday-primary-normalization-once.json"
META = json.loads(PACKAGE.read_text(encoding="utf8"))
NAME = "ml/intraday/recovered_population.py"
RECORD = META["sources"][NAME]
PRIMARY = "intraday-verified-source-observations-v1"
RECOVERY = "intraday-multi-provider-recovery-v1"
STEP = timedelta(minutes=15)


class ContractError(ValueError):
    pass


@total_ordering
class Stamp:
    """Only timestamp interfaces exercised by primary wrapping and proof."""
    def __init__(self, value):
        self.value = value.value if isinstance(value, Stamp) else value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    @property
    def tzinfo(self):
        return self.value.tzinfo
    def tz_convert(self, zone):
        if zone != "UTC":
            raise AssertionError("Unsupported test timezone")
        return Stamp(self.value.astimezone(timezone.utc))
    def isoformat(self):
        return self.value.isoformat()
    def floor(self, frequency):
        minutes = 15 if frequency == "15min" else 1 if frequency in ("min", "1min") else None
        if minutes is None:
            raise AssertionError("Unsupported test frequency")
        return Stamp(self.value.replace(minute=self.value.minute // minutes * minutes, second=0, microsecond=0))
    def __eq__(self, other):
        return isinstance(other, Stamp) and self.value == other.value
    def __lt__(self, other):
        return self.value < other.value
    def __add__(self, duration):
        return Stamp(self.value + duration)
    def __hash__(self):
        return hash(self.value)


class NumInt:
    def __init__(self, value):
        self.value = int(value)
    def item(self):
        return self.value


class NumFloat:
    def __init__(self, value):
        self.value = float(value)
    def item(self):
        return self.value


class NumBool:
    def __init__(self, value):
        self.value = bool(value)
    def item(self):
        return self.value
    def __bool__(self):
        return self.value


class DataFrame:
    pass


def isna(value):
    return value is None or isinstance(value, float) and math.isnan(value)


def source(name, phase="after"):
    value = RECORD["extracts_" + phase][name]
    if hashlib.sha256(value["source"].encode()).hexdigest() != value["sha256"]:
        raise AssertionError("Extract changed: " + name)
    return value["source"]


def namespace(phase="after"):
    pd = SimpleNamespace(Timestamp=Stamp, isna=isna, Timedelta=timedelta, DataFrame=DataFrame)
    np = SimpleNamespace(integer=NumInt, floating=NumFloat, bool_=NumBool)
    ns = {"Mapping": Mapping, "datetime": datetime, "dataclass": dataclass, "hashlib": hashlib,
          "json": json, "math": math, "pd": pd, "np": np, "ContractError": ContractError,
          "STEP": STEP, "POPULATION_CONTRACT": RECOVERY, "SOURCE_ID": "multi:databento-schwab-fmp:15m",
          "SOURCE_DATASET": "MULTI_PROVIDER", "PRICE_FIELDS": ("open", "high", "low", "close", "volume"),
          "_NATIVE_MINUTE": "min", "_SOURCE_QUARTER": "15min"}
    native = {}
    def controlled_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "data" and level == 1:
            return SimpleNamespace(**native)
        if name == "ml.market_gap_evidence":
            return SimpleNamespace(MARKERS=tuple(META["markers"]))
        return builtins.__import__(name, globals, locals, fromlist, level)
    ns["__builtins__"] = dict(vars(builtins), __import__=controlled_import)
    for name in ("_utc", "_default", "_json", "_plain", "_digest", "_session", "_wrapper"):
        exec(compile("from __future__ import annotations\n" + source(name, phase), "exact-" + name, "exec"), ns)
    ns["utc"] = ns["_utc"]
    for path, record in META["unchanged_dependencies"].items():
        for name, value in record["extracts"].items():
            if hashlib.sha256(value["source"].encode()).hexdigest() != value["sha256"]:
                raise AssertionError("Dependency extract changed")
            if name == "verified_source_observation":
                proof = dict(ns)
                proof["POPULATION_CONTRACT"] = PRIMARY
                exec(compile("from __future__ import annotations\n" + value["source"], "exact-native-proof", "exec"), proof)
                ns[name] = proof[name]
            else:
                exec(compile("from __future__ import annotations\n" + value["source"], "exact-" + name, "exec"), ns)
                native[name] = ns[name]
    for name in ("wrap_primary", "verified_recovery_candle"):
        exec(compile("from __future__ import annotations\n" + source(name, phase), "exact-" + name + "-" + phase, "exec"), ns)
    return ns


def primary():
    start = "2026-01-02T14:00:00+00:00"
    end = "2026-01-02T14:15:00+00:00"
    observation = {"opened_at": start, "available_at": "2026-01-02T14:01:00+00:00",
                   "source_id": "fixture:native", "synthetic": False,
                   "open": 100., "high": 102., "low": 99., "close": 101., "volume": 20.}
    evidence = {"aggregation": PRIMARY, "source_id": observation["source_id"], "source_dataset": "FIXTURE",
                "source_coverage": {"opened_at": start, "closed_at": end, "basis": "recorded_fixture"},
                "observations": [observation]}
    return {"symbol": "TEST", "opened_at": start, "available_at": end, "availability_kind": "actual",
            "open": 100., "high": 102., "low": 99., "close": 101., "volume": 20.,
            "source_id": observation["source_id"], "source_dataset": "FIXTURE", "population_contract": PRIMARY,
            "session_id": "fixture-session", "session_open": "2026-01-02T08:00:00+00:00", "session_close": "2026-01-03T00:00:00+00:00",
            "interval_seconds": 900, "observed_constituents": 1, "source_empty_constituents": 14,
            "synthetic_constituents": 0, "synthetic": False, "no_trade": False,
            "synthetic_open": False, "synthetic_close": False,
            "native_first_opened_at": start, "native_last_opened_at": start, "evidence_json": json.dumps(evidence)}


def apply_unified(original, text, reverse=False):
    lines = text.splitlines(keepends=True)
    if lines[:2] != ["--- a/" + NAME + "\n", "+++ b/" + NAME + "\n"]:
        raise AssertionError("Unexpected patch target")
    old = original.splitlines(keepends=True); result = []; pos = 0; index = 2
    while index < len(lines):
        match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", lines[index])
        if match is None:
            raise AssertionError("Invalid hunk")
        start = int(match.group(3 if reverse else 1)) - 1
        result.extend(old[pos:start]); pos = start; index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            kind, value = lines[index][0], lines[index][1:]
            if reverse:
                kind = {" ": " ", "+": "-", "-": "+"}[kind]
            if kind in (" ", "-"):
                if old[pos] != value:
                    raise AssertionError("Patch preimage changed")
                pos += 1
            if kind in (" ", "+"):
                result.append(value)
            index += 1
    result.extend(old[pos:]); return "".join(result)


class NormalizationOnce(unittest.TestCase):
    def setUp(self):
        self.before, self.after = namespace("before"), namespace()

    def parity(self, value):
        left = self.before["wrap_primary"](value); right = self.after["wrap_primary"](value)
        self.assertEqual(left, right); self.assertEqual(list(left), list(right))
        def order(item):
            if isinstance(item, dict):
                return [(key, order(value)) for key, value in item.items()]
            if isinstance(item, list):
                return [order(value) for value in item]
            return item
        self.assertEqual(order(left), order(right))
        self.assertEqual(self.before["_json"](left), self.after["_json"](right))
        self.assertEqual(self.before["_digest"](left), self.after["_digest"](right))
        self.assertTrue(self.before["verified_recovery_candle"](left))
        self.assertTrue(self.after["verified_recovery_candle"](right))
        return right

    def rejected(self, value):
        errors = []
        for ns in (self.before, self.after):
            try:
                ns["wrap_primary"](value)
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc)))
            else:
                self.fail("Invalid fixture accepted")
        self.assertEqual(errors[0], errors[1])

    def test_01_patch_and_all_extract_hashes(self):
        self.assertEqual(hashlib.sha256(PACKAGE.with_suffix(".patch").read_bytes()).hexdigest(), META["patch_sha256"])
        for phase in ("before", "after"):
            for name in RECORD["extracts_" + phase]:
                source(name, phase)

    def test_02_unchanged_helper_ast_and_validation_prefix(self):
        for name in RECORD["extracts_before"]:
            if name != "wrap_primary":
                self.assertEqual(ast.dump(ast.parse(source(name, "before"))), ast.dump(ast.parse(source(name))))
        before = ast.parse(source("wrap_primary", "before")).body[0].body
        after = ast.parse(source("wrap_primary")).body[0].body
        boundary = next(i for i, node in enumerate(before)
                        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name) and node.value.func.id == "_session") + 1
        self.assertEqual([ast.dump(n) for n in before[:boundary]], [ast.dump(n) for n in after[:boundary]])

    def test_03_full_roundtrip_or_exact_hunk_projection(self):
        patch = PACKAGE.with_suffix(".patch").read_text(encoding="utf8")
        local = ROOT / NAME
        if local.exists():
            text = local.read_bytes().decode().replace("\r\n", "\n")
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            if fingerprint == RECORD["before_normalized_sha256"]:
                after = apply_unified(text, patch)
                self.assertEqual(hashlib.sha256(after.encode()).hexdigest(), RECORD["after_normalized_sha256"])
                self.assertEqual(apply_unified(after, patch, True), text); return
            if fingerprint == RECORD["after_normalized_sha256"]:
                before = apply_unified(text, patch, True)
                self.assertEqual(hashlib.sha256(before.encode()).hexdigest(), RECORD["before_normalized_sha256"])
                self.assertEqual(apply_unified(before, patch), text); return
        for hunk in re.split(r"^@@ .*@@\n", patch, flags=re.MULTILINE)[1:]:
            pre = "".join(line[1:] for line in hunk.splitlines(keepends=True) if line.startswith((" ", "-")))
            post = "".join(line[1:] for line in hunk.splitlines(keepends=True) if line.startswith((" ", "+")))
            self.assertIn(pre.strip(), (source("wrap_primary", "before") + "\n\n" + source("_assessment_valid", "before")).strip())
            self.assertIn(post.strip(), (source("wrap_primary") + "\n\n" + source("_assessment_valid")).strip())

    def test_04_ordinary_bytes_hash_and_key_order(self):
        self.parity(primary())

    def test_05_nested_provenance_and_unicode(self):
        value = primary(); value["extra"] = {"z": [None, False, {"x": "Δ 東京 🦆\n\t"}], "a": []}
        self.parity(value)

    def test_06_integer_nested_keys(self):
        value = primary(); value["extra"] = {1: "first", 10: "tenth", 2: "second"}
        wrapped = self.parity(value)
        self.assertEqual(list(wrapped["extra"]), ["1", "10", "2"])

    def test_07_extra_input_key_order(self):
        value = {"z": 3, **primary(), "a": {"b": 2, "a": 1}}
        wrapped = self.parity(value)
        expected = list(self.before["wrap_primary"](value))
        self.assertEqual(list(wrapped), expected)

    def test_08_scalar_interface_normalization(self):
        value = primary(); value["extra"] = {"integer": NumInt(4), "float": NumFloat(1.25), "bool": NumBool(True)}
        wrapped = self.parity(value)
        self.assertEqual(wrapped["extra"], {"integer": 4, "float": 1.25, "bool": True})

    def test_09_datetime_interface_normalization(self):
        value = primary(); value["extra"] = {"at": datetime(2026, 1, 2, tzinfo=timezone.utc), "stamp": Stamp("2026-01-02T08:00:00Z")}
        self.parity(value)

    def test_10_mapping_evidence(self):
        value = primary(); value["evidence_json"] = json.loads(value["evidence_json"])
        self.parity(value)

    def test_11_negative_zero_bytes_preserved(self):
        value = primary(); value["extra"] = -0.
        wrapped = self.parity(value)
        self.assertIn("-0.0", self.after["_json"](wrapped))

    def test_12_input_mutation_cannot_change_returned_or_evidence(self):
        value = primary(); value["extra"] = {"nested": [{"x": 1}]}
        wrapped = self.after["wrap_primary"](value); saved = self.after["_json"](wrapped)
        value["extra"]["nested"][0]["x"] = 2
        self.assertEqual(self.after["_json"](wrapped), saved)
        self.assertEqual(json.loads(wrapped["evidence_json"])["native_primary"]["extra"]["nested"][0]["x"], 1)

    def test_13_return_mutation_cannot_change_input_or_frozen_evidence(self):
        value = primary(); value["extra"] = {"nested": [{"x": 1}]}
        wrapped = self.after["wrap_primary"](value)
        wrapped["extra"]["nested"][0]["x"] = 3
        self.assertEqual(value["extra"]["nested"][0]["x"], 1)
        self.assertEqual(json.loads(wrapped["evidence_json"])["native_primary"]["extra"]["nested"][0]["x"], 1)

    def test_14_repeated_calls_do_not_share_nested_outputs(self):
        value = primary(); value["extra"] = {"nested": []}
        first = self.after["wrap_primary"](value); second = self.after["wrap_primary"](value)
        first["extra"]["nested"].append("first")
        self.assertEqual(second["extra"]["nested"], [])

    def test_15_native_proof_and_session_calls_retained(self):
        for ns in (self.before, self.after):
            calls = []; proof = ns["verified_source_observation"]; session = ns["_session"]
            def validate(value):
                calls.append("native"); return proof(value)
            def validate_session(value, opened):
                calls.append("session"); return session(value, opened)
            ns["verified_source_observation"] = validate; ns["_session"] = validate_session
            ns["wrap_primary"](primary())
            self.assertEqual(calls, ["native", "session"])

    def test_16_only_duplicate_row_plain_copy_removed(self):
        counts = []
        for ns in (self.before, self.after):
            called = []; original = ns["_plain"]
            def counted(value):
                called.append(value); return original(value)
            ns["_plain"] = counted
            ns["wrap_primary"](primary()); counts.append(len(called))
        self.assertEqual(counts, [2, 1])

    def test_17_existing_wrapper_proof_and_copy_retained(self):
        wrapped = self.parity(primary())
        for ns in (self.before, self.after):
            result = ns["wrap_primary"](wrapped)
            self.assertEqual(result, wrapped); self.assertIsNot(result, wrapped)

    def test_18_corrupt_existing_wrapper_rejected(self):
        wrapped = self.after["wrap_primary"](primary()); wrapped["close"] = 999.
        self.rejected(wrapped)

    def test_19_synthetic_primary_rejected(self):
        value = primary(); value["synthetic"] = True; self.rejected(value)

    def test_20_coverage_gap_rejected(self):
        value = primary(); proof = json.loads(value["evidence_json"])
        proof["source_coverage"]["closed_at"] = "2026-01-02T14:01:00Z"
        value["evidence_json"] = json.dumps(proof); self.rejected(value)

    def test_21_aggregate_mutation_rejected(self):
        value = primary(); value["open"] = 1000.; self.rejected(value)

    def test_22_native_time_mutation_rejected(self):
        value = primary(); value["native_first_opened_at"] = "2026-01-02T14:00:01Z"; self.rejected(value)

    def test_23_native_source_mutation_rejected(self):
        value = primary(); proof = json.loads(value["evidence_json"]); proof["observations"][0]["source_id"] = "other"
        value["evidence_json"] = json.dumps(proof); self.rejected(value)

    def test_24_exact_session_rejected(self):
        value = primary(); value["session_close"] = "2026-01-02T14:05:00Z"; self.rejected(value)

    def test_25_nonfinite_nested_extra_rejected(self):
        value = primary(); value["extra"] = {"bad": float("nan")}; self.rejected(value)

    def test_26_unsupported_nested_extra_rejected(self):
        value = primary(); value["extra"] = {"bad": object()}; self.rejected(value)

    def test_27_nonfinite_price_rejected(self):
        value = primary(); value["close"] = float("inf"); self.rejected(value)

    def test_28_native_synthetic_marker_rejected(self):
        value = primary(); proof = json.loads(value["evidence_json"]); proof["observations"][0]["is_synthetic"] = True
        value["evidence_json"] = json.dumps(proof); self.rejected(value)

    def test_29_no_network_required(self):
        saved = socket.socket.connect
        def denied(*args, **kwargs):
            raise AssertionError("Network prohibited")
        socket.socket.connect = denied
        try:
            self.parity(primary())
        finally:
            socket.socket.connect = saved

    def test_30_native_hash_text_reused_without_full_row_encode(self):
        counts = []
        outputs = []
        for ns in (self.before, self.after):
            called = []
            original = ns["_json"]
            def counted(value):
                text = original(value)
                called.append(text)
                return text
            ns["_json"] = counted
            wrapped = ns["wrap_primary"](primary())
            counts.append(len(called))
            outputs.append(wrapped)
            proof = json.loads(wrapped["evidence_json"])
            expected = original(proof["native_primary"])
            self.assertEqual(proof["native_primary_sha256"], hashlib.sha256(expected.encode()).hexdigest())
        self.assertEqual(counts, [4, 3])
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
