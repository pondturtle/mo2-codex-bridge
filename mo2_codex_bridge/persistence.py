"""Commit Skyrim SE plugin changes through MO2 and verify its profile files.

MO2 2.5.2 setLoadOrder/setState omit the writePluginsList signal. Public
setPriority at an ordinary ESP's existing priority refreshes indexes and emits
that signal without changing order. Never write MO2's list files here.
"""
from pathlib import Path


class PluginPersistence:
    def __init__(self, organizer, mobase, wait_until):
        self.o, self.m, self.wait = organizer, mobase, wait_until
        self.profile = (organizer.profileName(), organizer.profilePath())
        self.pl = organizer.pluginList()
        if not callable(wait_until) or not callable(getattr(self.pl, 'setPriority', None)):
            raise RuntimeError('Plugin persistence support unavailable; no plugin changes made')
        # This verifier implements Skyrim SE asterisk plugin-list semantics.
        game = organizer.managedGame()
        if game.gameShortName().casefold() not in {'skyrimse'}:
            raise RuntimeError('Verified plugin persistence supports Skyrim SE only')
        self.primary = {n.casefold() for n in game.primaryPlugins()}
        self.anchor = next((n for n in self.pl.pluginNames()
                            if n.casefold().endswith('.esp')
                            and n.casefold() not in self.primary
                            and not self.pl.isMasterFlagged(n)
                            and not self.pl.hasMasterExtension(n)), None)
        if self.anchor is None:
            raise RuntimeError('No ordinary ESP available for a safe MO2 save signal; no plugin changes made')

    def check_profile(self):
        if (self.o.profileName(), self.o.profilePath()) != self.profile:
            raise RuntimeError('Profile changed during plugin commit; inspect both profiles before retrying')

    def commit(self, order, active):
        order = list(order)
        active = dict(active)
        expected = [n.casefold() for n in order]
        states = {n.casefold(): enabled for n, enabled in active.items()}

        def check_live():
            self.check_profile()
            actual = sorted(self.pl.pluginNames(), key=self.pl.priority)
            if actual != order or any((self.pl.state(n) == self.m.PluginState.ACTIVE) != active[n]
                                      for n in actual):
                raise RuntimeError('MO2 plugin state differs from the request or changed during commit; inspect live state before retrying')

        check_live()
        if self.anchor not in order:
            raise RuntimeError('Save anchor disappeared; plugin persistence is unconfirmed')
        priority = self.pl.priority(self.anchor)
        if self.pl.setPriority(self.anchor, priority) is False:
            raise RuntimeError('MO2 rejected the save signal; plugin persistence is unconfirmed')
        check_live()
        enabled = [n for n in order if active[n]]
        if any(self.pl.loadOrder(n) != i for i, n in enumerate(enabled)) or any(
                self.pl.loadOrder(n) != -1 for n in order if not active[n]):
            raise RuntimeError('MO2 derived plugin indexes differ from requested order; persistence is unconfirmed')

        reason = ['Profile files have not been saved']

        def saved():
            check_live()
            matches, why = self.saved_matches(order, active)
            reason[0] = why
            return matches

        if not self.wait(saved, 5000):
            raise RuntimeError('MO2 plugin changes applied in memory but saving was not confirmed: '
                               + reason[0] + '. Inspect live state before retrying; no automatic rollback was attempted.')
        # The waiter may process events after a successful predicate. Recheck
        # BOTH live and saved state without triggering another native write.
        check_live()
        if not saved():
            raise RuntimeError('Saved plugin state changed after verification: ' + reason[0])
        return {'persisted': True, 'verified_files': ['loadorder.txt', 'plugins.txt'],
                'commit_method': 'MO2 setPriority at unchanged ESP priority'}

    def saved_matches(self, order, active):
        """Read-only saved-state check; safe to combine with another Qt waiter."""
        self.check_profile()
        expected = [n.casefold() for n in order]
        states = {n.casefold(): enabled for n, enabled in active.items()}
        try:
            loadorder = self.read_lines('loadorder.txt')
            plugins = self.read_lines('plugins.txt')
        except (OSError, UnicodeError) as error:
            return False, 'Profile files cannot be read: ' + str(error)
        return saved_plugin_rows_match(loadorder, plugins, expected, states, self.primary)

    def read_lines(self, name):
        return plugin_file_lines((Path(self.profile[1]) / name).read_bytes())


def plugin_file_lines(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        text = data.decode('cp1252')
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def saved_plugin_rows_match(loadorder, plugins, expected, states, primary):
    """Compare Skyrim SE native list rows against a previously observed live state."""
    if [n.casefold() for n in loadorder] != expected:
        return False, 'loadorder.txt does not match the complete requested order'
    entries = [(n[1:] if n.startswith('*') else n).casefold() for n in plugins]
    listed = set(entries)
    if len(entries) != len(listed) or any(n not in states for n in entries):
        return False, 'plugins.txt contains duplicate or unexpected plugins'
    if entries != [n for n in expected if n in listed]:
        return False, 'plugins.txt order differs from the requested order'
    written = {n: line.startswith('*') for n, line in zip(entries, plugins)}
    for name, is_active in states.items():
        if name not in written:
            if is_active and name not in primary:
                return False, 'plugins.txt is missing an active plugin: ' + name
        elif written[name] != is_active:
            return False, 'plugins.txt activation differs from the request: ' + name
    return True, ''
