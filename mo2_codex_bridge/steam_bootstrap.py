"""Identify MO2 2.5.2's unhooked Steam bootstrap after a verified SKSE exit."""
import ntpath
from collections import defaultdict


_WEBHELPER = "steamwebhelper.exe"
_WEBHELPER_LAYOUTS = ("cef.win64", "cef.win7x64", "cef.win7")


class SteamBootstrap:
    """Certify exact client/helper generations without exempting their children."""

    def __init__(self, observer):
        self.observer = observer
        self._root = None

    def capture_start(self, lineage):
        try:
            executable = self.observer.steam_executable()
            owner_sequence = lineage.baseline[lineage.owner_pid][1]
            owner = lineage._get_birth(lineage.owner_pid, owner_sequence)
            start = self.observer.interrupt_clock()
        except (OSError, ValueError):
            return None
        if executable is None or owner is None or not start or start <= owner[1]:
            return None
        return owner[0], start, executable

    def excluded(self, lineage, toolhelp_rows, loader, start):
        if start is None:
            return set()
        try:
            return self._excluded(lineage, toolhelp_rows, loader, start)
        except (OSError, ValueError):
            # Optional evidence should never replace the normal survivor guard.
            return set()

    def _excluded(self, lineage, toolhelp_rows, loader, start):
        boot, boundary, steam_exe = start
        if self.observer.steam_executable() != steam_exe:
            return set()

        rows = lineage.previous
        owner_sequence = rows[lineage.owner_pid][1]
        direct_clients = []
        helpers = defaultdict(list)
        for pid, (parent, name) in toolhelp_rows.items():
            entry = rows.get(pid)
            if entry is None or entry[0] != parent:
                continue
            name = name.casefold()
            if parent == lineage.owner_pid and name == "steam.exe":
                direct_clients.append((pid, entry[1]))
            elif name == _WEBHELPER:
                helpers[parent].append((pid, entry[1]))
        if len(direct_clients) != 1:
            return set()

        pid, sequence = direct_clients[0]
        root_key = (pid, sequence, boot, owner_sequence, steam_exe)
        if (sequence in lineage.protected_sequences
                or lineage._first_parents.get(sequence) != owner_sequence):
            return set()
        birth = lineage._get_birth(pid, sequence)
        if birth is None or birth[0] != boot:
            return set()
        if root_key != self._root:
            # v2.5.2 calls beforeRun, checkSteam, then starts the requested
            # executable. checkSteam spawns SteamExe with hooked=false.
            loader_birth = self.observer.pinned_birth(loader)
            if (lineage.baseline.get(pid) == rows[pid] or loader_birth is None
                    or loader_birth[0] != boot or not boundary < birth[1] < loader_birth[1]):
                return set()
        if self.observer.process_image(pid, sequence) != steam_exe:
            return set()
        self._root = root_key

        path = ntpath.dirname(steam_exe)
        helper_paths = {ntpath.join(path, "bin", "cef", layout, _WEBHELPER)
                        for layout in _WEBHELPER_LAYOUTS}
        excluded = {pid}
        parents = [(pid, sequence, birth[1])]
        # Bound optional queries on the GUI thread. Every unchecked process
        # keeps its ordinary survivor classification.
        remaining = 64
        while parents and remaining:
            parent, parent_sequence, parent_birth = parents.pop()
            for child, child_sequence in helpers.get(parent, ()):
                if not remaining:
                    break
                remaining -= 1
                if (child in excluded or child_sequence in lineage.protected_sequences
                        or lineage._first_parents.get(child_sequence) != parent_sequence):
                    continue
                birth = lineage._get_birth(child, child_sequence)
                if birth is None or birth[0] != boot or birth[1] <= parent_birth:
                    continue
                if self.observer.process_image(child, child_sequence) not in helper_paths:
                    continue
                excluded.add(child)
                parents.append((child, child_sequence, birth[1]))
        return excluded
