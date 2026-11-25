"""Read back MO2 native saved mod order without writing profile lists."""
from pathlib import Path

from .common import contained_path


class ModListPersistence:
    def __init__(self, organizer, wait_until):
        if not callable(wait_until):
            raise RuntimeError('Mod-list verification waiter unavailable; no changes made')
        self.o, self.wait = organizer, wait_until
        self.profile = (organizer.profileName(), organizer.profilePath())
        self.path = contained_path(self.profile[1], 'modlist.txt')
        if not self.path.is_file():
            raise RuntimeError('Saved modlist.txt is unavailable; no changes made')

    def check_profile(self):
        if (self.o.profileName(), self.o.profilePath()) != self.profile:
            raise RuntimeError('Profile changed during mod-list verification')
        if contained_path(self.profile[1], 'modlist.txt') != self.path:
            raise RuntimeError('Mod-list destination changed during verification')

    def verify(self, mods, check_live):
        # MO2 writes descending priorities. Its native writer excludes automatic
        # priority entries (backup/Overwrite); snapshots likewise exclude them.
        expected = []
        names = set()
        for mod in sorted(mods, key=lambda item: item['priority'], reverse=True):
            if mod['overwrite']:
                continue
            name = mod['name'].casefold()
            if name in names:
                raise ValueError('Duplicate mod identity in saved-state verification')
            names.add(name)
            expected.append(('*' if mod['foreign'] else '+' if mod['active'] else '-', name))
        reason = ['Saved mod order has not matched the planned state']

        def saved():
            self.check_profile()
            check_live()
            try:
                data = self.path.read_bytes()
                if len(data) > 16 * 1024 * 1024:
                    reason[0] = 'modlist.txt exceeds the verification size limit'
                    return False
                text = data.decode('utf-8-sig')
            except (OSError, UnicodeError) as error:
                reason[0] = 'modlist.txt cannot be read: ' + str(error)
                return False
            actual = []
            seen = set()
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                if len(line) < 2 or line[0] not in '+-*':
                    reason[0] = 'modlist.txt has an invalid entry'
                    return False
                key = line[1:].casefold()
                if not key or key in seen:
                    reason[0] = 'modlist.txt has an empty or duplicate mod'
                    return False
                seen.add(key)
                actual.append((line[0], key))
            if actual != expected:
                reason[0] = 'modlist.txt order or enabled states differ from the plan'
                return False
            return True

        if not self.wait(saved, 5000):
            raise RuntimeError('Mod changes applied but saved modlist.txt was not confirmed: '
                               + reason[0] + '. Inspect live state before retrying; no rollback was attempted.')
        # Qt may process more events before its waiter returns.
        if not saved():
            raise RuntimeError('Saved modlist changed after verification: ' + reason[0])
        return {'modlist_persisted': True, 'verified_mod_files': ['modlist.txt']}
