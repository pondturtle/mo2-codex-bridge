"""Conservative MO2 launch guard; callback counts are not process identities.

MO2 2.5.2 supplies neither PID nor a canceled/failed-start callback. A pending
attempt is therefore never cleared merely by elapsed time or an empty scan.
Only finished callbacks discharge attempts; read-only Windows observations keep
other exact executable instances guarded even if one callback completes.
"""
import ctypes
import ntpath
import os
import time
import uuid
from copy import deepcopy
from ctypes import wintypes


def path_key(value):
    value = str(value)
    if not value or not ntpath.isabs(value):
        raise ValueError("An absolute launched executable path is required")
    return ntpath.normcase(ntpath.normpath(value))


def _filetime(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class _ProcessHandle:
    def __init__(self, kernel, handle, pid, expected=None):
        self.kernel = kernel
        self.handle = handle
        self.pid = pid
        self._created = None
        try:
            self.executable = self._image_name(0)
        except OSError:
            if expected is None:
                raise
            # Once a process ends, the DOS-name query can fail while its NT
            # image name is still available on a retained process object.
            # Accept only an exact local volume mapping of the expected path.
            drive, tail = ntpath.splitdrive(expected)
            if len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha() or not tail.startswith("\\"):
                raise
            mapping = ctypes.create_unicode_buffer(32768)
            if not kernel.QueryDosDeviceW(drive, mapping, len(mapping)):
                raise ctypes.WinError(ctypes.get_last_error())
            target = ntpath.normcase(mapping.value)
            if not target.startswith("\\device\\") or ntpath.normcase(self._image_name(1)) != target + tail:
                raise ValueError("Native process image did not match the expected SKSE loader")
            self.executable = expected

    def _image_name(self, flags):
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self.kernel.QueryFullProcessImageNameW(self.handle, flags, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    @property
    def created(self):
        if self._created is None:
            self._created, _ = self._times()
        return self._created

    def _times(self):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(item) for item in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _filetime(times[0]), _filetime(times[1])

    def exit_time(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 258:  # WAIT_TIMEOUT: the process handle is not signaled.
            return None
        if result != 0:  # WAIT_OBJECT_0 is the only successful result here.
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited = self._times()
        if created != self.created or not exited or exited < created:
            raise OSError("Process exit time did not match its recorded identity")
        return exited

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.kernel.CloseHandle(handle)


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", wintypes.USHORT), ("max_length", wintypes.USHORT),
                ("buffer", ctypes.c_void_p)]


class _BasicProcessInformation(ctypes.Structure):
    _fields_ = [("next_offset", wintypes.ULONG), ("pid", wintypes.HANDLE),
                ("parent_pid", wintypes.HANDLE), ("sequence", ctypes.c_uint64),
                ("image", _UnicodeString)]


class WindowsProcessObserver:
    """Inspect only basename candidates, then verify their full path and birth time."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows process observation is unavailable")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel
        self.owner_pid = os.getpid()
        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        self.Entry = Entry
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32FirstW.restype = kernel.Process32NextW.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetSystemTimePreciseAsFileTime.restype = None
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD

        kernel.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel.QueryDosDeviceW.restype = wintypes.DWORD

    def identities(self):
        """Read parent PIDs and process sequences from one Windows snapshot."""
        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        query.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.LONG
        size = 65536
        while size <= 16777216:
            buffer = ctypes.create_string_buffer(size)
            returned = wintypes.ULONG()
            status = query(252, buffer, size, ctypes.byref(returned)) & 0xffffffff
            if status in (0xc0000004, 0xc0000023):  # Buffer too small.
                size = max(size * 2, returned.value)
                continue
            if status != 0:
                raise OSError(f"Windows process sequence snapshot unavailable (0x{status:08x})")
            if returned.value > size:
                raise ValueError("Windows process snapshot exceeded its buffer")
            return self._sequence_rows(buffer, returned.value)
        raise OSError("Windows process sequence snapshot exceeded its size limit")

    @staticmethod
    def _sequence_rows(buffer, used):
        # SystemBasicProcessInformation is supported on modern Windows 11.
        # Only this fixed header is read. Image pointers are not dereferenced.
        minimum = ctypes.sizeof(_BasicProcessInformation)
        alignment = ctypes.alignment(_BasicProcessInformation)
        offset = 0
        keys = {}
        while offset + minimum <= used:
            entry = _BasicProcessInformation.from_buffer_copy(buffer, offset)
            if entry.pid is not None:
                if entry.pid in keys:
                    raise ValueError("Duplicate PID in Windows process sequence snapshot")
                keys[entry.pid] = (entry.parent_pid or 0, entry.sequence)
            if entry.next_offset == 0 and keys:
                return keys
            if entry.next_offset < minimum or entry.next_offset % alignment:
                break
            offset += entry.next_offset
        raise ValueError("Incomplete Windows process sequence snapshot")

    def clock(self):
        now = wintypes.FILETIME()
        self.kernel.GetSystemTimePreciseAsFileTime(ctypes.byref(now))
        return _filetime(now)

    def snapshot(self):
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        rows = {}
        try:
            entry = self.Entry()
            entry.dwSize = ctypes.sizeof(entry)
            ok = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            while ok:
                pid = int(entry.th32ProcessID)
                if pid in rows:
                    raise ValueError("Duplicate process ID in Windows process snapshot")
                rows[pid] = (int(entry.th32ParentProcessID), entry.szExeFile)
                ok = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(snapshot)
        return rows

    def _open(self, pid, access, allow_departed=False, *, expected=None):
        handle = self.kernel.OpenProcess(access, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if allow_departed and code in {87, 1168}:
                return None
            raise ctypes.WinError(code)
        try:
            return _ProcessHandle(self.kernel, handle, pid, expected)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def pin(self, pid):
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE. No write access.
        return self._open(pid, 0x101000)

    def pin_exited(self, pid, expected):
        return self._open(pid, 0x101000, expected=expected)

    def __call__(self, executable):
        expected = path_key(executable)
        basename = ntpath.basename(expected)
        rows = self.snapshot()
        parent_ids = {ppid for ppid, _ in rows.values()}
        matches = []
        for pid, (_, name) in rows.items():
            if name.casefold() != basename:
                continue
            process = self._open(pid, 0x1000, allow_departed=True)
            if process is None:
                continue
            try:
                if path_key(process.executable) == expected:
                    matches.append({"pid": pid, "created": process.created,
                                    "executable": process.executable,
                                    "children_observed": pid in parent_ids})
            finally:
                process.close()
        return matches


class _ProcessLineage:
    """Prove outside ancestry using ordered snapshots of opaque process identities."""

    _MAX_SEQUENCES = 65536

    def __init__(self, owner_pid, baseline):
        self.owner_pid = owner_pid
        self.baseline = dict(baseline)
        self.previous = {}
        self.seen = {}
        self.external_parent = {}
        self.protected_pids = set()
        self.protected_sequences = set()

        # A prelaunch process under MO2 cannot establish a separate branch.
        children = {}
        for pid, (parent, _) in baseline.items():
            children.setdefault(parent, []).append(pid)
        owner_tree = {owner_pid}
        pending = [owner_pid]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in owner_tree:
                    owner_tree.add(child)
                    pending.append(child)
        self.observe(baseline)
        self.external_parent.update(
            (sequence, None) for pid, (_, sequence) in baseline.items() if pid not in owner_tree)

    def observe(self, rows, related_pids=()):
        if (len(rows) > self._MAX_SEQUENCES or self.owner_pid not in rows
                or rows[self.owner_pid] != self.baseline.get(self.owner_pid)):
            raise ValueError("Incomplete Windows ancestry snapshot or lost MO2 process identity")

        new_protected = set(related_pids) - self.protected_pids
        if new_protected:
            self.protected_sequences.update(
                sequence for sequence, (pid, _) in self.seen.items() if pid in new_protected)
            self.protected_pids.update(new_protected)

        for pid, (parent, sequence) in rows.items():
            if (type(pid) is not int or pid <= 0 or type(parent) is not int or parent < 0
                    or type(sequence) is not int or sequence < 0):
                raise ValueError("Invalid Windows process ancestry identity")
            if pid in self.protected_pids:
                self.protected_sequences.add(sequence)
            fingerprint = (pid, parent)
            if sequence in self.seen:
                if self.seen[sequence] != fingerprint or self.previous.get(pid) != (parent, sequence):
                    raise ValueError("Contradictory Windows process ancestry identity")
                continue
            if len(self.seen) >= self._MAX_SEQUENCES:
                raise ValueError("SKSE process ancestry history reached its capacity")
            self.seen[sequence] = fingerprint

            previous_parent = self.previous.get(parent)
            if (previous_parent is not None and previous_parent == rows.get(parent)
                    and previous_parent[1] in self.external_parent):
                # The same parent generation was alive before the child first
                # appeared and remains alive. Raw PID and wall-clock ordering
                # cannot prove a link to a departed or replaced parent.
                self.external_parent[sequence] = previous_parent[1]
        self.previous = dict(rows)

        decisions = {sequence: False for sequence in self.protected_sequences}
        return {pid for pid, entry in rows.items()
                if self.baseline.get(pid) != entry and not self._is_external(entry[1], decisions)}

    def _is_external(self, sequence, decisions):
        chain = []
        while sequence not in decisions and sequence in self.external_parent:
            chain.append(sequence)
            sequence = self.external_parent[sequence]
        external = decisions.get(sequence, sequence is None)
        for member in chain:
            decisions[member] = external
        return external


class _SkseLaunch:
    """Proof for one standard SKSE loader and its directly launched Skyrim process."""

    def __init__(self, observer, loader_path, game_path):
        self.observer = observer
        self.loader_path = loader_path
        self.game_path = game_path
        self.processes = {}
        self.before_launch = None
        self.lineage = None
        self.rapid_until = time.monotonic() + 5
        self.problem = None

    def close(self):
        for process in self.processes.values():
            process.close()
        self.processes.clear()
        self.lineage = None

    def reject(self, reason):
        self.problem = reason
        self.close()

    def observe(self, *, checking_completion=False):
        scan_started = self.observer.clock()
        rows = self.observer.snapshot()
        if not rows or (self.before_launch is not None and scan_started < self.before_launch):
            raise ValueError("Incomplete process snapshot or inconsistent Windows clock")
        paths = {ntpath.basename(path): path for path in (self.loader_path, self.game_path)}
        pinned_by_pid = {process.pid: process for process in self.processes.values()}
        for pid, (_, name) in rows.items():
            expected = paths.get(name.casefold())
            if expected is None or pid in pinned_by_pid:
                continue
            process = self.observer.pin(pid)
            keep = False
            try:
                if path_key(process.executable) != expected:
                    continue
                if self.before_launch is None:
                    raise ValueError("An SKSE or Skyrim process already existed before the MO2 launch")
                if expected in self.processes:
                    raise ValueError("Multiple SKSE or Skyrim process identities")
                if (process.pid != pid or type(process.created) is not int
                        or process.created <= self.before_launch):
                    raise ValueError("Process creation time does not match the launch")
                if process.created > scan_started:
                    # It may have started during enumeration or reused a PID
                    # from the snapshot. Require a fresh snapshot before use.
                    if checking_completion:
                        raise ValueError("A possible replacement process appeared during completion")
                    continue
                self.processes[expected] = process
                keep = True
            finally:
                if not keep:
                    process.close()
        if self.before_launch is None:
            self.lineage = _ProcessLineage(self.observer.owner_pid, self.observer.identities())
            self.before_launch = self.observer.clock()
            if self.before_launch < scan_started:
                raise ValueError("Windows clock moved during the prelaunch scan")
            return [], [], set()

        root = self.processes.get(self.loader_path)
        game = self.processes.get(self.game_path)
        if game is not None and root is None:
            # SKSE normally returns quickly. MO2 may still own a reference to
            # the exited process object, even if Toolhelp no longer lists it.
            parent, _ = rows[game.pid]
            if parent <= 0 or parent in rows:
                raise ValueError("Skyrim had an unexpected parent")
            root = self.observer.pin_exited(parent, self.loader_path)
            try:
                if (path_key(root.executable) != self.loader_path or root.pid != parent
                        or type(root.created) is not int or not self.before_launch < root.created < game.created):
                    raise ValueError("Skyrim's recorded parent was not the SKSE launch instance")
            except BaseException:
                root.close()
                raise
            self.processes[self.loader_path] = root
        exits = {process.pid: process.exit_time() for process in self.processes.values()}
        for process in self.processes.values():
            entry = rows.get(process.pid)
            if entry is None and exits[process.pid] is None:
                raise ValueError("A tracked live process was missing from the snapshot")
            if entry is not None and entry[1].casefold() != ntpath.basename(process.executable).casefold():
                raise ValueError("Process snapshot did not match a tracked executable")
            if checking_completion and entry is not None:
                # Reconcile a snapshot row still present after its tracked
                # handle has signaled. A replacement may never count as exit.
                current = self.observer.pin(process.pid)
                try:
                    if (current.pid != process.pid or current.created != process.created
                            or path_key(current.executable) != path_key(process.executable)
                            or current.exit_time() != exits[process.pid]):
                        raise ValueError("Snapshot identity conflicted with the tracked exit")
                finally:
                    current.close()
        if game is not None:
            if root is None or game.created <= root.created:
                raise ValueError("Skyrim was not linked to the recorded SKSE launcher")
            entry = rows.get(game.pid)
            if entry is not None and entry[0] != root.pid:
                raise ValueError("Skyrim had an unexpected parent")
            if exits[root.pid] is not None and game.created > exits[root.pid]:
                raise ValueError("Skyrim started after the recorded SKSE launcher exited")
        possible_launch_pids = {pid for pid, (_, name) in rows.items() if name.casefold() in paths}
        possible_launch_pids.update(exits)
        unresolved = self.lineage.observe(self.observer.identities(), possible_launch_pids)
        evidence = [{"pid": process.pid, "created": process.created, "executable": process.executable,
                     "exited": exits[process.pid]} for process in self.processes.values()]
        instances = [{k: v for k, v in item.items() if k != "exited"} for item in evidence
                     if item["exited"] is None]
        for instance in instances:
            instance["children_observed"] = instance["pid"] == root.pid and game is not None
        return instances, evidence, unresolved

    @property
    def paired(self):
        return len(self.processes) == 2


class RunningApplications:
    def __init__(self, observer=None, *, on_skse_event=None):
        self._on_skse_event = on_skse_event
        self._session_id = uuid.uuid4().hex if on_skse_event is not None else None
        self._launch_number = 0
        self.manual = set()
        self.managed_game = None
        self._skse_launches = {}
        self.records = {}
        self.global_uncertainty = None
        self._last_poll = 0.0
        try:
            self.observer = observer if observer is not None else WindowsProcessObserver()
        except Exception as error:
            self.observer = None
            self.observer_error = f"{type(error).__name__}: {error}"

    def add(self, value):
        self.manual.add(str(value))

    def discard(self, value):
        # Manual/fake guards only. A guessed executable string must not erase a
        # tracked launch or its unresolved lifecycle.
        self.manual.discard(str(value))

    def starting(self, executable):
        try:
            key = path_key(executable)
        except Exception as error:
            self.global_uncertainty = f"Unobservable MO2 launch: {error}"
            return
        if key not in self.records and len(self.records) >= 4096:
            self.global_uncertainty = "Launch tracking capacity reached; lifecycle is unconfirmed"
            return
        previous = self.records.get(key)
        if previous is not None and previous.get("completion_state") == "checking_survivors":
            # AboutToRun precedes the new spawn. Reconcile the old proof first
            # so a clean sequential launch gets its own prelaunch snapshot.
            self._observe(key, previous)
            self._discard_closed(key, previous)
        fresh = key not in self.records
        record = self.records.setdefault(key, {"executable": str(executable), "pending_attempts": 0,
                                               "instances": [], "observation_error": None,
                                               "baseline_ids": None, "observed_after_launch": False,
                                               "tree_uncertainty": False, "finish_uncertainty": None})
        previous_pending = record["pending_attempts"]
        record["pending_attempts"] += 1
        if fresh and ntpath.basename(key) == "skse64_loader.exe" and self.managed_game is not None:
            try:
                managed = self.managed_game()
                if managed.gameShortName().casefold() == "skyrimse":
                    directory = path_key(managed.gameDirectory().absolutePath())
                    if key == ntpath.join(directory, "skse64_loader.exe"):
                        self._skse_launches[key] = _SkseLaunch(
                            self.observer, key, ntpath.join(directory, "skyrimse.exe"))
            except Exception as error:
                record["finish_uncertainty"] = f"Managed Skyrim path could not be verified: {error}"
        skse = self._skse_launches.get(key)
        if skse is not None:
            if fresh:
                self._launch_number += 1
                record.update(launch_number=self._launch_number, completion_state="tracking",
                              first_uncertainty=None, completion_survivor_count=0,
                              completion_survivor_pids=[], last_observed_processes=[],
                              callback_exit_evidence=[])
                self._skse_event("start", record, pending_before=previous_pending)
            else:
                if previous_pending:
                    reason = "A new SKSE start arrived with an earlier finish callback outstanding"
                else:
                    reason = "A new SKSE start arrived while the previous launch was still unresolved"
                self._reject_skse(skse, record, reason)
                self._skse_event("additional_start", record, pending_before=previous_pending)
        self._observe(key, record)

    def finished(self, executable, exit_code=None):
        try:
            key = path_key(executable)
        except Exception:
            self.global_uncertainty = "Unmatched finish callback with invalid executable identity"
            return
        record = self.records.get(key)
        if record is None:
            return
        skse = self._skse_launches.get(key)
        if skse is not None:
            self._finish_skse(key, record, skse, exit_code)
            return
        self._observe(key, record)
        if record["pending_attempts"]:
            record["pending_attempts"] -= 1
        # 259 means STILL_ACTIVE. MO2 may also invoke finished callbacks after
        # Force Unlock. An unobserved root/wrapper or observed child cannot be
        # certified by this path-only callback. Do not infer cancellation.
        if (not record["observed_after_launch"] or record["tree_uncertainty"]
                or type(exit_code) is not int or exit_code in {259, -1, 0xffffffff}):
            record["finish_uncertainty"] = "MO2 finish did not establish observable process/tree completion; restart after closing tools"
        self._discard_closed(key, record)

    def _finish_skse(self, key, record, skse, exit_code):
        previous_pending = record["pending_attempts"]
        if previous_pending:
            record["pending_attempts"] -= 1
        if skse.problem is None:
            if previous_pending != 1 or type(exit_code) is not int or exit_code != 0:
                self._reject_skse(skse, record, "SKSE finish did not match one pending launch with a zero exit code")
            else:
                self._verify_callback_exits(key, record, skse)
        self._skse_event("finish", record, pending_before=previous_pending,
                         exit_code=exit_code if type(exit_code) is int else None)
        if record["completion_state"] == "checking_survivors":
            # Scan after the exact producer exits, including processes that
            # may have appeared during the callback-time observations.
            self._observe(key, record)
        self._discard_closed(key, record)

    def _verify_callback_exits(self, key, record, skse):
        early = "MO2 finished before SKSE and Skyrim exits could be verified"
        if not skse.paired:
            self._reject_skse(skse, record, early)
            return
        # Force Unlock can return the loader's zero exit code. Inspect the
        # existing handles before a full scan, so even a wall-clock jump
        # during that scan cannot turn a running process into callback proof.
        try:
            callback_time = self.observer.clock()
            if type(callback_time) is not int or callback_time <= 0:
                raise ValueError("Invalid Windows clock at the SKSE finish callback")
            game = skse.processes[skse.game_path]
            loader = skse.processes[skse.loader_path]
            exits = {game.pid: game.exit_time(), loader.pid: loader.exit_time()}
        except Exception as error:
            self._reject_skse(skse, record, f"{type(error).__name__}: {error}", observation=True)
            return
        if any(exit_time is None or exit_time > callback_time for exit_time in exits.values()):
            self._reject_skse(skse, record, early)
            return
        self._observe(key, record)
        if skse.problem is not None:
            return
        evidence = record["last_observed_processes"]
        if {item["pid"]: item["exited"] for item in evidence} != exits:
            self._reject_skse(skse, record, "SKSE exit evidence changed during the callback", observation=True)
            return
        record["callback_exit_evidence"] = evidence
        record["completion_state"] = "checking_survivors"

    def _reject_skse(self, skse, record, reason, *, observation=False):
        if skse.problem is not None:
            return
        skse.reject(reason)
        # The last verified identities remain diagnostic even after we have
        # closed the handles for a launch that cannot safely be recovered.
        record["instances"] = []
        if observation:
            record["observation_error"] = reason
        record["first_uncertainty"] = record["first_uncertainty"] or reason
        record["finish_uncertainty"] = reason + "; restart after closing tools"
        record["completion_state"] = "restart_required"
        self._skse_event("restart_required", record)

    def _skse_event(self, event, record, **details):
        if self._on_skse_event is None:
            return
        try:
            self._on_skse_event(deepcopy({
                "event": event, "session_id": self._session_id, "launch_number": record["launch_number"],
                "executable": record["executable"], "pending_attempts": record["pending_attempts"],
                "state": record["completion_state"], "first_uncertainty": record["first_uncertainty"],
                "reason": record["finish_uncertainty"], "last_observed_processes": record["last_observed_processes"],
                "callback_exit_evidence": record["callback_exit_evidence"],
                "survivor_count": record["completion_survivor_count"],
                "survivor_pids": record["completion_survivor_pids"], **details,
            }))
        except Exception:
            # Diagnostics never influence launch or write authorization.
            pass

    def _discard_closed(self, key, record):
        if (not record["pending_attempts"] and not record["instances"]
                and not record["observation_error"] and not record["finish_uncertainty"]
                and record.get("completion_state") != "checking_survivors"):
            self.records.pop(key, None)
            skse = self._skse_launches.pop(key, None)
            if skse is not None:
                skse.close()

    def poll(self):
        now = time.monotonic()
        interval = .1 if any(now < launch.rapid_until and not launch.paired and not launch.problem
                              for launch in self._skse_launches.values()) else .5
        if now - self._last_poll < interval:
            return
        self._last_poll = now
        for key, record in list(self.records.items()):
            self._observe(key, record)
            self._discard_closed(key, record)

    def _observe(self, key, record):
        skse = self._skse_launches.get(key)
        if skse is not None:
            if skse.problem is None:
                self._observe_skse(skse, record)
            return
        try:
            if self.observer is None:
                raise OSError(getattr(self, "observer_error", "Process observation unavailable"))
            observed = self.observer(record["executable"])
            seen = set()
            for instance in observed:
                identity = (instance["pid"], instance["created"])
                if (type(identity[0]) is not int or type(identity[1]) is not int or identity[0] <= 0
                        or identity[1] < 0 or path_key(instance["executable"]) != path_key(record["executable"])
                        or identity in seen):
                    raise ValueError("Invalid or ambiguous observed process instance")
                seen.add(identity)
            if record["baseline_ids"] is None:
                record["baseline_ids"] = sorted(seen)
            elif seen - {tuple(item) for item in record["baseline_ids"]}:
                record["observed_after_launch"] = True
            if any(instance.get("children_observed", False) for instance in observed):
                record["tree_uncertainty"] = True
            record["instances"] = list(observed)
            record["observation_error"] = None
        except Exception as error:
            # Preserve last known instances and block; failure is not absence.
            record["observation_error"] = f"{type(error).__name__}: {error}"

    def _observe_skse(self, skse, record):
        checking = record["completion_state"] == "checking_survivors"
        try:
            instances, evidence, survivors = skse.observe(checking_completion=checking)
            if checking and (instances or evidence != record["callback_exit_evidence"]):
                raise ValueError("SKSE exit evidence changed during completion")
        except Exception as error:
            self._reject_skse(skse, record, f"{type(error).__name__}: {error}", observation=True)
            return
        record["instances"] = instances
        record["last_observed_processes"] = evidence
        record["observed_after_launch"] = bool(evidence)
        record["tree_uncertainty"] = bool(evidence) and not skse.paired
        if checking:
            was_waiting = record["finish_uncertainty"] is not None
            record["completion_survivor_count"] = len(survivors)
            record["completion_survivor_pids"] = sorted(survivors)[:16]
            if survivors:
                reason = "Waiting for surviving processes that could not be excluded from the SKSE launch"
                record["first_uncertainty"] = record["first_uncertainty"] or reason
                record["finish_uncertainty"] = reason
                if not was_waiting:
                    self._skse_event("waiting_for_survivors", record)
            else:
                record["finish_uncertainty"] = None
                record["completion_state"] = "verified"
                self._skse_event("recovered" if was_waiting else "verified", record)

    def _names(self):
        names = set(self.manual)
        if self.global_uncertainty:
            names.add("[launch tracking unconfirmed] " + self.global_uncertainty)
        for key, record in list(self.records.items()):
            self._observe(key, record)
            if record["instances"]:
                names.add(record["executable"])
            if (record["pending_attempts"] > len(record["instances"]) or record["observation_error"]
                    or record["finish_uncertainty"] or record.get("completion_state") == "checking_survivors"):
                names.add(record["executable"] + " [launch outcome unconfirmed]")
            self._discard_closed(key, record)
        return names

    def __bool__(self):
        return bool(self._names())

    def __iter__(self):
        return iter(self._names())

    def describe(self):
        self._names()
        return {"scope": "MO2 callback launches; exact executable instances observed read-only on Windows",
                "external_launches_certified": False, "pid_identity": "PID plus process creation time and full path",
                "cancellation_observable": False, "arbitrary_descendant_tree_certified": False,
                "skse_tracking": "standard managed Skyrim SE loader and directly launched game; "
                                 "require verified exits at the finish callback and exclude surviving processes",
                "unconfirmed_policy": "block changes; no time-based expiry or guessed cancellation",
                "restart_required_for_unobservable_canceled_launch": True,
                "uncertainty": self.global_uncertainty,
                "launches": deepcopy(list(self.records.values()))}
