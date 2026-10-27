"""Portable exact-leaf controller checks; no live subprocess, broker or provider.\n\nThe compatibility patch and metadata are the tested sources. Full composed\nconsumer and kernel-lifetime regressions are separate installation evidence.\n"""
from __future__ import annotations
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "docs/development/patches/trader-relay-terminal-evidence.json"
metadata = json.loads(PACKAGE.read_text())
host = __import__("types").ModuleType("portable_terminal_evidence")
host.__dict__.update(dict(Path=Path, datetime=__import__("datetime").datetime,
    timezone=__import__("datetime").timezone, hashlib=hashlib, json=json,
    os=os, time=__import__("time"), subprocess=__import__("subprocess"),
    PROTOCOL=metadata["protocol"], HOST_SHA256=metadata["after_sha256"]))
# Birth is intentionally supplied by offline fixtures. No process is inspected.
host.birth = lambda pid: None
for name in ("_diagnostic_birth", "_terminal_reference", "wait_for_controller_relay",
             "write_json", "read_stop_reason", "record_terminal"):
    item=metadata["extracts"][name]
    assert hashlib.sha256(item["source"].encode("utf8")).hexdigest()==item["sha256"]
    exec(compile(item["source"], str(PACKAGE)+":"+name, "exec"),host.__dict__)
stop_source=metadata["extracts"]["stop"]["source"]
factory="def make_stop():\n    reason=None\n"+__import__("textwrap").indent(stop_source,"    ")+"    return stop\n"
exec(compile(factory,str(PACKAGE)+":stop","exec"),host.__dict__)


class Clock:
    def __init__(self):
        self.at = 0.0

    def monotonic(self):
        return self.at

    def sleep(self, seconds):
        self.at = round(self.at + seconds, 8)


class RelayFixture:
    def __init__(self, root, *, ready=10.05, reason="FAILURE", mutate=None, birth_known=True):
        self.root = root
        self.path = root / "handoff.json"
        self.clock = Clock()
        self.reason = reason
        self.ready = ready
        self.birth_known = birth_known
        self.events = []
        self.starts = 0
        self.terminated = False
        self.returncode = None
        self.stopping = threading.Event()
        self.reason_seen = None
        self.sink = {}
        self.payload = dict(envelope=str(root / "session.json"), token="fixture-token",
            handoff_id="fixture-handoff", parent_pid=101, parent_birth="parent-birth",
            host_sha256=host.HOST_SHA256, generations=dict(execution=1), done=["initialize"])
        self.outcome = dict(disposition=reason, generations=dict(execution=30,scoring=3),
            completed_roles=["initialize"], session_id="one-original-session")
        self.envelope = dict(stop_path=str(root / "stop.json"), owner_pid=100,
            owner_birth="original-owner-birth", session=dict(session_id="one-original-session"))
        (root / "session.json").write_text(json.dumps(self.envelope))
        (root / "stop.json").write_text(json.dumps(dict(reason=reason)))
        self.path.write_text(json.dumps(self.payload))
        self.snapshot = dict(schema="trader-controller-terminal-evidence-v1", protocol=host.PROTOCOL,
            token=self.payload["token"], handoff_id=self.payload["handoff_id"],
            parent_pid=self.payload["parent_pid"], parent_birth=self.payload["parent_birth"],
            controller_pid=777, controller_birth="child-birth",
            owner_pid=self.envelope["owner_pid"], owner_birth=self.envelope["owner_birth"],
            session_id=self.envelope["session"]["session_id"], host_sha256=host.HOST_SHA256,
            disposition=reason, diagnostic_only=True, generations=self.outcome["generations"],
            completed_roles=["initialize"], states=dict(scoring=dict(state=dict(cursor=27))),
            failure=dict(role="scoring", exit_code=1, exception_type="FileNotFoundError"))
        if mutate:
            self.snapshot.update(mutate)
        self.path.with_suffix(".terminal.json").write_text(json.dumps(self.snapshot))

    @property
    def pid(self):
        return 777

    def popen(self, *a, **k):
        self.starts += 1
        return self

    def poll(self):
        if self.terminated:
            self.returncode = 1
            return 1
        if self.clock.at >= self.ready:
            self.path.with_suffix(".result.json").write_text(json.dumps(dict(protocol=host.PROTOCOL,
                token=self.payload["token"], handoff_id=self.payload["handoff_id"], outcome=self.outcome)))
            self.returncode = 1 if self.reason == "FAILURE" else 0
            return self.returncode
        return None

    def terminate(self):
        self.terminated = True
        self.events.append(("terminate", self.clock.at))

    def wait(self):
        self.events.append(("wait", self.clock.at))
        return self.poll()

    def stop(self, why):
        if not self.stopping.is_set():
            self.reason_seen = why
            self.stopping.set()
        return self.reason_seen

    def run(self):
        with patch.object(host.subprocess, "Popen", self.popen), \
             patch.object(host.time, "monotonic", self.clock.monotonic), \
             patch.object(host.time, "sleep", self.clock.sleep), \
             patch.object(host, "birth", lambda pid: "child-birth" if self.birth_known else None):
            return host.wait_for_controller_relay(self.path, self.payload, ["never-run"],
                stop=self.stop, stopping=self.stopping, phase=lambda *a: None,
                terminal_evidence=self.sink)


class RelayTerminalTests(unittest.TestCase):
    def test_after_grace_keeps_exact_snapshot_without_accepting_it_as_a_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = RelayFixture(Path(temporary))
            with self.assertRaises(FileNotFoundError):
                f.run()
            self.assertEqual(f.events, [("terminate", 10.0), ("wait", 10.0)])
            self.assertEqual(f.starts, 1)
            self.assertEqual(f.reason_seen, "FAILURE")
            self.assertFalse(f.path.with_suffix(".result.json").exists())
            reference = f.sink["descendant"]
            self.assertTrue(reference["diagnostic_only"])
            raw = Path(reference["path"]).read_bytes()
            self.assertEqual(reference["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(json.loads(raw)["generations"]["execution"], 30)

    def test_before_grace_accepts_only_existing_final_result_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = RelayFixture(Path(temporary), ready=9.95)
            result = f.run()
            self.assertEqual(result, f.outcome)
            self.assertFalse(f.terminated)
            self.assertEqual(f.sink, {})
            self.assertEqual(f.starts, 1)

    def test_manual_stop_after_grace_stays_manual_with_separate_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = RelayFixture(Path(temporary), reason="MANUAL_STOP")
            result = f.run()
            self.assertEqual(result["disposition"], "MANUAL_STOP")
            self.assertEqual(result["generations"], dict(execution=1))
            self.assertEqual(f.reason_seen, "MANUAL_STOP")
            self.assertEqual(f.events, [("terminate", 10.0), ("wait", 10.0)])
            self.assertTrue(f.sink["descendant"]["diagnostic_only"])
            self.assertEqual(f.starts, 1)

    def test_forged_identity_and_nonterminal_snapshots_are_never_attached(self):
        forged = dict(token="other", handoff_id="other", parent_pid=999,
            parent_birth="other", controller_pid=999, controller_birth="other",
            owner_pid=999, owner_birth="other", session_id="other", host_sha256="0"*64,
            schema="other", disposition="SESSION_COMPLETE", diagnostic_only=False)
        for key, value in forged.items():
            with self.subTest(field=key), tempfile.TemporaryDirectory() as temporary:
                f = RelayFixture(Path(temporary), mutate={key:value})
                with self.assertRaises(FileNotFoundError):
                    f.run()
                self.assertEqual(f.sink, {})
                self.assertEqual(f.reason_seen, "FAILURE")
                self.assertEqual(f.starts, 1)
                self.assertTrue(f.terminated)

    def test_unavailable_birth_and_malformed_diagnostics_do_not_mask_failure(self):
        for malformed in (None, [], "invalid-json"):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as temporary:
                f = RelayFixture(Path(temporary), birth_known=malformed is not None)
                path = f.path.with_suffix(".terminal.json")
                if malformed == []:
                    path.write_text("[]")
                elif malformed == "invalid-json":
                    path.write_text("{")
                with self.assertRaises(FileNotFoundError):
                    f.run()
                self.assertEqual(f.sink, {})
                self.assertEqual(f.events[0], ("terminate", 10.0))

    def recorder(self,root,*,diagnostic_error=False):
        path=root/"controller.terminal.json"
        stop_path=root/"stop.json"
        records=[]
        references={}
        real_write=host.write_json
        def write(target,value):
            records.append(Path(target).name)
            if diagnostic_error and Path(target)==path:
                raise PermissionError("fixture unavailable diagnostic path")
            return real_write(target,value)
        child=__import__("types").SimpleNamespace(pid=123)
        values=dict(envelope=dict(token="fixture-token",session=dict(session_id="same-session"),
                owner_pid=100,owner_birth="owner-birth"),
            continuation=dict(handoff_id="same-handoff",parent_pid=101,parent_birth="parent-birth"),
            controller_birth="fixture-controller-birth",terminal_path=path,
            terminal_evidence=references, generations=dict(execution=30,scoring=3),
            done={"initialize"},states=dict(scoring=dict(state=dict(cursor=27))),
            failure=dict(role="scoring",exit_code=1,exception_type="FileNotFoundError"),
            children=dict(execution=(child,root/"execution.response.json","same-turn")),
            stopping=threading.Event(),stop_path=stop_path,write_json=write)
        return path,stop_path,records,references,values

    def test_terminal_recorder_keeps_cursors_identity_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path,stop_path,records,references,values=self.recorder(Path(temporary))
            with patch.multiple(host,**values,create=True):
                host.record_terminal("FAILURE")
            value=json.loads(path.read_text())
            self.assertEqual(value["generations"],dict(execution=30,scoring=3))
            self.assertEqual(value["states"],dict(scoring=dict(state=dict(cursor=27))))
            self.assertEqual(value["controller_pid"],os.getpid())
            self.assertEqual(value["controller_birth"],"fixture-controller-birth")
            self.assertEqual(value["handoff_id"],"same-handoff")
            self.assertEqual(value["token"],"fixture-token")
            self.assertEqual(value["failure"]["role"],"scoring")
            self.assertEqual(value["active_workers"]["execution"]["turn"],"same-turn")
            self.assertTrue(value["diagnostic_only"])
            self.assertEqual(references["local"]["sha256"],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertFalse(stop_path.exists())

    def test_stop_publishes_original_latch_first_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path,stop_path,records,references,values=self.recorder(Path(temporary))
            with patch.multiple(host,**values,create=True):
                stop=host.make_stop()
                self.assertEqual(stop("MANUAL_STOP"),"MANUAL_STOP")
                self.assertEqual(stop("FAILURE"),"MANUAL_STOP")
            self.assertEqual(records,["stop.json","controller.terminal.json"])
            self.assertEqual(json.loads(stop_path.read_text())["reason"],"MANUAL_STOP")
            self.assertEqual(json.loads(path.read_text())["disposition"],"MANUAL_STOP")

    def test_unavailable_diagnostic_does_not_replace_manual_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            path,stop_path,records,references,values=self.recorder(Path(temporary),diagnostic_error=True)
            with patch.multiple(host,**values,create=True),redirect_stdout(io.StringIO()):
                stop=host.make_stop()
                self.assertEqual(stop("MANUAL_STOP"),"MANUAL_STOP")
            self.assertEqual(json.loads(stop_path.read_text())["reason"],"MANUAL_STOP")
            self.assertEqual(references,{})
            self.assertFalse(path.exists())

    def test_package_patch_hash_and_exact_source_extracts(self):
        patch_path=PACKAGE.with_suffix(".patch")
        raw=patch_path.read_bytes()
        self.assertEqual(metadata["schema"],"trader-relay-terminal-evidence-patch-v1")
        self.assertEqual(metadata["source"],"tools/trader_session_host.py")
        self.assertEqual(metadata["patch_sha256"],hashlib.sha256(raw).hexdigest())
        self.assertEqual([line for line in raw.decode().splitlines() if line.startswith("--- ")],
                         ["--- a/tools/trader_session_host.py"])
        self.assertEqual([line for line in raw.decode().splitlines() if line.startswith("+++ ")],
                         ["+++ b/tools/trader_session_host.py"])
        for name,item in metadata["extracts"].items():
            self.assertEqual(item["sha256"],hashlib.sha256(item["source"].encode("utf8")).hexdigest())
        self.assertIn("deadline = deadline or time.monotonic() + 10",
                      (raw.decode()+metadata["extracts"]["wait_for_controller_relay"]["source"]))


if __name__=="__main__":
    print("Portable extracted-leaf checks only; full consumer/runtime evidence is separate.")
    unittest.main()
