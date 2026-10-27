"""Offline regression for private unexpected-worker diagnostics; no live setup."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/trader_worker.py"
PACKAGE = ROOT / "docs/development/patches/trader-worker-failure-evidence.json"
NAMES = ("_record_failure", "main")


def load_worker():
    if PACKAGE.is_file():
        metadata = json.loads(PACKAGE.read_text(encoding="utf-8"))
        patch_bytes = PACKAGE.with_suffix(".patch").read_bytes()
        if hashlib.sha256(patch_bytes).hexdigest() != metadata["patch_sha256"]:
            raise AssertionError("Reviewed patch bytes differ")
    if SOURCE.is_file():
        text = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(text)
        extracts = {node.name: ast.get_source_segment(text, node) + "\n"
                    for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name in NAMES}
        if PACKAGE.is_file():
            metadata = json.loads(PACKAGE.read_text(encoding="utf-8"))
            for name in NAMES:
                if extracts[name] != metadata["extracts"][name]["source"]:
                    raise AssertionError("Installed worker differs from reviewed extract: " + name)
    else:
        metadata = json.loads(PACKAGE.read_text(encoding="utf-8"))
        extracts = {name: metadata["extracts"][name]["source"] for name in NAMES}
    if PACKAGE.is_file():
        for name, source in extracts.items():
            expected = metadata["extracts"][name]["sha256"]
            if hashlib.sha256(source.encode()).hexdigest() != expected:
                raise AssertionError("Reviewed extract hash differs: " + name)
    namespace = {"__file__": str(SOURCE), "argparse": argparse, "json": json,
                 "os": os, "Path": Path, "sys": sys}
    exec(compile("\n".join(extracts.values()), str(SOURCE), "exec"), namespace)
    return namespace


class PlannedRenewal(BaseException):
    pass


class FailureEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.worker = load_worker()
        self.request_path = self.root / "execution-17-exactturn.json"
        self.response = self.request_path.with_suffix(".response.json")
        self.diagnostic = self.request_path.with_suffix(".failure.json")
        self.request = {"role": "execution", "turn": "exact-turn", "token": "private-token",
                        "generation": 17, "response": str(self.response)}
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        self.finished = []
        self.calls = []
        self.stopped = False
        self.renewal = types.SimpleNamespace(request_path=self.request_path,
            request=self.request, envelope={"session": {"session_id": "same-manual-session"}},
            stopped=lambda: self.stopped, claim=lambda: self.calls.append("claim"),
            finish=self.finished.append)
        self.host_module = types.ModuleType("tools.trader_session_host")
        self.host_module.bind_kernel_lifetime = lambda: self.calls.append("bound")
        self.renewal_module = types.ModuleType("ml.trader_renewal")
        self.renewal_module.Renewal = lambda path: self.renewal
        self.renewal_module.PlannedRenewal = PlannedRenewal
        self.renewal_module.PLANNED_EXIT = 75
        self.renewal_module.process_birth = lambda pid: "exact-worker-birth"
        self.worker["bootstrap"] = lambda path, **kwargs: {
            "generation": "a" * 64, "runtime_id": "b" * 64}
        self.worker["run"] = lambda renewal: {"status": "COMPLETE"}
        self.module_patch = patch.dict(sys.modules, {
            "tools.trader_session_host": self.host_module,
            "ml.trader_renewal": self.renewal_module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.network_patch = patch.object(socket, "socket", side_effect=AssertionError("network prohibited"))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.environment_patch = patch.dict(os.environ)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def invoke(self):
        return self.worker["main"](["--generation", str(self.root / ("a" * 64)),
                                    "--request", str(self.request_path)])

    def fail_with(self, error):
        def fail(_):
            raise error
        self.worker["run"] = fail
        with self.assertRaises(type(error)) as raised:
            self.invoke()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.finished, [])
        self.assertFalse(self.response.exists())
        return error

    def read_diagnostic(self):
        return json.loads(self.diagnostic.read_text(encoding="utf-8"))

    def test_original_worker_error_and_native_sqlite_cause_are_retained(self):
        database = self.root / "offline.sqlite"
        first = sqlite3.connect(database, timeout=0)
        second = sqlite3.connect(database, timeout=0)
        try:
            first.execute("create table isolated (value integer)")
            first.execute("insert into isolated values (1)")
            try:
                second.execute("insert into isolated values (2)")
            except sqlite3.OperationalError as cause:
                error = RuntimeError("worker failed before handoff")
                error.__cause__ = cause
            else:
                self.fail("Expected native contention fixture")
            self.fail_with(error)
        finally:
            first.close()
            second.close()
        value = self.read_diagnostic()
        self.assertEqual(value["schema"], "trader-worker-failure-evidence-v1")
        self.assertTrue(value["diagnostic_only"])
        self.assertEqual(value["session_id"], "same-manual-session")
        self.assertEqual(value["role"], "execution")
        self.assertEqual(value["turn"], "exact-turn")
        self.assertEqual(value["token"], "private-token")
        self.assertEqual(value["generation"], 17)
        self.assertEqual(value["sealed_generation"], "a" * 64)
        self.assertEqual(value["worker_pid"], os.getpid())
        self.assertEqual(value["worker_birth"], "exact-worker-birth")
        self.assertEqual(value["request_sha256"], hashlib.sha256(self.request_path.read_bytes()).hexdigest())
        self.assertEqual(value["exception_chain"][0]["message"], "worker failed before handoff")
        native = value["exception_chain"][1]
        self.assertEqual(native["sqlite_errorcode"], sqlite3.SQLITE_BUSY)
        self.assertEqual(native["sqlite_errorname"], "SQLITE_BUSY")
        self.assertEqual(native["relation"], "cause")
        self.assertTrue(native["frames"])
        self.assertNotIn("locals", json.dumps(value))
        self.assertEqual(self.calls, ["bound", "claim"])

    def test_unrequested_interruption_retains_error_and_does_not_finish(self):
        self.fail_with(InterruptedError("unexpected interrupt"))
        self.assertEqual(self.read_diagnostic()["exception_chain"][0]["message"], "unexpected interrupt")

    def test_unrequested_keyboard_interrupt_keeps_original_raise(self):
        self.fail_with(KeyboardInterrupt("unexpected interrupt"))
        self.assertEqual(self.read_diagnostic()["exception_chain"][0]["type"], "builtins.KeyboardInterrupt")

    def test_manual_interrupt_still_finishes_stopped_without_diagnostic(self):
        self.stopped = True
        self.worker["run"] = lambda _: (_ for _ in ()).throw(InterruptedError("manual stop"))
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.finished, [{"status": "STOPPED"}])
        self.assertFalse(self.diagnostic.exists())

    def test_planned_renewal_preserves_exit75_without_terminal_evidence(self):
        def renew(_):
            raise PlannedRenewal()
        self.worker["run"] = renew
        self.assertEqual(self.invoke(), 75)
        self.assertEqual(self.finished, [])
        self.assertFalse(self.diagnostic.exists())

    def test_completed_worker_keeps_original_result(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.finished, [{"status": "COMPLETE"}])
        self.assertFalse(self.diagnostic.exists())

    def test_verify_only_never_claims_or_creates_diagnostic(self):
        self.assertEqual(self.worker["main"](["--generation", str(self.root), "--verify-only"]), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.diagnostic.exists())

    def test_existing_original_diagnostic_is_never_replaced(self):
        original = b"original retained evidence\x00"
        self.diagnostic.write_bytes(original)
        self.fail_with(RuntimeError("later failure"))
        self.assertEqual(self.diagnostic.read_bytes(), original)

    def test_unwritable_diagnostic_does_not_mask_original_error(self):
        self.diagnostic.mkdir()
        self.fail_with(ValueError("exact original error"))
        self.assertTrue(self.diagnostic.is_dir())

    def test_unserializable_identity_does_not_mask_original_error(self):
        self.request["token"] = object()
        self.fail_with(ValueError("exact original error"))
        self.assertFalse(self.diagnostic.exists())

    def test_cycle_long_messages_frames_and_broken_string_are_bounded(self):
        class UnprintableError(RuntimeError):
            def __str__(self):
                raise RuntimeError("broken repr")
        error = UnprintableError()
        other = RuntimeError("x" * 50000)
        error.__cause__ = other
        other.__cause__ = error
        self.fail_with(error)
        value = self.read_diagnostic()
        self.assertEqual(len(value["exception_chain"]), 2)
        self.assertTrue(value["chain_cycle"])
        self.assertEqual(value["exception_chain"][0]["message"], "<unprintable exception>")
        self.assertEqual(len(value["exception_chain"][1]["message"]), 2000)
        self.assertTrue(all(len(v["frames"]) <= 8 for v in value["exception_chain"]))
        self.assertLess(self.diagnostic.stat().st_size, 50000)

    def test_long_chain_is_capped_and_os_codes_are_retained(self):
        error = OSError(13, "fixture access denied")
        error.winerror = 5
        previous = error
        for number in range(20):
            previous.__cause__ = RuntimeError(str(number))
            previous = previous.__cause__
        self.fail_with(error)
        value = self.read_diagnostic()
        self.assertEqual(len(value["exception_chain"]), 8)
        self.assertTrue(value["chain_truncated"])
        self.assertFalse(value["chain_cycle"])
        self.assertEqual(value["exception_chain"][0]["errno"], 13)
        self.assertEqual(value["exception_chain"][0]["winerror"], 5)

    def test_suppressed_context_is_not_reported_as_a_cause(self):
        error = ValueError("visible error")
        error.__context__ = RuntimeError("suppressed context")
        error.__suppress_context__ = True
        self.fail_with(error)
        self.assertEqual(len(self.read_diagnostic()["exception_chain"]), 1)

    def test_unsuppressed_context_is_retained(self):
        error = ValueError("visible error")
        error.__context__ = RuntimeError("original context")
        self.fail_with(error)
        self.assertEqual(self.read_diagnostic()["exception_chain"][1]["relation"], "context")

    def test_claim_failure_does_not_publish_unadmitted_worker_evidence(self):
        def bad_claim():
            raise RuntimeError("claim refused")
        self.renewal.claim = bad_claim
        with self.assertRaisesRegex(RuntimeError, "claim refused"):
            self.invoke()
        self.assertFalse(self.diagnostic.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
