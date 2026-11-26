"""Offline metadata-only native backup inventory and conservative association."""
import copy
import itertools
import unittest
from unittest import mock

from mo2_codex_bridge import native_backups


class FakeMod:
    def __init__(self, name, *, backup=False, foreign=False, overwrite=False,
                 separator=False, path=None):
        self.values = {'name': name, 'absolutePath': path or 'G:\\Mods\\' + name,
                       'isBackup': backup, 'isForeign': foreign, 'isOverwrite': overwrite,
                       'isSeparator': separator}
        self.calls = []

    def __getattr__(self, name):
        if name in self.values:
            def get():
                self.calls.append(name)
                return self.values[name]
            return get
        raise AssertionError('Unexpected mod API: ' + name)


class FakeVersion:
    def __init__(self, value='2.5.2'):
        self.value, self.calls = value, []

    def displayString(self, forced_segments=2):
        self.calls.append(forced_segments)
        return self.value


class FakeList:
    def __init__(self, mods):
        self.mods = {mod.values['name']: mod for mod in mods}
        self.calls = []
        self.names = list(self.mods)

    def allMods(self):
        self.calls.append(('allMods',))
        return self.names[:]

    def getMod(self, name):
        self.calls.append(('getMod', name))
        return self.mods.get(name)

    def displayName(self, name):
        self.calls.append(('displayName', name))
        return self.mods[name].values['name']


class FakeOrganizer:
    def __init__(self, mods=(), version='2.5.2'):
        self.list = FakeList(mods)
        self.version = FakeVersion(version)
        self.root = 'G:\\Mods'
        self.calls = []

    def appVersion(self):
        self.calls.append('appVersion')
        return self.version

    def modsPath(self):
        self.calls.append('modsPath')
        return self.root

    def modList(self):
        self.calls.append('modList')
        return self.list


class NativeBackupInventoryTests(unittest.TestCase):
    def browse(self, mods, params=None):
        organizer = FakeOrganizer(mods)
        return native_backups.browse_native_mod_backups(organizer, params or {})

    def test_native_flag_controls_inventory_and_contents_are_not_certified(self):
        result = self.browse([FakeMod('Example'), FakeMod('Example_backup2', backup=True),
                              FakeMod('SuffixOnly_backup3'), FakeMod('Unmappedbackup9', backup=True)])
        self.assertEqual([row['name'] for row in result['backups']], ['Example_backup2', 'Unmappedbackup9'])
        self.assertEqual(result['backups'][0]['association']['status'], 'inferred')
        self.assertEqual(result['backups'][0]['association']['target_mod_name'], 'Example')
        self.assertEqual(result['backups'][1]['association']['status'], 'unresolved')
        self.assertTrue(result['model_inventory_complete'])
        self.assertFalse(result['content_checked'])
        self.assertFalse(result['restore_supported'])
        for row in result['backups']:
            self.assertFalse(row['restore_ready'])
            self.assertFalse(row['content_verified'])
            self.assertEqual(row['historical_completeness'], 'unknown')

    def test_bound_version_display_string_uses_three_forced_segments(self):
        organizer = FakeOrganizer()
        native_backups.browse_native_mod_backups(organizer, {})
        self.assertEqual(organizer.version.calls, [3, 3])

    def test_unsupported_version_and_missing_api_refuse_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, '2.5.2 only'):
            native_backups.browse_native_mod_backups(FakeOrganizer(version='2.6.0'), {})
        organizer = FakeOrganizer()
        organizer.list.allMods = None
        with self.assertRaisesRegex(RuntimeError, 'unsupported.*allMods'):
            native_backups.browse_native_mod_backups(organizer, {})

    def test_missing_original_still_lists_backup_and_filter_is_unresolved(self):
        result = self.browse([FakeMod('Missing_backup', backup=True)], {'mod': 'Missing'})
        self.assertEqual(result['filter']['status'], 'unresolved')
        self.assertEqual(result['matched_backups'], 1)
        self.assertEqual(result['backups'][0]['association']['status'], 'missing_target')

    def test_filter_is_exact_not_a_substring_or_case_guess(self):
        mods = [FakeMod('Example'), FakeMod('Example_backup', backup=True),
                FakeMod('Example More'), FakeMod('Example More_backup', backup=True)]
        selected = self.browse(mods, {'mod': 'Example'})
        self.assertEqual(selected['filter']['status'], 'resolved')
        self.assertEqual(selected['matched_backups'], 1)
        self.assertEqual(selected['backups'][0]['name'], 'Example_backup')
        self.assertEqual(self.browse(mods, {'mod': 'example'})['matched_backups'], 0)
        self.assertEqual(self.browse(mods, {'mod': 'Exam'})['matched_backups'], 0)

    def test_case_folded_target_is_only_a_candidate_not_a_resolved_association(self):
        result = self.browse([FakeMod('example'), FakeMod('Example_backup', backup=True)], {'mod': 'example'})
        association = result['backups'][0]['association']
        self.assertEqual(association['status'], 'unresolved')
        self.assertIsNone(association['target_mod_name'])
        self.assertEqual(association['candidate_target_names'], ['example'])

    def test_name_collision_reports_ambiguity_without_choosing_a_target(self):
        result = self.browse([FakeMod('Example'), FakeMod('example'), FakeMod('Example_backup', backup=True)],
                             {'mod': 'Example'})
        association = result['backups'][0]['association']
        self.assertEqual(association['status'], 'ambiguous')
        self.assertIsNone(association['target_mod_name'])
        self.assertEqual(association['candidate_count'], 2)
        self.assertEqual(result['filter']['status'], 'unresolved')

    def test_exact_filter_does_not_lose_ambiguous_candidates_beyond_display_limit(self):
        variants = [''.join(parts) for parts in itertools.product(*[(c, c.lower()) for c in 'ABCDEF'])]
        result = self.browse([*(FakeMod(name) for name in variants), FakeMod('ABCDEF_backup', backup=True)],
                             {'mod': 'abcdef'})
        self.assertEqual(result['matched_backups'], 1)
        association = result['backups'][0]['association']
        self.assertTrue(association['candidates_truncated'])
        self.assertEqual(len(association['candidate_target_names']), 20)
        self.assertEqual(association['candidate_count'], 64)
        self.assertEqual(result['filter']['status'], 'unresolved')

    def test_localized_foreign_name_uses_native_internal_identity_without_path_access(self):
        foreign = FakeMod('DLC : Dawnguard localis\u00e9', foreign=True)
        foreign.absolutePath = mock.Mock(side_effect=AssertionError('Foreign path must not be inspected'))
        organizer = FakeOrganizer([FakeMod('Example'), FakeMod('Example_backup', backup=True)])
        organizer.list.mods['DLC: Dawnguard'] = foreign
        organizer.list.names.append('DLC: Dawnguard')
        result = native_backups.browse_native_mod_backups(organizer, {})
        self.assertEqual(result['total_backups'], 1)
        self.assertEqual(result['backups'][0]['association']['status'], 'inferred')
        foreign.absolutePath.assert_not_called()

    def test_native_internal_display_name_mismatch_refuses_full_inventory(self):
        organizer = FakeOrganizer([FakeMod('Example_backup', backup=True)])
        organizer.list.displayName = lambda name: 'Different display identity'
        with self.assertRaisesRegex(RuntimeError, 'changed identity'):
            native_backups.browse_native_mod_backups(organizer, {})

    def test_native_suffix_is_ascii_case_sensitive_and_allows_no_number_or_leading_zeroes(self):
        result = self.browse([FakeMod('Example'), FakeMod('Example_backup', backup=True),
                              FakeMod('Example_backup007', backup=True),
                              FakeMod('Example_BACKUP1', backup=True),
                              FakeMod('Example_backup\u0661', backup=True)])
        by_name = {row['name']: row['association'] for row in result['backups']}
        self.assertEqual(by_name['Example_backup']['inferred_mod_name'], 'Example')
        self.assertEqual(by_name['Example_backup007']['inferred_mod_name'], 'Example')
        self.assertEqual(by_name['Example_BACKUP1']['status'], 'unresolved')
        self.assertEqual(by_name['Example_backup\u0661']['status'], 'unresolved')

    def test_unsafe_and_nested_native_restore_names_are_listed_but_invalid(self):
        for name in ('_backup', '._backup', '.._backup', 'CON_backup', 'LPT1.txt_backup',
                     'Part_backup_backup2', 'Part_separator_backup', 'Part._backup',
                     'Part/Other_backup', 'Part:Other_backup'):
            with self.subTest(name=name):
                association = self.browse([FakeMod(name, backup=True)])['backups'][0]['association']
                self.assertEqual(association['status'], 'invalid_mapping')

    def test_special_or_outside_target_never_becomes_an_ordinary_association(self):
        for options in ({'foreign': True}, {'overwrite': True}, {'separator': True},
                        {'path': 'G:\\Elsewhere\\Example'}):
            with self.subTest(options=options):
                result = self.browse([FakeMod('Example', **options), FakeMod('Example_backup', backup=True)])
                self.assertEqual(result['backups'][0]['association']['status'], 'invalid_mapping')

    def test_backup_path_must_be_lexically_direct_but_does_not_claim_physical_verification(self):
        for path in ('G:\\Elsewhere\\Example_backup', 'G:\\Mods\\Nested\\Example_backup',
                     '\\\\?\\G:\\Mods\\Example_backup', 'G:\\Mods\\.\\Example_backup',
                     'G:\\Mods\\Nested\\..\\Example_backup'):
            with self.subTest(path=path):
                result = self.browse([FakeMod('Example'), FakeMod('Example_backup', backup=True, path=path)])
                self.assertEqual(result['backups'][0]['association']['status'], 'invalid_mapping')
        result = self.browse([FakeMod('Example'), FakeMod('Example_backup', backup=True,
                                                        path='g:/mods/Example_backup')])
        self.assertEqual(result['backups'][0]['association']['status'], 'inferred')
        self.assertIn('Lexical', result['path_verification'])

    def test_nonmanaged_filter_selection_is_not_reported_as_resolved(self):
        result = self.browse([FakeMod('Example', path='G:\\Outside\\Example'),
                              FakeMod('Example_backup', backup=True)], {'mod': 'Example'})
        self.assertEqual(result['filter']['status'], 'unresolved')
        self.assertEqual(result['backups'][0]['association']['status'], 'invalid_mapping')

    def test_stable_sorted_pagination_and_metadata_revision(self):
        mods = [FakeMod('Z_backup', backup=True), FakeMod('a_backup2', backup=True),
                FakeMod('a_backup', backup=True)]
        first = self.browse(mods, {'limit': 2})
        self.assertEqual([row['name'] for row in first['backups']], ['a_backup', 'a_backup2'])
        self.assertEqual(first['next_offset'], 2)
        last = self.browse(list(reversed(mods)), {'limit': 2, 'offset': 2,
                                                'expected_inventory_revision': first['inventory_revision']})
        self.assertEqual([row['name'] for row in last['backups']], ['Z_backup'])
        self.assertIsNone(last['next_offset'])
        self.assertEqual(first['inventory_revision'], last['inventory_revision'])
        self.assertEqual(self.browse(mods, {'offset': 20})['backups'], [])

    def test_changed_page_revision_refuses_instead_of_combining_views(self):
        first = self.browse([FakeMod('A_backup', backup=True)])
        with self.assertRaisesRegex(ValueError, 'changed between pages'):
            self.browse([FakeMod('B_backup', backup=True)],
                        {'expected_inventory_revision': first['inventory_revision']})

    def test_inventory_model_must_remain_stable_across_both_reads(self):
        organizer = FakeOrganizer([FakeMod('Example_backup', backup=True)])
        original = organizer.list.allMods
        def changing():
            result = original()
            if len([call for call in organizer.list.calls if call[0] == 'allMods']) == 2:
                organizer.list.mods['Example_backup'].values['absolutePath'] = 'G:\\Changed\\Example_backup'
            return result
        organizer.list.allMods = changing
        with self.assertRaisesRegex(RuntimeError, 'changed during inspection'):
            native_backups.browse_native_mod_backups(organizer, {})

    def test_missing_mod_duplicate_or_invalid_flag_refuses_full_inventory(self):
        for fault in ('missing', 'duplicate', 'flag'):
            with self.subTest(fault=fault):
                organizer = FakeOrganizer([FakeMod('A_backup', backup=True)])
                if fault == 'missing':
                    organizer.list.mods.clear()
                elif fault == 'duplicate':
                    organizer.list.names *= 2
                else:
                    organizer.list.mods['A_backup'].values['isBackup'] = 1
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    native_backups.browse_native_mod_backups(organizer, {})

    def test_native_getter_error_is_not_silently_treated_as_no_backups(self):
        organizer = FakeOrganizer([FakeMod('A_backup', backup=True)])
        organizer.list.getMod = mock.Mock(side_effect=OSError('fixture failure'))
        with self.assertRaisesRegex(RuntimeError, 'incomplete.*getMod failed'):
            native_backups.browse_native_mod_backups(organizer, {})

    def test_parameters_are_bounded_and_rejected_before_reading_native_api(self):
        invalid = [None, [], {'extra': True}, {'limit': True}, {'limit': 0}, {'limit': 201},
                   {'offset': True}, {'offset': -1}, {'offset': 10001}, {'offset': 1.5},
                   {'mod': None}, {'mod': ''}, {'mod': 'A\nB'}, {'mod': '\ud800'},
                   {'expected_inventory_revision': None}, {'expected_inventory_revision': 'bad'}]
        for params in invalid:
            with self.subTest(params=params):
                organizer = FakeOrganizer()
                with self.assertRaises(ValueError):
                    native_backups.browse_native_mod_backups(organizer, params)
                self.assertEqual(organizer.calls, [])

    def test_oversized_or_unbounded_native_results_are_explicit_failures(self):
        for names in ((str(i) for i in range(5)), ['A'] * 10001):
            with self.subTest(type=type(names).__name__):
                organizer = FakeOrganizer()
                organizer.list.allMods = lambda: names
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    native_backups.browse_native_mod_backups(organizer, {})

    def test_metadata_browse_has_no_filesystem_or_mutator_access(self):
        mods = [FakeMod('Example'), FakeMod('Example_backup', backup=True)]
        before = [copy.deepcopy(mod.values) for mod in mods]
        organizer = FakeOrganizer(mods)
        forbidden = mock.Mock(side_effect=AssertionError('Filesystem access is forbidden'))
        with mock.patch('builtins.open', forbidden), mock.patch('os.scandir', forbidden), \
                mock.patch('pathlib.Path.resolve', forbidden), mock.patch('pathlib.Path.stat', forbidden), \
                mock.patch('pathlib.Path.read_bytes', forbidden):
            result = native_backups.browse_native_mod_backups(organizer, {})
        self.assertEqual(result['total_backups'], 1)
        self.assertEqual(before, [mod.values for mod in mods])
        self.assertEqual(set(organizer.calls), {'appVersion', 'modsPath', 'modList'})
        self.assertEqual({call[0] for call in organizer.list.calls}, {'allMods', 'getMod', 'displayName'})
        for mod in mods:
            self.assertEqual(set(mod.calls), {'name', 'absolutePath', 'isBackup', 'isForeign', 'isOverwrite', 'isSeparator'})


if __name__ == '__main__':
    unittest.main()
