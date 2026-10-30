"""Portable offline contract tests for the exact data-only catch-up helper.

Load the shipped helper itself. Stub only its existing owner, source-binding,
refresh and report contracts; no production data/model/provider modules load.
The primary pytest suite separately exercises real local binding and redaction.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch


MANIFEST = "docs/development/patches/intraday-history-catchup.json"
DIRECT = {"tools/intraday_history_catchup.py", "tests/test_intraday_history_catchup.py",
          "tests/check_intraday_history_catchup_package.py"}
ROOT = Path(__file__).resolve().parents[1]


class CatchupContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="intraday-catchup-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = {name: self.root/name for name in ("datastore", "profile", "watchlist", "config")}
        self.args["config"].write_text(json.dumps({"source_dataset": "FIXTURE.DATASET", "selection": {"retain": 1}}))
        self.args["profile"].write_text("fixture-profile-original")
        self.args["watchlist"].write_text("ABC\nXYZ\n")
        self.originals = {name: path.read_bytes() for name, path in self.args.items() if name != "datastore"}
        self.calls, self.depth, self.busy = [], 0, False
        self.response = {symbol: {"status": "refreshed", "reused_rows": 4} for symbol in ("ABC", "XYZ")}
        self.membership_error = False
        modules = {}
        for name in ("datafetching", "datafetching.runtime_lock", "ml", "ml.intraday",
                     "ml.intraday.prediction", "ml.intraday.runtime", "ml.intraday.supervisor"):
            module = modules[name] = ModuleType(name)
            module.__path__ = []
        class ContractError(ValueError):
            pass
        self.contract_error = ContractError
        modules["ml.intraday.prediction"].ContractError = ContractError

        def local_symbols(profile, watchlist, datastore):
            self.assertEqual((profile, watchlist, datastore),
                             (self.args["profile"], self.args["watchlist"], self.args["datastore"]))
            self.calls.append("binding")
            if self.membership_error:
                raise ContractError("Local membership differs")
            return "Atlas", ("ABC", "XYZ")

        @contextmanager
        def owner(path, *, process_name):
            self.assertEqual(path, self.args["datastore"] / "locks/intraday-model-supervision.lock")
            self.assertEqual(process_name, "local 15m history catch-up")
            self.calls.append("lock")
            if self.busy:
                raise RuntimeError("Existing owner holds the fixture lock")
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
                self.calls.append("unlock")

        def atomic_json(path, value):
            self.assertEqual(self.depth, 1, "Reports must remain inside the source owner's lock")
            self.calls.append("report:" + value["status"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, sort_keys=True))

        def refresh(datastore, symbols, *, source_dataset):
            self.assertEqual(self.depth, 1)
            self.assertEqual(datastore, self.args["datastore"])
            self.assertEqual(symbols, ("ABC", "XYZ"))
            self.calls.append(("refresh", source_dataset))
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        def failure(exc):
            self.assertEqual(self.depth, 1)
            self.calls.append("redaction")
            return {"failure_type": type(exc).__name__, "message": "redacted fixture"}

        def no_models(*a, **k):
            self.fail("Data catch-up cannot fit/select models or activate a trader")

        modules["datafetching.runtime_lock"].exclusive_runtime_lock = owner
        modules["ml.intraday.runtime"].local_symbols = local_symbols
        modules["ml.intraday.runtime"].ShadowRunner = no_models
        supervisor = modules["ml.intraday.supervisor"]
        supervisor._atomic_json, supervisor._failure, supervisor.refresh_ohlcv = atomic_json, failure, refresh
        supervisor.supervise = no_models
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("reviewed_history_catchup", ROOT/"tools/intraday_history_catchup.py")
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def test_owner_source_and_report_order_preserve_originals(self):
        result, report_path = self.helper.catch_up(**self.args)
        self.assertEqual(self.calls, ["binding", "lock", "report:running", ("refresh", "FIXTURE.DATASET"),
                                     "report:complete", "unlock"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["actor"], "Atlas")
        self.assertEqual(result["symbols"], self.response)
        self.assertFalse(result["model_fitting"])
        self.assertFalse(result["trader_activation"])
        self.assertEqual(json.loads(report_path.read_text()), result)
        self.assertEqual(report_path.parent, self.args["datastore"] / "intraday/history-catchup")
        for name, original in self.originals.items():
            self.assertEqual(self.args[name].read_bytes(), original)

    def test_missing_or_failed_symbol_is_incomplete(self):
        for evidence in ({}, {"ABC": {"status": "refreshed"}},
                         {"ABC": {"status": "refreshed"}, "XYZ": {"status": "failed"}},
                         {"ABC": {"status": "refreshed"}, "XYZ": {"status": "refreshed"}, "EXTRA": {"status": "refreshed"}}):
            with self.subTest(evidence=evidence):
                self.response = evidence
                result, path = self.helper.catch_up(**self.args)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(json.loads(path.read_text())["status"], "incomplete")

    def test_refresh_error_uses_existing_redactor_and_finishes_report(self):
        self.response = RuntimeError("private-fixture-token")
        result, path = self.helper.catch_up(**self.args)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["failure_type"], "RuntimeError")
        self.assertIn("completed_at", result)
        self.assertIn("redaction", self.calls)
        self.assertNotIn("private-fixture-token", path.read_text())
        self.assertEqual(self.calls[-1], "unlock")

    def test_busy_owner_never_refreshes_or_writes_a_report(self):
        self.busy = True
        with self.assertRaisesRegex(RuntimeError, "Existing owner"):
            self.helper.catch_up(**self.args)
        self.assertEqual(self.calls, ["binding", "lock"])
        self.assertFalse(self.args["datastore"].exists())

    def test_membership_failure_never_acquires_owner_or_fetches(self):
        self.membership_error = True
        with self.assertRaisesRegex(self.contract_error, "membership"):
            self.helper.catch_up(**self.args)
        self.assertEqual(self.calls, ["binding"])

    def test_invalid_source_binding_is_not_substituted(self):
        for value in ("", None, [], 12):
            with self.subTest(dataset=value):
                self.calls.clear()
                self.args["config"].write_text(json.dumps({"source_dataset": value}))
                with self.assertRaisesRegex(self.contract_error, "nonempty string"):
                    self.helper.catch_up(**self.args)
                self.assertEqual(self.calls, ["binding"])

    def test_absent_source_keeps_existing_supervisor_default(self):
        self.args["config"].write_text("{}")
        result, _ = self.helper.catch_up(**self.args)
        self.assertIn(("refresh", "EQUS.MINI"), self.calls)
        self.assertEqual(result["source_dataset"], "EQUS.MINI")

    def test_cli_reports_durable_result_and_accurate_exit(self):
        log_dir = self.root / "requested-log"
        arguments = [value for key, path in self.args.items() for value in ("--"+key, str(path))]
        arguments += ["--log-dir", str(log_dir)]
        for response, expected in ((self.response, 0), ({}, 1), (RuntimeError("fixture"), 1)):
            with self.subTest(expected=expected, response=response):
                self.response = response
                output = io.StringIO()
                with redirect_stdout(output):
                    code = self.helper.main(arguments)
                message = json.loads(output.getvalue())
                self.assertEqual(code, expected)
                self.assertEqual(Path(message["report"]).parent, log_dir)
                self.assertEqual(json.loads(Path(message["report"]).read_text())["status"], message["status"])
        self.assertEqual(len(list(log_dir.glob("*.json"))), 3)


def main():
    manifest = json.loads((ROOT/MANIFEST).read_text("utf-8"))
    if manifest.get("schema") != "intraday-history-catchup-package-v1" or set(manifest.get("direct_files", {})) != DIRECT:
        raise ValueError("Unexpected history catch-up package inventory")
    for name, checksum in manifest["direct_files"].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != checksum:
            raise ValueError("Exact history catch-up package bytes differ: " + name)
    attempts = []
    def no_network(*a, **k):
        attempts.append(True)
        raise AssertionError("History catch-up package tests are offline")
    socket.create_connection = socket.getaddrinfo = no_network
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = no_network
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CatchupContractTests))
    if attempts:
        raise AssertionError("History catch-up tests attempted network access")
    print("HISTORY_CATCHUP_PACKAGE=" + json.dumps(dict(tests_run=result.testsRun, successful=result.wasSuccessful(),
        skipped=len(result.skipped), network_attempts=0, source="exact_shipped_helper",
        dependency_contracts="mocked_owner_binding_refresh_redaction_atomic_report", model_calls=0,
        provider_calls=0, trader_activations=0), sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
