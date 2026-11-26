"""Bounded saved-profile observations using disposable fixtures, never live MO2."""
import hashlib
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_bridge  # Supplies the package bootstrap used by the offline suite.
from mo2_codex_bridge import profile_usage as usage


class NativeMod:
    def __init__(self, root, name):
        self._name, self.path = name, root / name
        self.path.mkdir()
        self.flags = set()
    def name(self): return self._name
    def absolutePath(self): return str(self.path)
    def isForeign(self): return 'foreign' in self.flags
    def isBackup(self): return 'backup' in self.flags
    def isOverwrite(self): return 'overwrite' in self.flags
    def isSeparator(self): return 'separator' in self.flags


class NativeMods:
    def __init__(self, root):
        self.mods = {name: NativeMod(root, name) for name in ('A', 'OtherMod')}
        self.active = {'A'}
    def allMods(self): return list(self.mods)
    def getMod(self, name): return self.mods.get(name)
    def state(self, name): return 1 if name in self.active else 0
    def setActive(self, *args): raise AssertionError('A read report must not change mods')


class CurrentPlugins:
    def __init__(self):
        self.active = {'A.esp', 'Dep.esp'}
        self.origins = {'A.esp': 'A', 'Dep.esp': 'OtherMod'}
        self.master_names = {'A.esp': [], 'Dep.esp': ['a.ESP']}
        self.calls = []
    def pluginNames(self): return ['A.esp', 'Dep.esp', 'Dormant.esp']
    def state(self, name): return 1 if name in self.active else 0
    def origin(self, name):
        self.calls.append(('origin', name))
        if name == 'Dormant.esp': raise AssertionError('Inactive origin is not evidence')
        return self.origins[name]
    def masters(self, name):
        self.calls.append(('masters', name))
        if name == 'Dormant.esp': raise AssertionError('Inactive masters are not evidence')
        return self.master_names[name]


class FakeOrganizer:
    def __init__(self, root):
        self.root = root
        self.profile_root = root / 'profiles'; self.profile_root.mkdir()
        self.mods_root = root / 'mods'; self.mods_root.mkdir()
        self.ml, self.pl = NativeMods(self.mods_root), CurrentPlugins()
        self.winners = {'A.esp': self.mods_root / 'A' / 'A.esp',
                        'Dep.esp': self.mods_root / 'OtherMod' / 'Dep.esp'}
        for path in self.winners.values(): path.write_bytes(b'fixture only; headers never read')
        self.resolve_calls = []
    def modList(self): return self.ml
    def pluginList(self): return self.pl
    def modsPath(self): return str(self.mods_root)
    def profilePath(self): return str(self.profile_root / 'Current')
    def profileName(self): return 'Current'
    def resolvePath(self, name):
        self.resolve_calls.append(name)
        if name == 'Dormant.esp': raise AssertionError('Inactive plugin must not be resolved')
        return str(self.winners[name])
    def selectProfile(self, *args): raise AssertionError('Profile switch is forbidden')
    def refresh(self): raise AssertionError('Refresh is forbidden')
    def profile(self): raise AssertionError('No profile construction or flush is permitted')


class ProfileUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='profile-usage-')
        self.root = Path(self.temp.name)
        self.o = FakeOrganizer(self.root)
        self.ops = types.SimpleNamespace(o=self.o, m=types.SimpleNamespace(
            ModState=types.SimpleNamespace(ACTIVE=1), PluginState=types.SimpleNamespace(ACTIVE=1)))
        self.saved('Current', b'# Native\r\n+A\r\n-OtherMod\r\n')
        self.saved('Other', b'-A\n+OtherMod\n')
    def tearDown(self): self.temp.cleanup()
    def saved(self, profile, data):
        directory = self.o.profile_root / profile; directory.mkdir(exist_ok=True)
        (directory / 'modlist.txt').write_bytes(data)
        return directory / 'modlist.txt'
    def report(self, mod='A'): return usage.report_mod_profile_usage(self.ops, {'mod': mod})
    def row(self, result, name): return next(row for row in result['profiles'] if row['name'] == name)

    def test_saved_live_and_absent_are_independent_without_mutation(self):
        self.saved('Empty', b'# no entry\n')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.report()
        self.assertTrue(result['saved_usage_complete'])
        self.assertEqual(self.row(result, 'Other')['saved']['state'], 'disabled')
        self.assertEqual(self.row(result, 'Empty')['saved']['state'], 'absent')
        self.assertEqual(result['current_profile']['live_state'], 'enabled')
        self.assertTrue(result['current_profile']['live_saved_agree'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_absent_is_literal_saved_entry_not_live_or_future_disabled_state(self):
        self.saved('Other', b'')
        result = self.report()
        self.assertEqual(self.row(result, 'Other')['saved']['state'], 'absent')
        self.assertEqual(result['current_profile']['live_state'], 'enabled')
        self.assertTrue(any('not proof' in item for item in result['limitations']))

    def test_missing_current_directory_cannot_be_complete_with_only_other_profiles(self):
        current = self.o.profile_root / 'Current'
        (current / 'modlist.txt').unlink(); current.rmdir()
        result = self.report()
        self.assertEqual([row['name'] for row in result['profiles']], ['Other'])
        self.assertEqual(result['current_profile']['saved']['state'], 'unknown')
        self.assertFalse(result['saved_usage_complete'])

    def test_current_unsaved_state_is_not_substituted_for_saved_state(self):
        self.saved('Current', b'-A\n')
        current = self.report()['current_profile']
        self.assertEqual((current['live_state'], current['saved']['state']), ('enabled', 'disabled'))
        self.assertFalse(current['live_saved_agree'])

    def test_missing_modlist_is_unknown_not_absent_and_not_silently_dropped(self):
        (self.o.profile_root / 'Missing').mkdir()
        result = self.report()
        self.assertEqual(self.row(result, 'Missing')['saved']['state'], 'unknown')
        self.assertFalse(result['saved_usage_complete'])

    def test_pinned_byte_trim_foreign_colon_labels_and_one_marker(self):
        self.saved('Current', b' \t*DLC: Dragonborn\t\r\n\t +A\t \r\n')
        self.assertEqual(self.report()['current_profile']['saved']['state'], 'enabled')
        plus = NativeMod(self.o.mods_root, '+A'); self.o.ml.mods['+A'] = plus
        self.saved('Other', b'++A\n')
        self.assertEqual(self.row(self.report('+A'), 'Other')['saved']['state'], 'enabled')

    def test_legacy_bare_name_is_explicitly_identified(self):
        self.saved('Current', b' A \r\n')
        saved = self.report()['current_profile']['saved']
        self.assertEqual(saved['state'], 'enabled')
        self.assertTrue(saved['legacy_unmarked'])

    def test_bom_before_data_is_unknown_and_bom_comment_has_no_state(self):
        self.saved('Current', b'\xef\xbb\xbf+A\n')
        self.assertEqual(self.report()['current_profile']['saved']['state'], 'unknown')
        self.saved('Current', b'\xef\xbb\xbf# comment\n-A\n')
        self.assertEqual(self.report()['current_profile']['saved']['state'], 'disabled')

    def test_unicode_whitespace_does_not_become_native_ascii_trim(self):
        self.saved('Current', '\u00a0+A\n'.encode('utf-8'))
        self.assertEqual(self.report()['current_profile']['saved']['state'], 'absent')

    def test_non_native_unicode_line_separator_is_not_a_new_line(self):
        self.saved('Current', 'Other\u2028+A\n'.encode('utf-8'))
        self.assertEqual(self.report()['current_profile']['saved']['state'], 'absent')

    def test_duplicate_or_case_alias_entries_are_unknown(self):
        for data in (b'+A\n-A\n', b'+A\n-a\n', b'+a\n'):
            with self.subTest(data=data):
                self.saved('Current', data)
                self.assertEqual(self.report()['current_profile']['saved']['state'], 'unknown')

    def test_encoding_alias_is_unknown_without_renaming_or_guessing(self):
        mod = NativeMod(self.o.mods_root, 'Caf\u00e9'); self.o.ml.mods[mod.name()] = mod
        self.saved('Other', '+Caf\u00c3\u00a9\n'.encode('utf-8'))
        self.assertEqual(self.row(self.report(mod.name()), 'Other')['saved']['state'], 'unknown')
        self.saved('Other', '+Caf\u00e9\n'.encode('utf-8'))
        self.assertEqual(self.row(self.report(mod.name()), 'Other')['saved']['state'], 'enabled')

    def test_foreign_marker_for_regular_target_is_unknown(self):
        self.saved('Current', b'*A\n')
        self.assertEqual(self.report()['current_profile']['saved']['state'], 'unknown')

    def test_invalid_utf8_or_nul_is_unknown(self):
        for data in (b'+A\n\xffOther\n', b'+A\x00ignored\n', b'+\n'):
            self.saved('Current', data)
            self.assertEqual(self.report()['current_profile']['saved']['state'], 'unknown')

    def test_size_and_total_budget_limits_do_not_guess_absence(self):
        with patch.object(usage, 'MAX_MODLIST_BYTES', 3):
            self.assertFalse(self.report()['saved_usage_complete'])
        with patch.object(usage, 'MAX_TOTAL_BYTES', 1):
            result = self.report()
            self.assertTrue(all(row['saved']['state'] == 'unknown' for row in result['profiles']))

    def test_profile_count_limit_is_explicitly_incomplete(self):
        with patch.object(usage, 'MAX_PROFILES', 1):
            result = self.report()
        self.assertFalse(result['discovery']['complete'])
        self.assertFalse(result['saved_usage_complete'])

    def test_link_or_reparse_guard_prevents_opening_suspicious_file(self):
        other = self.o.profile_root / 'Other' / 'modlist.txt'
        real = usage._safe_stat
        def checked(path, directory=False):
            if Path(path) == other: raise ValueError('reparse fixture')
            return real(path, directory)
        with patch.object(usage, '_safe_stat', side_effect=checked):
            result = self.report()
        self.assertEqual(self.row(result, 'Other')['saved']['state'], 'unknown')
        self.assertEqual(self.row(result, 'Current')['saved']['state'], 'enabled')

    def test_hardlinked_saved_list_is_unknown_before_content_read(self):
        source = self.root / 'shared-data.txt'; source.write_bytes(b'+A\n')
        directory = self.o.profile_root / 'Linked'; directory.mkdir()
        destination = directory / 'modlist.txt'; os.link(source, destination)
        original = Path.open
        def checked(path, *args, **kwargs):
            if Path(path) == destination: raise AssertionError('A hardlinked list was opened')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', checked): result = self.report()
        saved = self.row(result, 'Linked')['saved']
        self.assertEqual(saved['state'], 'unknown')
        self.assertIn('hard link', saved['reason'])

    def test_changed_handle_metadata_marks_file_unknown(self):
        real = os.fstat; calls = [0]
        def fstat(fd):
            item = real(fd); calls[0] += 1
            if calls[0] == 2:
                return types.SimpleNamespace(st_dev=item.st_dev, st_ino=item.st_ino,
                    st_size=item.st_size, st_mtime_ns=item.st_mtime_ns + 1, st_nlink=item.st_nlink)
            return item
        with patch.object(usage.os, 'fstat', side_effect=fstat): result = self.report()
        self.assertEqual(self.row(result, 'Current')['saved']['state'], 'unknown')

    def test_active_provider_and_active_dependents_only(self):
        plugins = self.report()['current_active_plugins']
        self.assertEqual(plugins['status'], 'complete')
        self.assertEqual([row['name'] for row in plugins['provided_by_mod']], ['A.esp'])
        self.assertEqual(plugins['provided_by_mod'][0]['active_dependents'], ['Dep.esp'])
        self.assertTrue(plugins['provided_by_mod'][0]['dependents_complete'])
        self.assertNotIn('Dormant.esp', self.o.resolve_calls)
        self.assertFalse(any(name == 'Dormant.esp' for _, name in self.o.pl.calls))

    def test_contradictory_origin_or_winner_is_not_certified(self):
        self.o.pl.origins['A.esp'] = 'OtherMod'
        result = self.report()['current_active_plugins']
        self.assertEqual(result['provided_by_mod'], [])
        self.assertIn('disagree', result['unknown'][0]['reason'])
        self.o.pl.origins['A.esp'] = 'A'
        self.o.winners['A.esp'] = self.o.mods_root / 'OtherMod' / 'A.esp'
        result = self.report()['current_active_plugins']
        self.assertEqual(result['provided_by_mod'], [])
        self.assertIn('disagree', result['unknown'][0]['reason'])

    def test_out_of_mods_native_path_is_rejected_before_any_disk_stat(self):
        outside = self.root / 'outside'; outside.mkdir(); self.o.ml.mods['A'].path = outside
        real = usage._safe_stat; inspected = []
        def checked(path, directory=False):
            inspected.append(Path(path)); self.assertNotEqual(Path(path), outside)
            return real(path, directory)
        with patch.object(usage, '_safe_stat', side_effect=checked):
            result = self.report()['current_active_plugins']
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['provided_by_mod'], [])

    def test_context_change_invalidates_current_live_plugin_claims(self):
        with patch.object(self.o, 'profileName', side_effect=['Current', 'Other']):
            result = self.report()
        self.assertEqual(result['current_profile']['live_state'], 'unknown')
        self.assertEqual(result['current_active_plugins']['status'], 'unknown')
        self.assertFalse(result['saved_usage_complete'])

    def test_params_and_nonregular_targets_are_rejected(self):
        for params in ({}, {'mod': 'a'}, {'mod': '../A'}, {'mod': 'A', 'profile': 'Other'}):
            with self.assertRaises(ValueError): usage.report_mod_profile_usage(self.ops, params)
        for flag in ('foreign', 'backup', 'overwrite', 'separator'):
            self.o.ml.mods['A'].flags = {flag}
            with self.assertRaises(ValueError): self.report()

    def test_active_plugin_response_limit_is_explicit(self):
        with patch.object(usage, 'MAX_REPORTED_PLUGINS', 0):
            plugins = self.report()['current_active_plugins']
        self.assertEqual(plugins['status'], 'unknown')
        self.assertTrue(plugins['unknown_truncated'])
        self.assertEqual(plugins['unknown_count'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)