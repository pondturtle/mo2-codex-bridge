"""Model invalid native mod indexes between removeMod and organizer refresh."""
from pathlib import Path
from unittest import mock

import test_operation_fixes as base


class RemovalCacheTransitionTests(base.OperationFixture):
    def setUp(self):
        super().setUp()
        self.folder,self.original_lists=self.make_disposable()
        self.cache_invalid=False
        self.invalid_snapshot_attempts=0
        self.refresh_calls=0
        self.after_remove=None
        self.after_refresh=None
        self.fail_refresh=False
        self.keep_invalid_after_refresh=False
        snapshot=self.ops.snapshot
        def native_snapshot():
            if self.cache_invalid:
                self.invalid_snapshot_attempts+=1
                raise RuntimeError('controlled invalid mod index 284 before native refresh')
            return snapshot()
        self.ops.snapshot=native_snapshot
        remove=self.organizer.ml.removeMod
        def native_remove(mod):
            result=remove(mod)
            self.cache_invalid=True
            if self.after_remove: self.after_remove()
            return result
        self.organizer.ml.removeMod=native_remove
        refresh=self.ops.refresh
        def native_refresh():
            self.refresh_calls+=1
            if self.fail_refresh: raise OSError('controlled native refresh failure')
            refresh()
            if not self.keep_invalid_after_refresh: self.cache_invalid=False
            if self.after_refresh: self.after_refresh()
        self.ops.refresh=native_refresh

    def remove(self):
        return self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)

    def test_native_invalid_cache_is_refreshed_before_first_full_snapshot(self):
        result=self.remove()
        self.assertTrue(result['removed'])
        self.assertEqual(self.refresh_calls,1)
        self.assertEqual(self.invalid_snapshot_attempts,0)
        self.assertFalse(self.folder.exists())
        self.assertTrue(result['removal_backup_verified'])
        for name,content in self.original_lists.items():
            self.assertEqual((self.root/'profile'/name).read_bytes(),content)

    def test_refresh_failure_preserves_verified_folder_and_original_error(self):
        self.fail_refresh=True
        with self.assertRaisesRegex(OSError,'controlled native refresh failure') as caught: self.remove()
        failure=caught.exception.bridge_failure
        self.assertEqual(failure['recovery_details']['stage'],'refresh-after-native-remove')
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertTrue(failure['recovery_details']['removal_backup_verified'])
        self.assertIn('controlled invalid mod index',failure['observation_error'])
        copy=Path(failure['recovery_details']['mod_files_backup'])
        self.assertEqual((copy/'options.ini').read_bytes(),b'irreplaceable disposable bytes')
        self.assertFalse(self.folder.exists())
        self.assertFalse(failure['recovery_details']['automatic_rollback_attempted'])

    def test_profile_identity_change_after_remove_blocks_refresh_without_enumeration(self):
        self.after_remove=lambda:setattr(self.organizer,'profileName',lambda:'Synthetic other profile')
        with self.assertRaisesRegex(RuntimeError,'Profile or instance changed') as caught: self.remove()
        self.assertEqual(self.refresh_calls,0)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertTrue(caught.exception.bridge_failure['recovery_details']['removal_backup_verified'])

    def test_root_identity_change_after_remove_blocks_refresh_without_enumeration(self):
        self.after_remove=lambda:setattr(self.organizer,'modsPath',lambda:str(self.root/'SyntheticOtherMods'))
        with self.assertRaisesRegex(RuntimeError,'Profile or instance changed'): self.remove()
        self.assertEqual(self.refresh_calls,0)
        self.assertFalse((self.root/'SyntheticOtherMods').exists())

    def test_game_start_after_native_remove_blocks_refresh(self):
        self.after_remove=lambda:self.ops.running.add('controlled fake game')
        with self.assertRaisesRegex(RuntimeError,'tool is running'): self.remove()
        self.assertEqual(self.refresh_calls,0)
        self.assertFalse(self.folder.exists())

    def test_profile_change_during_refresh_blocks_plugin_save(self):
        self.after_refresh=lambda:setattr(self.organizer,'profileName',lambda:'Synthetic other profile')
        with mock.patch.object(self.organizer.pl,'setPriority',wraps=self.organizer.pl.setPriority) as setter:
            with self.assertRaisesRegex(RuntimeError,'Profile or instance changed'): self.remove()
            setter.assert_not_called()
        self.assertEqual(self.refresh_calls,1)

    def test_survivor_state_edit_during_refresh_is_detected_not_overwritten(self):
        self.after_refresh=lambda:self.organizer.pl.active.discard('Child.esp')
        with mock.patch.object(self.organizer.pl,'setPriority',wraps=self.organizer.pl.setPriority) as setter:
            with self.assertRaisesRegex(RuntimeError,'Surviving plugin identity'): self.remove()
            setter.assert_not_called()
        self.assertNotIn('Child.esp',self.organizer.pl.active)

    def test_unhealed_native_cache_is_not_reported_as_success(self):
        self.keep_invalid_after_refresh=True
        with mock.patch.object(self.organizer.pl,'setPriority',wraps=self.organizer.pl.setPriority) as setter:
            with self.assertRaisesRegex(RuntimeError,'controlled invalid mod index') as caught: self.remove()
            setter.assert_not_called()
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertTrue(caught.exception.bridge_failure['recovery_details']['removal_backup_verified'])
