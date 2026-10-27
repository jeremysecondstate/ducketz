"""Portable offline exact-method regression; isolated SQLite and no app imports."""
from __future__ import annotations

import ast
import copy
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import socket
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/development/patches/intraday-intention-encode-once.json"
META = json.loads(PACKAGE.read_text(encoding="utf8"))
NAME = "ml/intraday/intentions.py"
RECORD = META["sources"][NAME]


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ContractError(ValueError):
    pass


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stock_direction(value):
    return "BULLISH" if value >= .5001 else "BEARISH" if value <= .4999 else "HOLD"


def quantity(value):
    if type(value) is not int or value < 0:
        raise ContractError("Quantity must be a nonnegative integer")
    return value


def extract(phase):
    value = RECORD["extracts_" + phase]["save_sizing"]
    if hashlib.sha256(value["source"].encode()).hexdigest() != value["sha256"]:
        raise AssertionError("Exact method extract changed")
    return value["source"]


def apply_unified(original, text, reverse=False):
    lines = text.splitlines(keepends=True)
    if lines[:2] != ["--- a/" + NAME + "\n", "+++ b/" + NAME + "\n"]:
        raise AssertionError("Unexpected patch target")
    old = original.splitlines(keepends=True)
    output, position, index = [], 0, 2
    while index < len(lines):
        match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", lines[index])
        if match is None:
            raise AssertionError("Invalid hunk")
        start = int(match.group(3 if reverse else 1)) - 1
        if start < position:
            raise AssertionError("Overlapping hunks")
        output.extend(old[position:start]); position = start; index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            kind, value = lines[index][0], lines[index][1:]
            if reverse:
                kind = {" ": " ", "+": "-", "-": "+"}[kind]
            if kind in (" ", "-"):
                if old[position] != value:
                    raise AssertionError("Exact patch preimage differs")
                position += 1
            if kind in (" ", "+"):
                output.append(value)
            index += 1
    output.extend(old[position:])
    return "".join(output)


class Lock:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.trace.append("lock_enter")
        self.store.locked = True
        if self.store.on_lock:
            self.store.on_lock()

    def __exit__(self, *args):
        self.store.trace.append("lock_exit")
        self.store.locked = False


class DB:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, args=()):
        self.store.trace.append("sql:" + sql.split()[0])
        if sql == "BEGIN IMMEDIATE":
            self.store.writer = True
        if self.store.fail_sql and sql.startswith(self.store.fail_sql):
            raise sqlite3.OperationalError("fixture writer failure")
        if sql.startswith("INSERT"):
            self.store.inserts.append(args)
        return self.store.db.execute(sql, args)


class Store:
    def __init__(self):
        self.trace, self.inserts = [], []
        self.locked = self.writer = False
        self.on_lock = None
        self.fail_sql = None
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE intraday_intentions(forecast_id TEXT PRIMARY KEY,id TEXT,payload TEXT)")
        self.db.commit()
        self._execution_forecast_lock = Lock(self)

    @contextmanager
    def connect(self):
        self.trace.append("connect_enter")
        try:
            with self.db:
                yield DB(self)
        finally:
            self.writer = False
            self.trace.append("connect_exit")

    def close(self):
        self.db.close()


class Result:
    def __init__(self, value, corrupt=False):
        self.raw = encoded(value)
        self.result_id = digest(value)
        if corrupt:
            self.raw = self.raw.replace('"desired_quantity":4', '"desired_quantity":5')

    def payload(self):
        value = json.loads(self.raw)
        if digest(value) != self.result_id:
            raise ContractError("Sizing result evidence changed")
        return value


class Owner:
    def __init__(self, store, forecast):
        self.store = store
        self.forecast = copy.deepcopy(forecast)
        self.records = []
        self.fail_preflight = self.fail_recheck = False
        self.fail_record = False

    def get(self, forecast_id):
        self.store.trace.append("get")
        return self._value(self.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions WHERE forecast_id=?", (forecast_id,)).fetchone())

    def _value(self, row):
        if row is None:
            return None
        value = json.loads(row[2])
        if digest(value) != row[1]:
            raise ContractError("Intention evidence changed")
        return value

    def _ordinary_forecast_preflight(self, forecast):
        self.store.trace.append("preflight")
        if self.fail_preflight or forecast != self.forecast:
            raise ContractError("Original forecast changed")
        return "marker", copy.deepcopy(self.forecast)

    def _ordinary_forecast_recheck(self, db, forecast, marker, original):
        self.store.trace.append("recheck")
        if self.fail_recheck or forecast != original or marker != "marker":
            raise ContractError("Original forecast changed")

    def record(self, forecast, **kwargs):
        self.store.trace.append("record")
        if self.fail_record:
            raise sqlite3.OperationalError("fixture record failure")
        self.records.append((copy.deepcopy(forecast), copy.deepcopy(kwargs)))
        return {"recorded": kwargs["event_key"]}


def fixture(phase="after", *, probability=.6, side="BUY", status="sized", padding=0):
    store = Store()
    forecast = {"forecast_id": "fixture-original", "symbol": "AAPL", "issued_at": "2026-01-02T10:00:00Z", "probability_not_down": probability, "extra": {"original": [1, 2]}}
    value = {"forecast_id": forecast["forecast_id"], "symbol": forecast["symbol"], "side": side,
             "calculated_at": "2026-01-02T10:01:00Z", "status": status, "reason": "missing_volume",
             "desired_quantity": 4, "provenance": {"original": "Δ\n" + "x" * padding}}
    owner = Owner(store, forecast)
    calls = []
    def observed(value):
        calls.append({"locked": store.locked, "writer": store.writer, "size": len(encoded(value))})
        return encoded(value)
    namespace = {"ContractError": ContractError, "stock_direction": stock_direction, "instant": instant,
                 "quantity": quantity, "digest": lambda v: hashlib.sha256(observed(v).encode()).hexdigest(),
                 "encoded": observed, "hashlib": hashlib, "VERSION": "intraday-independent-intentions-v1"}
    exec(compile(extract(phase), "exact-" + phase + "-save_sizing", "exec"), namespace)
    return owner, forecast, value, namespace["save_sizing"], calls


class EncodeOnce(unittest.TestCase):
    def run_case(self, phase="after", **kwargs):
        owner, forecast, value, method, calls = fixture(phase, **kwargs)
        self.addCleanup(owner.store.close)
        returned = method(owner, forecast, Result(value))
        row = owner.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone()
        return owner, forecast, value, method, calls, returned, row

    def test_01_patch_and_extract_fingerprints(self):
        self.assertEqual(hashlib.sha256(PACKAGE.with_suffix(".patch").read_bytes()).hexdigest(), META["patch_sha256"])
        extract("before"); extract("after")

    def test_02_exact_minimal_method_delta(self):
        expected = extract("before").replace("    key = digest(payload)\n", "    canonical = encoded(payload)\n    key = hashlib.sha256(canonical.encode()).hexdigest()\n")
        expected = expected.replace('(forecast["forecast_id"], key, encoded(payload)))', '(forecast["forecast_id"], key, canonical))')
        self.assertEqual(extract("after"), expected)
        self.assertEqual(ast.dump(ast.parse(extract("after"))), ast.dump(ast.parse(expected)))

    def test_03_full_local_patch_roundtrip_or_hunk_projection(self):
        path = ROOT / NAME
        patch = PACKAGE.with_suffix(".patch").read_text(encoding="utf8")
        if path.exists():
            local = path.read_bytes().decode().replace("\r\n", "\n")
            current = hashlib.sha256(local.encode()).hexdigest()
            if current == RECORD["before_normalized_sha256"]:
                result = apply_unified(local, patch)
                self.assertEqual(hashlib.sha256(result.encode()).hexdigest(), RECORD["after_normalized_sha256"])
                self.assertEqual(apply_unified(result, patch, True), local)
                return
            if current == RECORD["after_normalized_sha256"]:
                result = apply_unified(local, patch, True)
                self.assertEqual(hashlib.sha256(result.encode()).hexdigest(), RECORD["before_normalized_sha256"])
                self.assertEqual(apply_unified(result, patch), local)
                return
        # Common source may have different local composition. Check every hunk
        # preimage/postimage sequence against exact reviewed method text.
        for hunk in re.split(r"^@@ .*@@\n", patch, flags=re.MULTILINE)[1:]:
            pre = "".join(line[1:] for line in hunk.splitlines(keepends=True) if line.startswith((" ", "-")))
            post = "".join(line[1:] for line in hunk.splitlines(keepends=True) if line.startswith((" ", "+")))
            self.assertIn(pre.strip(), ("    " + extract("before").replace("\n", "\n    ")).strip())
            self.assertIn(post.strip(), ("    " + extract("after").replace("\n", "\n    ")).strip())

    def test_04_buy_result_and_exact_insert_parity(self):
        old = self.run_case("before"); new = self.run_case()
        self.assertEqual(old[5:], new[5:])
        self.assertEqual(old[0].records, new[0].records)
        self.assertEqual(old[0].store.inserts, new[0].store.inserts)

    def test_05_sell_parity(self):
        old = self.run_case("before", probability=.4, side="SELL")
        new = self.run_case(probability=.4, side="SELL")
        self.assertEqual(old[5:], new[5:])

    def test_06_inclusive_buy_cutoff(self):
        self.assertEqual(self.run_case(probability=.5001)[5]["side"], "BUY")

    def test_07_inclusive_sell_cutoff(self):
        self.assertEqual(self.run_case(probability=.4999, side="SELL")[5]["side"], "SELL")

    def test_08_one_full_encode_before_writer(self):
        old = self.run_case("before", padding=1000000)
        new = self.run_case(padding=1000000)
        self.assertEqual(len(old[4]), 2); self.assertEqual(len(new[4]), 1)
        self.assertFalse(new[4][0]["locked"]); self.assertFalse(new[4][0]["writer"])
        self.assertTrue(old[4][1]["writer"])
        self.assertEqual(old[5:], new[5:])

    def test_09_exact_insert_hash_matches_bytes(self):
        row = self.run_case()[6]
        self.assertEqual(row[1], hashlib.sha256(row[2].encode()).hexdigest())

    def test_10_matching_old_row_retry(self):
        owner, forecast, value, method, calls, first, row = self.run_case()
        self.assertEqual(method(owner, forecast, Result(value)), first)
        self.assertEqual(owner.store.db.execute("SELECT COUNT(*) FROM intraday_intentions").fetchone()[0], 1)
        self.assertEqual(owner.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone(), row)
        self.assertEqual(len(owner.records), 2)

    def test_11_conflicting_old_quantity_rejected(self):
        owner, forecast, value, method, calls, first, row = self.run_case()
        value["desired_quantity"] += 1
        with self.assertRaisesRegex(ContractError, "immutable"):
            method(owner, forecast, Result(value))
        self.assertEqual(owner.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone(), row)

    def test_12_conflicting_old_evidence_rejected(self):
        owner, forecast, value, method, calls, first, row = self.run_case()
        value["provenance"]["original"] += "changed"
        with self.assertRaisesRegex(ContractError, "immutable"):
            method(owner, forecast, Result(value))
        self.assertEqual(len(owner.records), 1)

    def test_13_unavailable_bypasses_writer_and_encode(self):
        old = self.run_case("before", status="unavailable")
        new = self.run_case(status="unavailable")
        self.assertEqual(old[5:], new[5:]); self.assertEqual(new[4], [])
        self.assertNotIn("lock_enter", new[0].store.trace)
        self.assertEqual(new[0].records[0][1]["kind"], "sizing_unavailable")

    def test_14_hold_sized_rejected(self):
        owner, forecast, value, method, calls = fixture(probability=.5, side="HOLD")
        self.addCleanup(owner.store.close)
        with self.assertRaisesRegex(ContractError, "neutral"):
            method(owner, forecast, Result(value))
        self.assertEqual(calls, [])

    def reject_value(self, mutation, pattern):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close); mutation(value)
        with self.assertRaisesRegex(ContractError, pattern):
            method(owner, forecast, Result(value))
        self.assertEqual(owner.store.inserts, [])

    def test_15_wrong_forecast_rejected(self):
        self.reject_value(lambda v: v.update(forecast_id="different"), "another forecast")

    def test_16_wrong_symbol_rejected(self):
        self.reject_value(lambda v: v.update(symbol="MU"), "another forecast")

    def test_17_wrong_direction_rejected(self):
        self.reject_value(lambda v: v.update(side="SELL"), "direction or availability")

    def test_18_early_calculated_at_rejected(self):
        self.reject_value(lambda v: v.update(calculated_at="2026-01-02T09:59:00Z"), "direction or availability")

    def test_19_corrupt_result_rejected(self):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close)
        with self.assertRaisesRegex(ContractError, "Sizing result evidence changed"):
            method(owner, forecast, Result(value, corrupt=True))
        self.assertEqual(calls, []); self.assertEqual(owner.store.trace, [])

    def test_20_preflight_failure_prevents_insert(self):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close); owner.fail_preflight = True
        with self.assertRaisesRegex(ContractError, "Original forecast changed"):
            method(owner, forecast, Result(value))
        self.assertEqual(owner.store.inserts, []); self.assertNotIn("connect_enter", owner.store.trace)

    def test_21_recheck_failure_rolls_back(self):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close); owner.fail_recheck = True
        with self.assertRaisesRegex(ContractError, "Original forecast changed"):
            method(owner, forecast, Result(value))
        self.assertEqual(owner.store.inserts, []); self.assertEqual(owner.records, [])

    def test_22_forecast_nested_mutation_at_lock_rejected(self):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close)
        owner.store.on_lock = lambda: forecast["extra"]["original"].append(3)
        with self.assertRaisesRegex(ContractError, "Original forecast changed"):
            method(owner, forecast, Result(value))
        self.assertEqual(owner.store.inserts, [])

    def test_23_ordering_preserved(self):
        old = self.run_case("before")[0].store.trace
        new = self.run_case()[0].store.trace
        self.assertEqual(old, new)
        self.assertLess(new.index("preflight"), new.index("sql:BEGIN"))
        self.assertLess(new.index("sql:BEGIN"), new.index("recheck"))
        self.assertLess(new.index("recheck"), new.index("sql:INSERT"))
        self.assertLess(new.index("connect_exit"), new.index("record"))

    def test_24_writer_error_preserved(self):
        for phase in ("before", "after"):
            owner, forecast, value, method, calls = fixture(phase)
            self.addCleanup(owner.store.close); owner.store.fail_sql = "INSERT"
            with self.assertRaisesRegex(sqlite3.OperationalError, "fixture writer failure"):
                method(owner, forecast, Result(value))
            self.assertEqual(owner.records, [])
            self.assertEqual(owner.store.db.execute("SELECT COUNT(*) FROM intraday_intentions").fetchone()[0], 0)

    def test_25_record_error_retains_committed_row_for_retry(self):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close); owner.fail_record = True
        with self.assertRaisesRegex(sqlite3.OperationalError, "fixture record failure"):
            method(owner, forecast, Result(value))
        row = owner.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone()
        self.assertIsNotNone(row)
        owner.fail_record = False
        method(owner, forecast, Result(value))
        self.assertEqual(owner.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone(), row)

    def test_26_input_and_return_mutation_do_not_change_saved_bytes(self):
        owner, forecast, value, method, calls, returned, row = self.run_case()
        value["provenance"]["original"] += "input_changed"
        returned["sizing"]["provenance"]["original"] += "return_changed"
        self.assertEqual(owner.store.db.execute("SELECT forecast_id,id,payload FROM intraday_intentions").fetchone(), row)

    def test_27_no_network_required(self):
        original = socket.socket.connect
        def denied(*args, **kwargs):
            raise AssertionError("Network prohibited")
        socket.socket.connect = denied
        try:
            self.run_case(padding=4000000)
        finally:
            socket.socket.connect = original

    def test_28_nonfinite_encode_error_precedes_writer(self):
        owner, forecast, value, method, calls = fixture()
        self.addCleanup(owner.store.close)
        class NonfiniteResult:
            result_id = "fixture"
            def payload(self):
                output = copy.deepcopy(value); output["extra"] = float("nan"); return output
        with self.assertRaises(ValueError):
            method(owner, forecast, NonfiniteResult())
        self.assertNotIn("lock_enter", owner.store.trace)


if __name__ == "__main__":
    unittest.main()
