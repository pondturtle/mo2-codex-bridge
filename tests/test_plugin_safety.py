"""Independent bounded retention, launch lifecycle and protocol regressions."""
import ast
import concurrent.futures
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import test_client_recovery as existing
from mo2_codex_bridge.common import VERSION
from mo2_codex_bridge.job_store import JobStore, CACHE_BYTES
from mo2_codex_bridge.running_apps import RunningApplications, WindowsProcessObserver
import mcp_server

ROOT = Path(__file__).resolve().parents[1]


class JobRetentionTests(unittest.TestCase):
    setUpClass = classmethod(existing.PluginRecoveryTests.setUpClass.__func__)
    setUp = existing.PluginRecoveryTests.setUp
    tearDown = existing.PluginRecoveryTests.tearDown

    def test_skse_diagnostic_uses_existing_audit_stream_without_a_job(self):
        event = {"event": "waiting_for_survivors", "session_id": "fictional-session",
                 "launch_number": 1, "survivor_count": 1}
        tree = ast.parse((ROOT / "mo2_codex_bridge/plugin.py").read_text(encoding="utf-8"))
        plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        method = next(node for node in plugin.body if isinstance(node, ast.FunctionDef)
                      and node.name == "audit_skse_event")
        namespace = {"json": json, "os": os, "time": time}
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, "<SKSE diagnostic audit>", "exec"), namespace)
        namespace["audit_skse_event"](self.owner, event)
        record = json.loads((self.owner.data / "audit.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["tracking"], event)
        self.assertEqual(record["action"], "skse_launch_tracking")
        self.assertEqual(record["status"], event["event"])
        self.assertIsNone(record["job_id"])
        self.assertGreater(record["monotonic_ns"], 0)
        self.assertEqual(len(self.owner.jobs), 0)

    def test_ten_thousand_read_jobs_remain_bounded_and_mutations_still_work(self):
        payload = "x" * 8192
        self.owner.ops.dispatch = lambda *args: {"snapshot": payload}
        for i in range(10000):
            self.owner.enqueue({"action": "snapshot", "request_id": "read-" + str(i)})
            self.owner.tick()
        self.assertLessEqual(len(self.owner.jobs), 128)
        self.assertLessEqual(sum(j.get("_retained_size", 0) for j in self.owner.jobs.cache.values()), CACHE_BYTES)
        self.assertIsNone(self.owner.get_job("read-0"))
        self.assertEqual(self.owner.get_job("read-9999")["status"], "done")
        self.owner.enqueue({"action": "write_config", "request_id": "after-reads"})
        self.owner.tick()
        self.assertEqual(self.owner.get_job("after-reads")["status"], "done")

    def test_active_jobs_are_never_evicted_by_count_age_or_byte_pressure(self):
        self.owner.jobs = JobStore(self.owner.data, read_limit=1, byte_limit=128, clock=lambda: 10**9)
        for i in range(64):
            self.owner.enqueue({"action": "snapshot", "request_id": "active-" + str(i)})
        self.owner.jobs.prune()
        self.assertEqual(len(self.owner.jobs), 64)
        self.assertTrue(all(job["status"] == "queued" for job in self.owner.jobs.cache.values()))
        with self.assertRaisesRegex(ValueError, "queue is full"):
            self.owner.enqueue({"action": "write_config", "request_id": "queue-full-mutation"})
        self.assertIsNone(self.owner.get_job("queue-full-mutation"))

    def test_evicted_and_restarted_mutation_id_never_executes_twice(self):
        calls = []
        self.owner.ops.dispatch = lambda action, params: calls.append((action, params)) or {"backup_id": "kept-backup", "saved": True}
        self.owner.jobs = JobStore(self.owner.data, mutation_limit=0)
        request = {"action": "write_config", "params": {"text": "owned-test-text"}, "request_id": "durable-one"}
        self.owner.enqueue(request)
        self.owner.tick()
        self.assertEqual(len(self.owner.jobs), 0)
        self.owner.jobs = JobStore(self.owner.data)
        self.assertEqual(self.owner.enqueue(request)["status"], "done")
        self.assertEqual(self.owner.get_job("durable-one")["result"]["backup_id"], "kept-backup")
        self.assertTrue(self.owner.pending.empty())
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ValueError, "another operation"):
            self.owner.enqueue(dict(request, params={"text": "different"}))
        raw = next((self.owner.data / "mutation-receipts").rglob("*.json")).read_text()
        self.assertNotIn("owned-test-text", raw)
        self.assertNotIn('"params"', raw)

    def test_interrupted_claim_survives_restart_as_unknown_without_enqueue(self):
        request = {"action": "write_config", "request_id": "interrupted-claim", "params": {"text": "unexecuted"}}
        self.owner.enqueue(request)
        self.owner.jobs = JobStore(self.owner.data)
        self.owner.pending = queue.Queue(maxsize=64)
        receipt = self.owner.enqueue(request)
        self.assertTrue(receipt["recovery_required"])
        job = self.owner.get_job("interrupted-claim")
        self.assertEqual(job["status"], "unknown")
        self.assertTrue(job["pending"])
        self.assertTrue(self.owner.pending.empty())
        with self.assertRaisesRegex(ValueError, "another operation"):
            self.owner.enqueue(dict(request, params={"text": "other"}))

    def test_real_process_exit_after_claim_never_requeues_on_restart(self):
        worker = """import sys,os
sys.path.insert(0,sys.argv[1])
from mo2_codex_bridge.job_store import JobStore
store=JobStore(sys.argv[2])
store.reserve({'job_id':'abrupt-claim','action':'write_config','params':{},'status':'queued','created':0},mutation=True)
os._exit(73)
"""
        crashed = subprocess.run([sys.executable, "-B", "-c", worker, str(ROOT), str(self.owner.data)],
                                 capture_output=True, text=True, timeout=10, executable=sys.executable)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.owner.jobs = JobStore(self.owner.data)
        self.assertTrue(self.owner.enqueue({"action": "write_config", "request_id": "abrupt-claim"})["recovery_required"])
        self.assertTrue(self.owner.pending.empty())

    def test_claim_fsync_failure_retains_identity_without_executing(self):
        with mock.patch("mo2_codex_bridge.job_store.os.fsync", side_effect=OSError("flush failed")):
            with self.assertRaisesRegex(OSError, "flush failed"):
                self.owner.enqueue({"action": "write_config", "request_id": "claim-flush-failed"})
        self.assertTrue(self.owner.pending.empty())
        self.owner.jobs = JobStore(self.owner.data)
        self.assertTrue(self.owner.enqueue({"action": "write_config", "request_id": "claim-flush-failed"})["recovery_required"])
        self.assertTrue(self.owner.pending.empty())

    def test_concurrent_duplicate_submissions_enqueue_exactly_once(self):
        self.owner.jobs = JobStore(self.owner.data)
        request = {"action": "write_config", "request_id": "concurrent-one"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: self.owner.enqueue(request), range(64)))
        self.assertTrue(all(x["job_id"] == "concurrent-one" for x in receipts))
        self.assertEqual(self.owner.pending.qsize(), 1)
        calls = []
        self.owner.ops.dispatch = lambda *args: calls.append(True) or {"saved": True}
        self.owner.tick()
        self.assertEqual(calls, [True])

    def test_claim_write_failure_prevents_enqueue(self):
        original = Path.open
        def deny(path, *args, **kwargs):
            if "mutation-receipts" in path.parts and args and args[0] == "xb":
                raise PermissionError("claim denied")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "open", deny):
            with self.assertRaisesRegex(PermissionError, "claim denied"):
                self.owner.enqueue({"action": "write_config", "request_id": "claim-denied"})
        self.assertTrue(self.owner.pending.empty())
        self.assertIsNone(self.owner.get_job("claim-denied"))

    def test_terminal_receipt_failure_preserves_claim_and_warns(self):
        self.owner.enqueue({"action": "write_config", "request_id": "receipt-denied"})
        with mock.patch("mo2_codex_bridge.job_store.atomic_write", side_effect=OSError("receipt denied")):
            self.owner.tick()
        job = self.owner.get_job("receipt-denied")
        self.assertFalse(job["receipt_recorded"])
        self.assertFalse(job["result"]["receipt_recorded"])
        self.owner.jobs = JobStore(self.owner.data)
        receipt = self.owner.enqueue({"action": "write_config", "request_id": "receipt-denied"})
        self.assertTrue(receipt["recovery_required"])
        self.assertTrue(self.owner.pending.empty())

    def test_corrupt_receipt_cannot_turn_old_id_into_new_work(self):
        self.owner.enqueue({"action": "write_config", "request_id": "corrupt-receipt"})
        self.owner.tick()
        self.owner.jobs.cache.clear()
        path = next((self.owner.data / "mutation-receipts").rglob("*.json"))
        path.write_bytes(b"{corrupt")
        with self.assertRaises(ValueError):
            self.owner.enqueue({"action": "write_config", "request_id": "corrupt-receipt"})
        self.assertTrue(self.owner.pending.empty())

    def test_oversized_read_is_explicit_failure_not_disappearing_or_unbounded(self):
        self.owner.jobs = JobStore(self.owner.data, byte_limit=4096)
        self.owner.ops.dispatch = lambda *args: {"snapshot": "x" * 10000}
        self.owner.enqueue({"action": "snapshot", "request_id": "large-read"})
        self.owner.tick()
        job = self.owner.get_job("large-read")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure"]["error_type"], "ResultRetentionLimit")
        self.assertFalse(job["failure"]["changes_may_have_occurred"])

    def test_oversized_mutation_retains_claim_and_backup_link_after_eviction(self):
        self.owner.jobs = JobStore(self.owner.data, mutation_limit=0)
        self.owner.ops.dispatch = lambda *args: {"backup_id": "large-result-backup", "payload": "x" * 150000}
        request = {"action": "write_config", "request_id": "large-mutation"}
        self.owner.enqueue(request)
        self.owner.tick()
        job = self.owner.get_job("large-mutation")
        self.assertTrue(job["recovery_required"])
        self.assertEqual(job["backup_id"], "large-result-backup")
        self.assertEqual(self.owner.enqueue(request)["status"], "unknown")
        self.assertTrue(self.owner.pending.empty())

    def test_params_are_frozen_when_submitted(self):
        params = {"text": "before"}
        self.owner.enqueue({"action": "write_config", "params": params, "request_id": "frozen"})
        params["text"] = "after"
        self.assertEqual(self.owner.jobs["frozen"]["params"]["text"], "before")

    def test_age_eviction_drops_read_result_but_preserves_mutation_identity(self):
        now = [0]
        self.owner.jobs = JobStore(self.owner.data, clock=lambda: now[0], read_ttl=1, mutation_ttl=1)
        for action in ("snapshot", "write_config"):
            self.owner.enqueue({"action": action, "request_id": action.replace("_", "-")})
            self.owner.tick()
        now[0] = 2
        self.assertIsNone(self.owner.get_job("snapshot"))
        self.assertEqual(self.owner.get_job("write-config")["status"], "done")
        self.assertEqual(len(self.owner.jobs), 0)


class RunningApplicationTests(unittest.TestCase):
    def setUp(self):
        self.path = r"C:\owned-tests\tool.exe"
        self.instances = []
        self.guard = RunningApplications(lambda _: list(self.instances))

    def instance(self, pid, birth=1):
        return {"pid": pid, "created": birth, "executable": self.path}

    def test_same_executable_two_instances_one_finish_keeps_guard(self):
        self.guard.starting(self.path)
        self.instances[:] = [self.instance(1)]
        self.guard.starting(self.path)
        self.instances[:] = [self.instance(1), self.instance(2)]
        self.assertTrue(self.guard)
        self.instances[:] = [self.instance(2)]
        self.guard.finished(self.path, 0)
        self.assertTrue(self.guard)
        self.assertIn(self.path, list(self.guard))
        self.instances.clear()
        self.guard.finished(self.path, 0)
        self.assertFalse(self.guard)

    def test_canceled_or_failed_unobserved_attempt_is_not_guessed_away(self):
        for _ in range(3):
            self.guard.starting(self.path)
        self.guard.finished(self.path, 0)
        self.assertTrue(self.guard)
        self.assertEqual(self.guard.describe()["launches"][0]["pending_attempts"], 2)
        self.assertTrue(any("unconfirmed" in x for x in self.guard))

    def test_observer_failure_or_denied_access_is_not_absence(self):
        self.guard.starting(self.path)
        self.guard.observer = mock.Mock(side_effect=PermissionError("denied"))
        self.guard.finished(self.path, 0)
        self.assertTrue(self.guard)
        self.assertIn("PermissionError", self.guard.describe()["launches"][0]["observation_error"])
        self.guard.observer = lambda _: []
        self.assertTrue(self.guard, "Unobserved launch remains uncertain after observer recovers")

    def test_force_unlocked_or_observed_wrapper_children_remain_uncertain(self):
        for exit_code, children in ((259, False), (0, True)):
            self.guard = RunningApplications(lambda _: list(self.instances))
            self.instances.clear()
            self.guard.starting(self.path)
            self.instances[:] = [dict(self.instance(10), children_observed=children)]
            self.assertTrue(self.guard)
            self.instances.clear()
            self.guard.finished(self.path, exit_code)
            self.assertTrue(self.guard)
            self.assertIsNotNone(self.guard.describe()["launches"][0]["finish_uncertainty"])

    def test_finish_without_observed_root_never_claims_wrapper_tree_exited(self):
        self.guard.starting(self.path)
        self.guard.finished(self.path, 0)
        self.assertTrue(self.guard)
        self.assertTrue(any("unconfirmed" in name for name in self.guard))

    def test_pid_reuse_is_reported_with_distinct_creation_identity(self):
        self.guard.starting(self.path)
        self.instances[:] = [self.instance(5, 100)]
        first = self.guard.describe()["launches"][0]["instances"][0]
        self.instances[:] = [self.instance(5, 200)]
        second = self.guard.describe()["launches"][0]["instances"][0]
        self.assertNotEqual((first["pid"], first["created"]), (second["pid"], second["created"]))

    def test_manual_fake_guards_and_unmatched_finished_callback(self):
        self.guard.add("fake-game")
        self.guard.finished(self.path, 0)
        self.assertTrue(self.guard)
        self.guard.discard("fake-game")
        self.assertFalse(self.guard)
        self.guard.starting("relative.exe")
        self.assertTrue(self.guard)

    def test_busy_plugin_veto_does_not_create_phantom_launch(self):
        tree = ast.parse((ROOT / "mo2_codex_bridge/plugin.py").read_text())
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef))
        method = next(x for x in cls.body if isinstance(x, ast.FunctionDef) and x.name == "before_run")
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        namespace = {}
        exec(compile(module, "<run guard>", "exec"), namespace)
        owner = types.SimpleNamespace(busy=True, ops=types.SimpleNamespace(running=self.guard))
        self.assertFalse(namespace["before_run"](owner, self.path))
        self.assertFalse(self.guard)

    def test_callback_registration_failure_keeps_mutations_blocked(self):
        tree = ast.parse((ROOT / "mo2_codex_bridge/plugin.py").read_text())
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef))
        method = next(x for x in cls.body if isinstance(x, ast.FunctionDef) and x.name == "init")
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        with tempfile.TemporaryDirectory() as folder:
            namespace = {"Path": Path, "__file__": str(Path(folder) / "candidate/mo2_codex_bridge/plugin.py"),
                         "Operations": lambda *args: types.SimpleNamespace(), "mobase": object(),
                         "JobStore": JobStore, "RunningApplications": lambda **kwargs: self.guard}
            exec(compile(module, "<init guard>", "exec"), namespace)
            owner = types.SimpleNamespace(refresh_sync=lambda: None, select_profile=lambda name: None,
                                          wait_until=lambda *args: True, before_run=lambda *args: True,
                                          finished_run=lambda *args: None, start=lambda *args: None,
                                          audit_skse_event=lambda event: None)
            organizer = types.SimpleNamespace(onAboutToRun=lambda _: False, onFinishedRun=lambda _: True,
                                              onUserInterfaceInitialized=lambda _: True)
            self.assertTrue(namespace["init"](owner, organizer))
            self.assertTrue(owner.ops.running)
            self.assertIn("registration failed", owner.ops.running.global_uncertainty)

    @unittest.skipUnless(os.name == "nt", "Windows observer integration")
    def test_read_only_windows_observer_identifies_controlled_python_child(self):
        observer = WindowsProcessObserver()
        child = subprocess.Popen([sys.executable, "-B", "-c", "import sys; sys.stdin.read(1)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, executable=sys.executable)
        try:
            found = [x for x in observer(sys.executable) if x["pid"] == child.pid]
            self.assertEqual(len(found), 1)
            self.assertGreater(found[0]["created"], 0)
        finally:
            child.communicate(b"x", timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(any(x["pid"] == child.pid for x in observer(sys.executable)))


class ProtocolClassificationTests(unittest.TestCase):
    def exchange(self, messages, worker=None):
        command = [sys.executable, "-B", str(ROOT / "mcp_server.py")]
        if worker:
            command = [sys.executable, "-B", "-c", worker, str(ROOT)]
        result = subprocess.run(command, input="\n".join(
            x if isinstance(x, str) else json.dumps(x) for x in messages) + "\n",
            capture_output=True, text=True, encoding="utf-8", timeout=10, executable=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_actual_adapter_classifies_parse_envelope_method_and_params_errors(self):
        messages = ["{broken", [], {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": True, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 2, "method": "missing"},
                    {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": []},
                    {"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {"protocolVersion": []}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": 1}},
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "mo2_job", "arguments": {"job_id": []}}},
                    {"jsonrpc": "2.0", "id": 7, "method": "ping"}]
        replies = self.exchange(messages)
        self.assertEqual([x["error"]["code"] for x in replies[:-1]],
                         [-32700, -32600, -32600, -32600, -32601, -32602, -32602, -32602, -32602])
        self.assertEqual(replies[-1]["result"], {})
        self.assertEqual([x["id"] for x in replies[:4]], [None] * 4)

    def test_notifications_are_silent_and_duplicate_members_refused(self):
        replies = self.exchange([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "unrecognized", "params": []},
            '{"jsonrpc":"2.0","id":1,"method":"ping","method":"other"}',
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}])
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertEqual(replies[1]["result"]["serverInfo"]["version"], VERSION)

    def test_actual_adapter_internal_failure_is_not_invalid_request(self):
        worker = ("import sys;sys.path.insert(0,sys.argv[1]);import mcp_server;"
                  "mcp_server.dispatch=lambda *args:1/0;sys.argv=['mcp_server.py'];mcp_server.main()")
        replies = self.exchange([{"jsonrpc": "2.0", "id": "internal", "method": "ping"}], worker)
        self.assertEqual(replies[0]["error"]["code"], -32603)
        self.assertEqual(replies[0]["id"], "internal")

    def test_nonfinite_id_and_params_do_not_crash_stdio_or_reach_client(self):
        replies = self.exchange([
            '{"jsonrpc":"2.0","id":1e400,"method":"ping"}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mo2_inspect","arguments":{"action":"status","params":{"number":1e400}}}}',
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertIsNone(replies[0]["id"])
        self.assertEqual(replies[1]["error"]["code"], -32602)
        self.assertEqual(replies[2]["result"], {})

    def test_interrupted_install_receipt_never_drives_installer_helper(self):
        from client import BridgeClient, BridgePendingError
        for status in ("unknown", "running"):
            client = BridgeClient(str(ROOT / "fake-offline"))
            client.submit = mock.Mock(return_value={"job_id": "interrupted-install", "status": status})
            client.job = mock.Mock(return_value={"job_id": "interrupted-install", "status": status,
                                                "recovery_required": True, "backup_id": "preserved-backup",
                                                "message": "Inspect preserved receipt; do not resubmit"})
            client.advance_quick_install = mock.Mock()
            with self.assertRaises(BridgePendingError) as caught:
                client.call("install_archive", {"name": "owned fixture"}, request_id="interrupted-install")
            self.assertEqual(caught.exception.job_id, "interrupted-install")
            self.assertEqual(caught.exception.backup_id, "preserved-backup")
            self.assertTrue(caught.exception.failure["pending"])
            self.assertIn("Inspect preserved receipt", str(caught.exception))
            self.assertEqual(client.submit.call_count, 1)
            client.advance_quick_install.assert_not_called()

    def test_tool_schema_validation_happens_before_client_call(self):
        client = mock.Mock()
        for params in ({}, {"name": "unknown"}, {"name": "mo2_inspect", "arguments": []},
                       {"name": "mo2_inspect", "arguments": {"action": "write_config"}},
                       {"name": "mo2_change", "arguments": {"action": "write_config", "params": None}}):
            with self.assertRaises(mcp_server.RPCError) as caught:
                mcp_server.dispatch({"method": "tools/call", "params": params}, client)
            self.assertEqual(caught.exception.code, -32602)
        client.call.assert_not_called()
        client.job.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
