"""Native master-section refresh regression and insertion commit failure boundaries."""
import json
import types
import unittest
from pathlib import Path
from unittest import mock

import test_operation_fixes as base
from mo2_codex_bridge import managed_operations as managed
from mo2_codex_bridge.prospective_plugins import provider_identity, native_relationship_order
from mo2_codex_bridge.common import digest


class NativeInsertionTests(base.OperationFixture):
    def setUp(self):
        super().setUp()
        for name in ['Particle.esp','Tail1.esp','Tail2.esp','Tail3.esp','Tail4.esp']:
            self.registry['A'][name]={'masters':['Master.esm'],'master':False,'light':False}
        self.refresh()
        self.organizer.pl.active.discard('Tail2.esp')
        self.organizer.pl.refresh_indexes()
        self.organizer.pl.write_files()
        self.organizer.appVersion=lambda:types.SimpleNamespace(displayString=lambda:'2.5.2')
        self.organizer.pl.pluginNames=lambda:sorted(self.organizer.pl.order,key=str.casefold)
        self.existing_order=list(self.organizer.pl.order)
        self.existing_states=set(self.organizer.pl.active)
        self.new_specs={name:{'masters':['Master.esm'],'master':True,'light':False}
                        for name in ['NewA.esp','NewB.esp','NewC.esp']}
        original_refresh=self.refresh
        self.native_refresh_applied=False
        self.after_native_refresh=None
        def native_refresh():
            original_refresh()
            if 'NewA.esp' not in self.organizer.pl.order or self.native_refresh_applied:
                return
            self.native_refresh_applied=True
            pl=self.organizer.pl
            # Observable effect of MO2 2.5.2 fixPluginRelationships: an old
            # ordinary plugin formerly below masterCount is moved separately.
            pl.order=['Master.esm','NewB.esp','NewC.esp','NewA.esp',
                      'Particle.esp','Tail1.esp','Tail2.esp','Tail3.esp','Child.esp','Tail4.esp']
            pl.refresh_indexes()
            self.native_repaired_order=list(pl.order)
            if self.after_native_refresh:
                self.after_native_refresh()
        self.ops.refresh=native_refresh

    def test_three_new_master_plugins_preserve_all_original_pairs_and_tail(self):
        result=self.install_change(active=True)
        pl=self.organizer.pl
        self.assertTrue(self.native_refresh_applied)
        self.assertNotEqual([n for n in self.native_repaired_order if n in self.existing_order],self.existing_order)
        self.assertEqual([n for n in pl.order if n in self.existing_order],self.existing_order)
        self.assertEqual(pl.order[-4:],['Tail1.esp','Tail2.esp','Tail3.esp','Tail4.esp'])
        self.assertEqual(pl.active.intersection(self.existing_order),self.existing_states)
        self.assertEqual(pl.order[:4],['Master.esm','NewA.esp','NewB.esp','NewC.esp'])
        self.assertEqual(set(result['verified_files']),{'modlist.txt','plugins.txt','loadorder.txt'})
        self.assertEqual((self.root/'profile/loadorder.txt').read_text(encoding='utf-8').splitlines()[1:],pl.order)
        record=self.ops.data/'backups'/result['backup_id']/'activation-order-plan.json'
        self.assertEqual(json.loads(record.read_text(encoding='utf-8'))['order'],pl.order)

    def test_update_with_new_master_identities_uses_same_guarded_insertion(self):
        self.new_specs['Child.esp']=dict(self.registry['A']['Child.esp'])
        for name in self.registry['A']:
            self.new_specs.setdefault(name,dict(self.registry['A'][name]))
        result=self.change('update_archive',mod='A',archive=str(self.archive),name='Updated',activate_update=True)
        self.assertEqual([n for n in self.organizer.pl.order if n in self.existing_order],self.existing_order)
        self.assertEqual(self.organizer.pl.active.intersection(self.existing_order),self.existing_states)
        self.assertTrue(result['active'])
        self.assertFalse(result['previous_mod_active'])

    def test_unrelated_state_change_is_refused_before_order_setter(self):
        self.after_native_refresh=lambda:self.organizer.pl.active.discard('Child.esp')
        with mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter:
            with self.assertRaisesRegex(RuntimeError,'activation changed') as caught:
                self.install_change(active=True)
            setter.assert_not_called()
        self.assertNotIn('Child.esp',self.organizer.pl.active)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])

    def test_user_order_edit_during_native_refresh_is_not_overwritten(self):
        changed=[]
        def intervening():
            pl=self.organizer.pl
            first,second=pl.order.index('Tail1.esp'),pl.order.index('Tail2.esp')
            pl.order[first],pl.order[second]=pl.order[second],pl.order[first]
            pl.refresh_indexes()
            changed.extend(pl.order)
        self.after_native_refresh=intervening
        with mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter:
            with self.assertRaisesRegex(RuntimeError,'Unrecognized plugin order change during refresh'):
                self.install_change(active=True)
            setter.assert_not_called()
        self.assertEqual(self.organizer.pl.order,changed)

    def test_unreviewed_native_version_does_not_authorize_order_repair(self):
        self.organizer.appVersion=lambda:types.SimpleNamespace(displayString=lambda:'2.6.0')
        with mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter:
            with self.assertRaisesRegex(RuntimeError,'Unrecognized native plugin ordering version'):
                self.install_change(active=True)
            setter.assert_not_called()
        self.assertEqual(self.organizer.pl.order,self.native_repaired_order)

    def test_later_order_edit_during_plan_record_is_preserved(self):
        original=managed.write_json
        changed=[]
        def intervening(path,data):
            result=original(path,data)
            if Path(path).name=='activation-order-plan.json':
                self.organizer.pl.order.reverse()
                self.organizer.pl.refresh_indexes()
                changed.extend(self.organizer.pl.order)
            return result
        with (mock.patch.object(managed,'write_json',side_effect=intervening),
              mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter):
            with self.assertRaisesRegex(RuntimeError,'state changed before planned insertion'):
                self.install_change(active=True)
            setter.assert_not_called()
        self.assertEqual(self.organizer.pl.order,changed)

    def test_external_new_header_edit_before_setter_is_preserved(self):
        original=managed.write_json
        edited=[]
        def intervening(path,data):
            result=original(path,data)
            if Path(path).name=='activation-order-plan.json':
                target=self.root/'mods/New version/NewA.esp'
                value=base.plugin_bytes(masters=['Master.esm','Other.esm'],master=True)
                target.write_bytes(value)
                edited.append((target,value))
            return result
        with (mock.patch.object(managed,'write_json',side_effect=intervening),
              mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter):
            with self.assertRaisesRegex(RuntimeError,'headers changed before planned insertion'):
                self.install_change(active=True)
            setter.assert_not_called()
        self.assertEqual(edited[0][0].read_bytes(),edited[0][1])

    def test_running_tool_during_plan_record_blocks_setter(self):
        original=managed.write_json
        def intervening(path,data):
            result=original(path,data)
            if Path(path).name=='activation-order-plan.json': self.ops.running.add('fake tool')
            return result
        with (mock.patch.object(managed,'write_json',side_effect=intervening),
              mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter):
            with self.assertRaisesRegex(RuntimeError,'tool is running'):
                self.install_change(active=True)
            setter.assert_not_called()

    def test_setter_partial_failure_keeps_plan_backup_and_honest_stage(self):
        original=self.organizer.pl.setLoadOrder
        def fails_after(order):
            original(order)
            raise OSError('controlled native failure after order mutation')
        with mock.patch.object(self.organizer.pl,'setLoadOrder',side_effect=fails_after):
            with self.assertRaises(OSError) as caught: self.install_change(active=True)
        failure=caught.exception.bridge_failure
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertEqual(failure['recovery_details']['stage'],'commit-planned-plugin-insertion')
        self.assertFalse(failure['recovery_details']['automatic_rollback_attempted'])
        self.assertTrue((self.ops.data/'backups'/failure['backup_id']/'activation-order-plan.json').is_file())
        self.assertEqual([n for n in self.organizer.pl.order if n in self.existing_order],self.existing_order)

    def test_conflicting_new_master_header_refuses_before_activation(self):
        self.new_specs={'NewA.esp':{'masters':['Child.esp'],'master':True,'light':False}}
        with mock.patch.object(self.organizer.ml,'setActive',wraps=self.organizer.ml.setActive) as setter:
            with self.assertRaisesRegex(ValueError,'cannot preserve existing relative order'):
                self.install_change(active=True)
            setter.assert_not_called()

    def test_no_new_identities_do_not_authorize_correction_of_external_order(self):
        self.new_specs={name:dict(value) for name,value in self.registry['A'].items()}
        original=self.ops.refresh
        def drift():
            original()
            if 'Updated' in self.organizer.ml.active:
                self.organizer.pl.order.reverse()
                self.organizer.pl.refresh_indexes()
        self.ops.refresh=drift
        with mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter:
            with self.assertRaisesRegex(RuntimeError,'Plugin order differs'):
                self.change('update_archive',mod='A',archive=str(self.archive),name='Updated',activate_update=True)
            setter.assert_not_called()


class CachedProviderIdentityTests(base.OperationFixture):
    def rename_fixture_provider(self):
        canonical='Where\u2019s Sample'
        alias=canonical.encode('utf-8').decode('cp1252')
        ml=self.organizer.ml
        mod=ml.mods.pop('A')
        (self.root/'mods/A').rename(self.root/'mods'/canonical)
        mod._name=canonical
        ml.mods[canonical]=mod
        ml.order[ml.order.index('A')]=canonical
        ml.active.remove('A');ml.active.add(canonical)
        self.registry[canonical]=self.registry.pop('A')
        (self.root/'mods'/canonical/'Child.esp').write_bytes(base.plugin_bytes())
        self.refresh()
        real_origin=self.organizer.pl.origin
        self.organizer.pl.origin=lambda name:alias if name=='Child.esp' and not self.ops.is_active('New version') else real_origin(name)
        self.organizer.resolvePath=lambda name:str(self.root/'mods'/canonical/name)
        ml.write_files()
        return canonical,alias

    def stale_mod_snapshot(self,canonical,alias):
        snapshot=self.ops.snapshot
        def cached():
            result=snapshot()
            if not self.ops.is_active('New version'):
                for item in result['mods']:
                    if item['name']==canonical:
                        item['name']=alias
                        item['path']=str(self.root/'mods'/alias)
            result['revision']=digest(json.dumps({key:value for key,value in result.items() if key!='revision'},sort_keys=True).encode())
            return result
        self.ops.snapshot=cached

    def test_stale_mod_name_path_and_origin_have_preexisting_physical_proof(self):
        canonical,alias=self.rename_fixture_provider()
        self.stale_mod_snapshot(canonical,alias)
        self.assertFalse((self.root/'mods'/alias).exists())
        result=self.install_change(active=True)
        self.assertTrue(result['installed'])
        self.assertTrue((self.root/'mods'/canonical).is_dir())
        self.assertFalse((self.root/'mods'/alias).exists())
        record=self.ops.data/'backups'/result['backup_id']/'metadata-identity-proof.json'
        proof=json.loads(record.read_text(encoding='utf-8'))['mod_aliases']
        self.assertEqual(proof[0]['raw_name'],alias)
        self.assertEqual(proof[0]['canonical_name'],canonical)
        self.assertIn('+'+canonical,(self.root/'profile/modlist.txt').read_text(encoding='utf-8'))

    def test_stale_mod_name_path_without_saved_canonical_entry_is_refused(self):
        canonical,alias=self.rename_fixture_provider()
        self.stale_mod_snapshot(canonical,alias)
        path=self.root/'profile/modlist.txt'
        path.write_text(path.read_text(encoding='utf-8').replace(canonical,alias),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'unique saved profile entry'):
            self.install_change(active=True)
        self.assertEqual(self.install_calls,[])
        self.assertTrue((self.root/'mods'/canonical).is_dir())

    def test_existing_raw_folder_is_not_reinterpreted_as_stale_cache(self):
        canonical,alias=self.rename_fixture_provider()
        self.stale_mod_snapshot(canonical,alias)
        (self.root/'mods'/alias).mkdir()
        with self.assertRaisesRegex(RuntimeError,'Existing mod identity'):
            self.install_change(active=True)
        self.assertTrue((self.root/'mods'/alias).is_dir())
        self.assertTrue((self.root/'mods'/canonical).is_dir())

    def test_linked_raw_entry_is_never_accepted_as_absent_cached_folder(self):
        canonical,alias=self.rename_fixture_provider()
        self.stale_mod_snapshot(canonical,alias)
        raw=self.root/'mods'/alias
        original=Path.is_symlink
        def linked(path):
            return path == raw or original(path)
        with mock.patch.object(Path,'is_symlink',linked):
            with self.assertRaisesRegex(ValueError,'Linked raw provider entry'):
                self.install_change(active=True)
        self.assertEqual(self.install_calls,[])

    def test_exact_cp1252_utf8_cached_alias_repair_keeps_known_provider_path(self):
        canonical,alias=self.rename_fixture_provider()
        self.assertNotEqual(canonical,alias)
        result=self.install_change(active=True)
        self.assertTrue(result['installed'])
        self.assertEqual(self.organizer.pl.origin('Child.esp'),canonical)
        self.assertIn(canonical,self.organizer.ml.mods)
        self.assertNotIn(alias,self.organizer.ml.mods)

    def test_alias_with_wrong_current_vfs_path_fails_before_order_setter(self):
        self.rename_fixture_provider()
        self.organizer.resolvePath=lambda name:str(self.root/'mods/B'/name)
        with mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter:
            with self.assertRaisesRegex(ValueError,'differs from current VFS winner'):
                self.install_change(active=True)
            setter.assert_not_called()
        self.assertFalse(self.ops.is_active('New version'))

    def test_ambiguous_known_provider_paths_are_refused(self):
        canonical,alias=self.rename_fixture_provider()
        mods=[{'name':canonical,'path':str(self.root/'mods'/canonical)},
              {'name':canonical,'path':str(self.root/'mods/B')}]
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            provider_identity({'name':'Child.esp','origin':alias},mods,self.organizer.resolvePath)

    def test_arbitrary_provider_change_is_not_treated_as_encoding_repair(self):
        self.rename_fixture_provider()
        real_refresh=self.ops.refresh
        def drift():
            real_refresh()
            if self.ops.is_active('New version'):
                self.organizer.pl.origin=lambda name:'B' if name=='Child.esp' else self.available[name]['origin']
        self.ops.refresh=drift
        with mock.patch.object(self.organizer.pl,'setLoadOrder',wraps=self.organizer.pl.setLoadOrder) as setter:
            with self.assertRaisesRegex(RuntimeError,'provider/header differs'):
                self.install_change(active=True)
            setter.assert_not_called()


class NativeFingerprintTests(unittest.TestCase):
    def test_pinned_master_lookup_is_case_sensitive_but_names_remain_original(self):
        raw=[{'name':'Master.esm','masters':[],'master':True},
             {'name':'Child.esp','masters':['Master.esm'],'master':False},
             {'name':'New1.esp','masters':['new2.esp'],'master':False},
             {'name':'New2.esp','masters':['Master.esm'],'master':False}]
        expected={item['name'].casefold():{**item,'masters':[m.casefold() for m in item['masters']]} for item in raw}
        plan={'expected':expected,'existing_order':['master.esm','child.esp'],
              'introduced':['new1.esp','new2.esp']}
        iterator=[item['name'] for item in raw]
        self.assertEqual(native_relationship_order(plan,iterator,['Master.esm'],raw),
                         ['master.esm','child.esp','new1.esp','new2.esp'])
        raw[2]['masters']=['New2.esp']
        self.assertEqual(native_relationship_order(plan,iterator,['Master.esm'],raw),
                         ['master.esm','child.esp','new2.esp','new1.esp'])


    def test_cpp_out_of_range_after_master_constraint_is_not_python_clamped(self):
        raw=[{'name':'Master.esm','masters':[],'master':True}]
        raw += [{'name':'Old'+str(index)+'.esp','masters':['Master.esm'],'master':False} for index in range(5)]
        raw += [{'name':'Bad.esm','masters':['Last.esm'],'master':True},
                {'name':'Last.esm','masters':['Master.esm'],'master':True}]
        expected={item['name'].casefold():{**item,'masters':[m.casefold() for m in item['masters']]} for item in raw}
        plan={'expected':expected,'existing_order':[item['name'].casefold() for item in raw[:6]],
              'introduced':['bad.esm','last.esm']}
        iterator=['Bad.esm']+[item['name'] for item in raw if item['name']!='Bad.esm']
        with self.assertRaisesRegex(RuntimeError,'Unrecognized native relationship boundary'):
            native_relationship_order(plan,iterator,['Master.esm'],raw)
