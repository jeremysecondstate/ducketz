"""Portable exact packaged controller behavior; no real process or live service."""
from __future__ import annotations
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/development/patches/trader-initial-controller-relay.json"
metadata = json.loads(PACKAGE.read_text())


class ProtocolFixture:
    def __init__(self, root, *, scenario="pristine", stop_phase=None):
        self.root, self.scenario, self.stop_phase = root, scenario, stop_phase
        self.starts, self.transfers, self.phases = [], [], []
        self.initialized = False
        self.rolled_back = False
        self.ticks = 0
        self.host = ModuleType("portable_initial_controller")
        self.host.__dict__.update(Path=Path, datetime=datetime, timezone=timezone,
            hashlib=hashlib, json=json, os=os, threading=threading, uuid=uuid,
            PROTOCOL=metadata["protocol"], PLANNED_EXIT=75, HOST_SHA256=metadata["after_sha256"])
        self.host.birth = self.host._diagnostic_birth = lambda pid: "fixture-birth"
        self.host.signal = SimpleNamespace(SIGINT=2, SIGBREAK=21, SIGTERM=15,
                                          signal=lambda *args: None)
        self.host.time = SimpleNamespace(monotonic=lambda: self.ticks / 20, sleep=self.sleep)
        self.host.subprocess = SimpleNamespace(Popen=self.popen)
        self.host.wait_for_controller_relay = self.relay
        self.host.intraday_status = lambda envelope, status, **extra: dict(envelope["session"], status=status, **extra)
        self.host.guarded_initial_control = self.initial_control
        for name in ("write_json", "read_stop_reason", "run_protocol"):
            item = metadata["extracts"][name]
            assert hashlib.sha256(item["source"].encode()).hexdigest() == item["sha256"]
            exec(compile(item["source"], str(PACKAGE) + ":" + name, "exec"), self.host.__dict__)

    def initial_control(self, envelope, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return True

    def sleep(self, seconds):
        self.ticks += 1
        if self.ticks > 100:
            raise AssertionError("Offline protocol did not quiesce")

    def command(self, role, request):
        return [role, str(request)]

    def popen(self, argv, **kwargs):
        role, request_path = argv
        request = json.loads(Path(request_path).read_text())
        self.starts.append((role, request))
        if role == "initialize":
            result = {"session": {"session_id": "same-original-session",
                "started_at": "2026-10-28T10:55:00+00:00", "close_at": "2026-10-29T00:00:00+00:00",
                "order_identity": "unchanged-original-order"}, "roles": ["forecast", "execution"]}
            disposition, code = "COMPLETED", 0
            self.initialized = True
        elif self.scenario == "postdispatch" and role == "execution":
            result, disposition, code = None, "PLANNED_RENEWAL", 75
        else:
            result, disposition, code = {"status": "COMPLETED"}, "COMPLETED", 0
        response = {key: request[key] for key in ("token", "turn", "role")}
        response.update(protocol=metadata["protocol"], disposition=disposition)
        if disposition == "PLANNED_RENEWAL":
            response.update(state={"count": request["generation"]}, next_poll_at="2026-10-28T11:00:00+00:00")
        else:
            response["result"] = result
        self.host.write_json(request["response"], response)
        return SimpleNamespace(pid=1000 + len(self.starts), poll=lambda: code, wait=lambda: code,
                               terminate=lambda: None)

    def selector(self):
        if self.rolled_back:
            return None
        if self.scenario == "postdispatch" and not any(role == "execution" for role, _ in self.starts):
            return None
        return {"host_sha256": metadata["after_sha256"], "runtime_changed": True, "command": ["never-run"]}

    def phase(self, name, role):
        self.phases.append((name, role))
        if self.scenario == "rollback" and name == "controller_draining":
            self.rolled_back = True
        if name == self.stop_phase:
            self.host.write_json(self.root / "session/stop.json", {"reason": "MANUAL_STOP"})

    def relay(self, path, payload, command, **kwargs):
        self.transfers.append(payload)
        envelope = json.loads(Path(payload["envelope"]).read_text())
        return {"disposition": "SESSION_COMPLETE", "session_id": envelope["session"]["session_id"],
                "generations": payload["generations"], "completed_roles": payload["done"]}

    def run(self):
        envelope = {"trader": "intraday", "datastore": str(self.root), "check_controls": False,
                    "session": {"session_id": "uninitialized"}, "roles": []}
        code = self.host.run_protocol(directory=self.root / "session", envelope=envelope,
            command=self.command, relay_selector=self.selector, phase=self.phase)
        self.envelope = json.loads((self.root / "session/session.json").read_text())
        self.outcome = json.loads((self.root / "session/outcome.json").read_text())
        return code


class InitialControllerRelayTests(unittest.TestCase):
    def test_pristine_session_relays_without_any_operating_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = ProtocolFixture(Path(temporary))
            self.assertEqual(f.run(), 0)
            self.assertEqual([role for role, _ in f.starts], ["initialize"])
            self.assertEqual(len(f.transfers), 1)
            transfer = f.transfers[0]
            self.assertEqual(transfer["generations"], {"initialize": 1})
            self.assertEqual(transfer["done"], ["initialize"])
            self.assertEqual(transfer["token"], f.envelope["token"])
            self.assertEqual(f.envelope["session"]["session_id"], "same-original-session")
            self.assertEqual(f.envelope["session"]["close_at"], "2026-10-29T00:00:00+00:00")
            self.assertEqual(f.envelope["session"]["order_identity"], "unchanged-original-order")

    def test_postdispatch_controller_retains_one_final_execution_drain(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = ProtocolFixture(Path(temporary), scenario="postdispatch")
            self.assertEqual(f.run(), 0)
            self.assertEqual([role for role, _ in f.starts], ["initialize", "forecast", "execution", "execution"])
            self.assertEqual(len(f.transfers), 1)
            self.assertEqual(f.transfers[0]["generations"], {"initialize": 1, "forecast": 1, "execution": 2})
            self.assertEqual(f.transfers[0]["states"]["execution"]["state"], {"count": 2})

    def test_stop_before_initial_handoff_prevents_all_operating_workers(self):
        for phase in ("controller_draining", "controller_handoff"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                f = ProtocolFixture(Path(temporary), stop_phase=phase)
                self.assertEqual(f.run(), 0)
                self.assertEqual([role for role, _ in f.starts], ["initialize"])
                self.assertEqual(f.transfers, [])
                self.assertEqual(f.outcome["disposition"], "MANUAL_STOP")

    def test_rollback_restores_all_original_role_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = ProtocolFixture(Path(temporary), scenario="rollback")
            self.assertEqual(f.run(), 0)
            self.assertEqual([role for role, _ in f.starts], ["initialize", "forecast", "execution"])
            self.assertEqual(f.transfers, [])
            self.assertEqual(f.outcome["disposition"], "SESSION_COMPLETE")

    def test_package_hashes_and_full_local_consumer_extracts(self):
        raw = PACKAGE.with_suffix(".patch").read_bytes()
        self.assertEqual(metadata["schema"], "trader-initial-controller-relay-patch-v1")
        self.assertEqual(metadata["source"], "tools/trader_session_host.py")
        self.assertEqual(metadata["patch_sha256"], hashlib.sha256(raw).hexdigest())
        files = metadata["files"]
        self.assertEqual([line for line in raw.decode().splitlines() if line.startswith("--- ")],
                         ["--- a/" + item["path"] for item in files])
        for item in files:
            local = ROOT / item["path"]
            if local.exists():
                self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), item["after_sha256"])
        for name, item in metadata["extracts"].items():
            self.assertEqual(hashlib.sha256(item["source"].encode()).hexdigest(), item["sha256"])
            local = ROOT / item["source_path"]
            if local.exists():
                source = local.read_text()
                functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
                self.assertEqual(ast.get_source_segment(source, functions[name]) + "\n", item["source"])


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--worker-diagnostics"]:
        import pytest
        parent = ROOT / "scratch"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="initial-controller-tests-", dir=parent) as temporary:
            test_root = Path(temporary).resolve()
            assert test_root.is_relative_to(parent.resolve())
            raise SystemExit(pytest.main(["tests/test_trader_worker_loaded_evidence.py",
                "tests/test_trader_initial_controller_handoff.py", "tests/test_intraday_renewal_storage.py",
                "tests/test_intraday_partitioned_storage_setup.py", "--noconftest", "-q",
                "-p", "no:cacheprovider", "--basetemp=" + str(test_root / "pytest")]))
    unittest.main()
