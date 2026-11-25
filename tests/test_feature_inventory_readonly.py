"""Independent MCP -> real Operations -> real read-only inventory integration.

All directories and bytes below are synthetic workspace fixtures. No live MO2,
network, session file, native Qt object, actual archive or process API is used.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import mcp_server
from mo2_codex_bridge.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
TARGET = "Owned Fixture AI MADE"
BACKUP = TARGET + "_backup"
OTHER = "Other Fixture AI MADE"
OWN_PLUGIN = "OwnedFixture.esp"
CONSUMER = "ConsumerFixture.esp"
_AUDIT = {"active": False}


def _feature_audit(event, args):
    if not _AUDIT["active"]:
        return
    violation = None
    if event == "open":
        path, mode, flags = args[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        if writing:
            violation = "filesystem write: " + str(path)
        elif not isinstance(path, int):
            absolute = os.path.normcase(os.path.abspath(os.fsdecode(path)))
            fixture = _AUDIT["root"]
            if (absolute == fixture or absolute.startswith(fixture + os.sep)) and absolute not in _AUDIT["allowed_reads"]:
                violation = "unrequested fixture payload/archive read: " + str(path)
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.replace", "os.link",
                   "os.symlink", "os.chmod", "os.utime", "os.truncate", "subprocess.Popen",
                   "socket.connect", "socket.bind", "socket.getaddrinfo"}:
        violation = event
    if violation:
        _AUDIT["violations"].append(violation)
        raise AssertionError("Read-only feature attempted " + violation)


sys.addaudithook(_feature_audit)


@contextlib.contextmanager
def forbid_feature_side_effects(environment):
    assert not _AUDIT["active"]
    _AUDIT.update(root=os.path.normcase(os.path.abspath(str(environment.root))),
                  allowed_reads={os.path.normcase(os.path.abspath(str(p / "modlist.txt")))
                                 for p in environment.profile_paths.values()},
                  violations=[], active=True)
    try:
        yield
    finally:
        _AUDIT["active"] = False
        if _AUDIT["violations"]:
            raise AssertionError("Forbidden feature side effects: " + repr(_AUDIT["violations"]))
        if environment.mutations:
            raise AssertionError("Organizer mutation/refresh/switch attempted: " + repr(environment.mutations))


class FakeMod:
    def __init__(self, name, path, *, backup=False, foreign=False, separator=False, overwrite=False, display=None):
        self._name, self._path = name, Path(path)
        self._display = display if display is not None else name
        self.flags = dict(backup=backup, foreign=foreign, separator=separator, overwrite=overwrite)

    def name(self): return self._display
    def absolutePath(self):
        if self.flags["foreign"]:
            raise AssertionError("Foreign paths must not be queried for native backup discovery")
        return str(self._path)
    def isBackup(self): return self.flags["backup"]
    def isForeign(self): return self.flags["foreign"]
    def isSeparator(self): return self.flags["separator"]
    def isOverwrite(self): return self.flags["overwrite"]


class FakeModList:
    def __init__(self, environment): self.e = environment
    def allMods(self): return list(self.e.mods)
    def displayName(self, name): return self.e.mods[name]._display
    def allModsByProfilePriority(self):
        raise AssertionError("The native backup read must not use the profile-priority list")
    def getMod(self, name): return None if name in self.e.missing_mods else self.e.mods.get(name)
    def state(self, name): return 1 if name == TARGET else 0
    def __getattr__(self, name): return self.e.forbidden("modList." + name)


class FakePlugins:
    def __init__(self, environment): self.e = environment
    def pluginNames(self): return [OWN_PLUGIN, CONSUMER]
    def state(self, name): return 1
    def masters(self, name): return [OWN_PLUGIN] if name == CONSUMER else []
    def origin(self, name): return TARGET if name == OWN_PLUGIN else OTHER
    def __getattr__(self, name): return self.e.forbidden("pluginList." + name)


class SyntheticEnvironment:
    def __init__(self, root):
        self.root = Path(root)
        self.mods_root = self.root / "mods"
        self.profile_paths = {name: self.root / "profiles" / name for name in ("Current Fixture", "Other Fixture")}
        self.current = "Current Fixture"
        self.mutations, self.missing_mods = [], set()
        self.mods = {
            TARGET: FakeMod(TARGET, self.mods_root / TARGET),
            OTHER: FakeMod(OTHER, self.mods_root / OTHER),
            BACKUP: FakeMod(BACKUP, self.mods_root / BACKUP, backup=True),
            "DLC: Fixture": FakeMod("DLC: Fixture", self.root / "foreign-game", foreign=True, display="Contenu de base"),
            "Section_separator": FakeMod("Section_separator", self.mods_root / "Section_separator", separator=True),
            "Overwrite": FakeMod("Overwrite", self.root / "overwrite", overwrite=True),
        }
        self.mod_list, self.plugin_list = FakeModList(self), FakePlugins(self)
        self.mobase = types.SimpleNamespace(ModState=types.SimpleNamespace(ACTIVE=1),
                                            PluginState=types.SimpleNamespace(ACTIVE=1))
        self.ops = Operations(self, self.mobase, self.root / "bridge-data-never-created",
                              self.forbidden("refresh"), self.forbidden("select_profile"))
        self.ops.snapshot = self.forbidden("unrequested full snapshot")

    def forbidden(self, operation):
        def fail(*args, **kwargs):
            self.mutations.append(operation)
            raise AssertionError("Forbidden read-side operation: " + operation)
        return fail

    def appVersion(self): return types.SimpleNamespace(displayString=lambda *args: "2.5.2")
    def modsPath(self): return str(self.mods_root)
    def profileName(self): return self.current
    def profilePath(self): return str(self.profile_paths[self.current])
    def modList(self): return self.mod_list
    def pluginList(self): return self.plugin_list
    def resolvePath(self, name):
        return str(self.mods_root / (TARGET if name == OWN_PLUGIN else OTHER) / name)
    def __getattr__(self, name): return self.forbidden("organizer." + name)

    def populate(self):
        for mod in self.mods.values(): mod._path.mkdir(parents=True, exist_ok=True)
        for path in self.profile_paths.values(): path.mkdir(parents=True)
        (self.mods_root / TARGET / OWN_PLUGIN).write_bytes(b"fictional plugin; metadata-only read")
        (self.mods_root / OTHER / CONSUMER).write_bytes(b"fictional consumer; no header read")
        (self.mods_root / TARGET / "meta.ini").write_bytes(b"[General]\nversion=1.0\n")
        (self.mods_root / BACKUP / "backup-payload.7z").write_bytes(b"NOT AN ARCHIVE; feature must not open payload")
        (self.mods_root / BACKUP / "meta.ini").write_bytes(b"[General]\nversion=0.9\n")
        (self.profile_paths["Current Fixture"] / "modlist.txt").write_bytes(
            ("# MO2 synthetic list\r\n+" + TARGET + "\r\n-" + OTHER + "\r\n*DLC: Fixture\r\n").encode("utf-8"))
        (self.profile_paths["Other Fixture"] / "modlist.txt").write_bytes(
            ("# MO2 synthetic list\n-" + TARGET + "\n+" + OTHER + "\n*DLC: Fixture\n").encode("utf-8"))

    def bytes_and_inventory(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            result[relative] = ("directory",) if path.is_dir() else ("file", path.read_bytes())
        return result


class OperationsClient:
    """Adapter substitutes only HTTP; no feature function or dispatch is mocked."""
    def __init__(self, environment): self.environment, self.calls = environment, []
    def call(self, action, params=None, **kwargs):
        self.calls.append((action, params))
        return self.environment.ops.dispatch(action, params or {})
    def job(self, job_id): raise AssertionError("No job/receipt/session lookup needed for this fixture")


def request(action, params=None, request_id=1, tool="mo2_inspect"):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": tool, "arguments": {"action": action, "params": params or {}}}}


def result_value(response):
    if response.get("isError"):
        raise AssertionError("Read request failed: " + response["content"][0]["text"])
    return json.loads(response["content"][0]["text"])


class FeatureReadOnlyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="readonly-feature-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.e = SyntheticEnvironment(self.temporary.name)
        self.e.populate()
        self.client = OperationsClient(self.e)

    def inspect(self, action, params=None):
        before = self.e.bytes_and_inventory()
        current = self.e.current
        with forbid_feature_side_effects(self.e):
            response = mcp_server.dispatch(request(action, params), self.client)
        self.assertEqual(self.e.bytes_and_inventory(), before)
        self.assertEqual(self.e.current, current)
        self.assertFalse((self.e.root / "bridge-data-never-created").exists())
        return response

    def test_real_read_dispatch_lists_native_backup_and_two_profile_states(self):
        backup = result_value(self.inspect("native_mod_backups"))
        self.assertTrue(backup["read_only"])
        self.assertFalse(backup["content_checked"])
        self.assertFalse(backup["restore_supported"])
        self.assertEqual(backup["total_backups"], 1)
        row = backup["backups"][0]
        self.assertEqual(row["name"], BACKUP)
        self.assertEqual(row["association"]["target_mod_name"], TARGET)
        self.assertEqual(row["association"]["status"], "inferred")
        self.assertFalse(row["content_verified"])
        self.assertFalse(row["restore_ready"])
        usage = result_value(self.inspect("mod_profile_usage", {"mod": TARGET}))
        self.assertTrue(usage["saved_usage_complete"])
        self.assertEqual({p["name"]: p["saved"]["state"] for p in usage["profiles"]},
                         {"Current Fixture": "enabled", "Other Fixture": "disabled"})
        self.assertTrue(usage["current_profile"]["live_saved_agree"])
        provided = usage["current_active_plugins"]["provided_by_mod"]
        self.assertEqual([p["name"] for p in provided], [OWN_PLUGIN])
        self.assertEqual(provided[0]["active_dependents"], [CONSUMER])
        self.assertEqual([a for a, _ in self.client.calls], ["native_mod_backups", "mod_profile_usage"])

    def test_unsafe_saved_file_is_unknown_without_erasing_other_evidence(self):
        (self.e.profile_paths["Other Fixture"] / "modlist.txt").write_bytes(b"+ambiguous\n\xffinvalid UTF8\n")
        usage = result_value(self.inspect("mod_profile_usage", {"mod": TARGET}))
        self.assertFalse(usage["saved_usage_complete"])
        rows = {p["name"]: p["saved"] for p in usage["profiles"]}
        self.assertEqual(rows["Current Fixture"]["state"], "enabled")
        self.assertEqual(rows["Other Fixture"]["state"], "unknown")
        self.assertTrue(rows["Other Fixture"]["reason"])

    def test_disappeared_native_backup_returns_error_not_false_empty_inventory(self):
        self.e.missing_mods.add(BACKUP)
        response = self.inspect("native_mod_backups")
        self.assertTrue(response["isError"])
        self.assertIn("incomplete", response["content"][0]["text"].lower())

    def test_read_features_are_rejected_by_mutating_mcp_tool(self):
        for action, params in (("native_mod_backups", {}), ("mod_profile_usage", {"mod": TARGET})):
            with self.subTest(action=action):
                before = self.e.bytes_and_inventory()
                with forbid_feature_side_effects(self.e):
                    with self.assertRaises(mcp_server.RPCError) as caught:
                        mcp_server.dispatch(request(action, params, tool="mo2_change"), self.client)
                self.assertEqual(caught.exception.code, -32602)
                self.assertEqual(self.e.bytes_and_inventory(), before)
        self.assertFalse(self.client.calls)

    def test_actual_stdio_adapter_reaches_real_features_with_no_disk_side_effects(self):
        worker = """import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,str(Path(sys.argv[1])/'tests'))
import test_feature_inventory_readonly as fixture
import mcp_server
environment=fixture.SyntheticEnvironment(Path(sys.argv[2]))
mcp_server.BridgeClient=lambda *a,**kw:fixture.OperationsClient(environment)
sys.argv=['mcp_server.py','--mo2',str(environment.root/'no-session-read')]
with fixture.forbid_feature_side_effects(environment):
    mcp_server.main()
"""
        messages = [request("native_mod_backups", request_id=1),
                    request("mod_profile_usage", {"mod": TARGET}, request_id=2)]
        before = self.e.bytes_and_inventory()
        completed = subprocess.run([sys.executable, "-B", "-c", worker, str(ROOT), str(self.e.root)],
                                   input="\n".join(json.dumps(m) for m in messages) + "\n",
                                   capture_output=True, text=True, encoding="utf-8", timeout=15,
                                   executable=sys.executable)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(result_value(replies[0]["result"])["total_backups"], 1)
        usage = result_value(replies[1]["result"])
        self.assertTrue(usage["saved_usage_complete"], json.dumps(usage, ensure_ascii=False))
        self.assertEqual(self.e.bytes_and_inventory(), before)
        self.assertFalse((self.e.root / "bridge-data-never-created").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
