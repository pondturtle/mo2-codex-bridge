"""Second-batch operation regressions; disposable fake folders only."""
import json
import struct
import unittest
from pathlib import Path
from unittest import mock

import test_bridge
from mo2_codex_bridge import managed_operations as managed
from mo2_codex_bridge.configuration_encoding import encode_configuration
from mo2_codex_bridge.common import digest


def plugin_bytes(masters=('Master.esm',), master=False, light=False):
    def sub(signature,data): return signature+struct.pack('<H',len(data))+data
    body=sub(b'HEDR',struct.pack('<fII',1.7,0,0x800))
    for name in masters:
        body+=sub(b'MAST',name.encode('utf-8')+b'\0')+sub(b'DATA',bytes(8))
    return struct.pack('<4sIIIIHH',b'TES4',len(body),(1 if master else 0)|(0x200 if light else 0),0,0,44,0)+body


class OperationFixture(unittest.TestCase):
    setUp_base=test_bridge.BridgeTests.setUp
    tearDown=test_bridge.BridgeTests.tearDown
    change=test_bridge.BridgeTests.change

    def setUp(self):
        self.setUp_base()
        self.registry={'A':{'Child.esp':{'masters':['Master.esm'],'master':False,'light':False}}}
        self.available={'Master.esm':{'origin':'Data','masters':[],'master':True,'light':False},
                        'Child.esp':{'origin':'A',**self.registry['A']['Child.esp']}}
        self.new_specs={}
        self.install_calls=[]
        self.install_hook=None
        self.archive=self.root/'downloads/fixture.zip'
        self.archive.write_bytes(b'archive bytes consumed only by fake native SDK')
        self.organizer.pl.origin=lambda name:self.available[name]['origin']
        self.organizer.pl.masters=lambda name:self.available[name]['masters']
        self.organizer.pl.isMasterFlagged=lambda name:self.available[name]['master']
        self.organizer.pl.isLightFlagged=lambda name:self.available[name]['light']
        self.organizer.getFileOrigins=lambda name:[self.available[name]['origin']] if name in self.available else ['A','B']
        self.ops.refresh=self.refresh
        self.organizer.installMod=self.install

    def refresh(self):
        pl,ml=self.organizer.pl,self.organizer.ml
        available={'Master.esm':{'origin':'Data','masters':[],'master':True,'light':False}}
        for mod in ml.order:
            if mod not in ml.active: continue
            for name,spec in self.registry.get(mod,{}).items():
                available[name]={'origin':mod,**spec}
        known=set(pl.order)
        order=[name for name in pl.order if name in available]
        order+=[name for name in available if name not in order]
        order=[name for name in order if available[name]['master']]+[name for name in order if not available[name]['master']]
        pl.active.intersection_update(available)
        pl.active.update(name for name in available if name not in known)
        pl.order=order
        self.available=available
        pl.refresh_indexes()
        # Deliberately don't save plugin files: this models the native removal bug.

    def install(self,archive,name):
        self.install_calls.append((archive,name))
        mod=self.organizer.createMod(name)
        self.registry[name]={key:dict(value) for key,value in self.new_specs.items()}
        for plugin,spec in self.new_specs.items():
            (Path(mod.absolutePath())/plugin).write_bytes(plugin_bytes(**spec))
        (Path(mod.absolutePath())/'options.ini').write_bytes(b'value=new\n')
        if self.install_hook: self.install_hook(name)
        return mod

    def install_change(self,**params):
        return self.change('install_archive',archive=str(self.archive),name='New version',**params)

    def make_disposable(self):
        original={name:(self.root/'profile'/name).read_bytes() for name in ['modlist.txt','plugins.txt','loadorder.txt']}
        mod=self.organizer.createMod('Disposable')
        self.organizer.ml.setActive('Disposable',True)
        self.registry['Disposable']={'Owned.esp':{'masters':['Master.esm'],'master':False,'light':False}}
        folder=Path(mod.absolutePath())
        (folder/'Owned.esp').write_bytes(plugin_bytes())
        (folder/'options.ini').write_bytes(b'irreplaceable disposable bytes')
        self.refresh()
        self.organizer.pl.write_files()
        return folder,original


class ConfigurationFormatTests(OperationFixture):
    def roundtrip(self,data,*,use_read_encoding=False):
        path=self.root/'mods/A/options.cfg'
        path.write_bytes(data)
        read=self.ops.read_config({'mod':'A','path':'options.cfg'},None)
        params={'mod':'A','path':'options.cfg','expected_sha256':read['sha256'],'text':read['text']}
        if use_read_encoding: params['encoding']=read['encoding']
        result=self.change('write_config',**params)
        self.assertEqual(path.read_bytes(),data)
        return read,result

    def test_five_native_formats_roundtrip_without_conversion(self):
        for name,data in [('utf8',b'value=caf\xc3\xa9\r\n'),('utf8bom',b'\xef\xbb\xbfvalue=caf\xc3\xa9\n'),
                          ('utf16le',b'\xff\xfe'+'value=cafe\u00e9\r\n'.encode('utf-16-le')),
                          ('utf16be',b'\xfe\xff'+'value=cafe\u00e9\r\n'.encode('utf-16-be')),
                          ('cp1252',b'value=caf\xe9\r\n')]:
            for explicit in [False,True]:
                with self.subTest(format=name,explicit=explicit):
                    read,result=self.roundtrip(data,use_read_encoding=explicit)
                    self.assertEqual(read['bom'],data.startswith((b'\xef\xbb\xbf',b'\xff\xfe',b'\xfe\xff')))
                    self.assertEqual(result['encoding'],read['encoding'])

    def test_default_preserves_existing_newlines(self):
        path=self.root/'mods/A/options.ini'
        path.write_bytes(b'value=old\r\nnext=yes\r\n')
        self.change('write_config',mod='A',path='options.ini',expected_sha256=digest(path.read_bytes()),text='value=new\nnext=yes\n')
        self.assertEqual(path.read_bytes(),b'value=new\r\nnext=yes\r\n')

    def test_explicit_conversion_and_legacy_signature_encodings(self):
        be=b'\xfe\xff'+'old\r\n'.encode('utf-16-be')
        data,fmt=encode_configuration(be,'new\n',{'encoding':'utf-16'})
        self.assertEqual(data,b'\xfe\xff'+'new\r\n'.encode('utf-16-be'))
        data,_=encode_configuration(None,'new\n',{'encoding':'utf-16'})
        self.assertEqual(data,b'\xff\xfe'+'new\n'.encode('utf-16-le'))
        data,_=encode_configuration(b'old','new',{'encoding':'utf-8-sig'})
        self.assertEqual(data,b'\xef\xbb\xbfnew')
        data,_=encode_configuration(b'\xef\xbb\xbfold\r\n','new\r\n',{'bom':False,'newline':'lf'})
        self.assertEqual(data,b'new\n')
        data,_=encode_configuration(None,'new',{'encoding':'utf-16-be'})
        self.assertEqual(data,'new'.encode('utf-16-be'))

    def test_new_utf8_json_is_valid_without_implicit_bom(self):
        result=self.change('write_config',mod='A',path='new.json',expected_sha256=None,text='{"word":"caf\u00e9"}')
        data=(self.root/'mods/A/new.json').read_bytes()
        self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(json.loads(data.decode('utf-8')),{'word':'caf\u00e9'})
        self.assertFalse(result['bom'])

    def test_invalid_format_or_lossy_text_never_replaces_target(self):
        path=self.root/'mods/A/options.ini'
        path.write_bytes(b'old=\xe9\n')
        for change in [{'encoding':'latin-1'},{'bom':'yes'},{'encoding':'cp1252','bom':True},
                       {'encoding':'utf-8-sig','bom':False},{'newline':'guess'},
                       {'text':'not cp1252: \u6f22'}]:
            with self.subTest(change=change):
                params=dict(mod='A',path='options.ini',expected_sha256=digest(path.read_bytes()),text='new')
                params.update(change)
                with self.assertRaises((ValueError,UnicodeError)):
                    self.change('write_config',**params)
                self.assertEqual(path.read_bytes(),b'old=\xe9\n')

    def test_unmarked_null_text_and_utf32_are_refused(self):
        for data in [b'a\0b\0',b'\xff\xfe\0\0a\0\0\0']:
            path=self.root/'mods/A/binary.cfg'
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                self.ops.read_config({'mod':'A','path':'binary.cfg'},None)
            self.assertEqual(path.read_bytes(),data)


class ArchivePreflightTests(OperationFixture):
    def test_invalid_options_refuse_before_native_install(self):
        invalid=[{'priority':-1},{'priority':999},{'priority':True},{'priority':1.0},
                 {'active':1},{'active':'false'},{'active':None},{'unknown':True}]
        for params in invalid:
            with self.subTest(params=params):
                with self.assertRaises(ValueError) as caught:
                    self.install_change(**params)
                self.assertFalse(caught.exception.bridge_failure['changes_may_have_occurred'])
                self.assertEqual(self.install_calls,[])
                self.assertEqual(self.organizer.ml.order,['A','B'])

    def test_backup_entries_do_not_inflate_prospective_priority_bounds(self):
        self.organizer.ml.allMods=lambda:['A','B','Old_backup','Overwrite']
        with self.assertRaises(ValueError): self.install_change(priority=3)
        with self.assertRaises(ValueError): self.change('create_separator',name='Invalid',priority=3)
        self.assertEqual(self.install_calls,[])
        self.assertIsNone(self.organizer.ml.getMod('Invalid_separator'))
        result=self.install_change(priority=2,active=False)
        self.assertEqual(result['priority'],2)

    def test_invalid_separator_name_or_priority_creates_nothing(self):
        for params in [{'name':'x'*145},{'name':'Valid','priority':999},{'name':'Valid','priority':False}]:
            with self.assertRaises(ValueError): self.change('create_separator',**params)
        self.assertEqual(self.organizer.ml.order,['A','B'])

    def test_case_insensitive_collision_and_relative_archive_refuse_before_install(self):
        with self.assertRaises(ValueError): self.change('install_archive',archive=str(self.archive),name='a')
        with self.assertRaises(ValueError): self.change('install_archive',archive='fixture.zip',name='Fresh')
        self.assertEqual(self.install_calls,[])

    def test_update_invalid_type_and_priority_conflict_refuse_before_install(self):
        for extra in [{'activate_update':1},{'activate_update':'false'},
                      {'activate_update':True,'priority':0},{'active':True}]:
            with self.assertRaises(ValueError):
                self.change('update_archive',mod='B',archive=str(self.archive),name='Updated',**extra)
        self.assertEqual(self.install_calls,[])
        self.assertTrue(self.ops.is_active('B'))

    def test_update_final_response_matches_observed_and_saved_state(self):
        result=self.change('update_archive',mod='B',archive=str(self.archive),name='Updated',activate_update=True)
        self.assertTrue(result['active'])
        self.assertEqual(result['priority'],1)
        self.assertFalse(result['previous_mod_active'])
        self.assertEqual(result['previous_mod_priority'],2)
        self.assertTrue(result['previous_mod_preserved'])
        self.assertEqual(set(result['verified_files']),{'modlist.txt','plugins.txt','loadorder.txt'})
        self.assertEqual(self.organizer.ml.order,['A','Updated','B'])
        self.assertIn('+Updated',(self.root/'profile/modlist.txt').read_text())
        self.assertIn('-B',(self.root/'profile/modlist.txt').read_text())

    def test_native_valid_but_unrelated_plugin_activation_drift_is_rejected(self):
        self.install_hook=lambda name:self.organizer.pl.active.discard('Child.esp')
        with self.assertRaisesRegex(RuntimeError,'plugin identity, state or relative order') as caught:
            self.install_change(active=False)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertFalse(self.ops.is_active('New version'))

    def test_third_party_autoactivation_is_partial_not_a_safe_success(self):
        self.install_hook=lambda name:self.organizer.ml.setActive(name,True)
        with self.assertRaisesRegex(RuntimeError,'unexpectedly enabled') as caught:
            self.install_change(active=True)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertIn('backup_id',caught.exception.bridge_failure)

    def test_install_exception_after_creation_retains_partial_recovery_id(self):
        def fail(name): raise OSError('fake SDK failure after creation')
        self.install_hook=fail
        with self.assertRaises(OSError) as caught: self.install_change(active=False)
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])
        folder=self.ops.data/'backups'/caught.exception.bridge_failure['backup_id']
        self.assertTrue((folder/'before.json').is_file())
        self.assertTrue((folder/'error.json').is_file())
        self.assertIsNotNone(self.organizer.ml.getMod('New version'))


class ProspectiveUpdateTests(OperationFixture):
    def prepare_old_optional(self):
        self.registry['A']['Optional.esp']={'masters':['Master.esm'],'master':False,'light':False}
        self.refresh()
        self.organizer.pl.active.discard('Optional.esp')
        self.organizer.pl.refresh_indexes()
        self.organizer.pl.write_files()
        self.new_specs={key:dict(value) for key,value in self.registry['A'].items()}

    def test_same_plugin_names_change_provider_and_preserve_disabled_optional(self):
        self.prepare_old_optional()
        result=self.change('update_archive',mod='A',archive=str(self.archive),name='Updated',activate_update=True)
        self.assertEqual(self.organizer.pl.origin('Child.esp'),'Updated')
        self.assertEqual(self.organizer.pl.origin('Optional.esp'),'Updated')
        self.assertIn('Child.esp',self.organizer.pl.active)
        self.assertNotIn('Optional.esp',self.organizer.pl.active)
        self.assertTrue(result['active'])
        self.assertEqual(result['priority'],0)
        self.assertFalse(result['previous_mod_active'])
        self.assertEqual(result['previous_mod_priority'],1)

    def test_new_missing_master_refuses_before_old_disable_or_priority_move(self):
        self.new_specs={'Child.esp':{'masters':['Missing.esm'],'master':False,'light':False}}
        with (mock.patch.object(self.organizer.ml,'setActive',wraps=self.organizer.ml.setActive) as active,
              mock.patch.object(self.organizer.ml,'setPriority',wraps=self.organizer.ml.setPriority) as moved):
            with self.assertRaisesRegex(ValueError,'Prospective activation') as caught:
                self.change('update_archive',mod='A',archive=str(self.archive),name='Updated',activate_update=True)
            active.assert_not_called()
            moved.assert_not_called()
        self.assertTrue(self.ops.is_active('A'))
        self.assertFalse(self.ops.is_active('Updated'))
        self.assertTrue(caught.exception.bridge_failure['changes_may_have_occurred'])

    def test_missing_old_plugin_identity_refuses_before_old_disable(self):
        self.prepare_old_optional()
        self.new_specs.pop('Optional.esp')
        with mock.patch.object(self.organizer.ml,'setActive',wraps=self.organizer.ml.setActive) as active:
            with self.assertRaisesRegex(ValueError,'omit an existing plugin'):
                self.change('update_archive',mod='A',archive=str(self.archive),name='Updated',activate_update=True)
            active.assert_not_called()
        self.assertTrue(self.ops.is_active('A'))

    def test_new_plugin_dependency_cycles_refuse_before_activation(self):
        for specs in [{'Self.esp':{'masters':['Self.esp'],'master':False,'light':False}},
                      {'One.esp':{'masters':['Two.esp'],'master':False,'light':False},
                       'Two.esp':{'masters':['One.esp'],'master':False,'light':False}}]:
            self.new_specs=specs
            name='Cycle'+str(len(self.install_calls))
            with mock.patch.object(self.organizer.ml,'setActive',wraps=self.organizer.ml.setActive) as active:
                with self.assertRaisesRegex(ValueError,'dependency cycle'):
                    self.change('install_archive',archive=str(self.archive),name=name,active=True)
                active.assert_not_called()

    def test_invalid_new_header_refuses_before_activation(self):
        self.new_specs={'Bad.esp':{'masters':['Master.esm'],'master':False,'light':False}}
        def corrupt(name): (self.root/'mods'/name/'Bad.esp').write_bytes(b'not a plugin')
        self.install_hook=corrupt
        with mock.patch.object(self.organizer.ml,'setActive',wraps=self.organizer.ml.setActive) as active:
            with self.assertRaisesRegex(ValueError,'no valid Skyrim TES4'):
                self.install_change(active=True)
            active.assert_not_called()


class RemovalPersistenceTests(OperationFixture):
    def test_removal_flushes_ghost_rows_and_preserves_exact_existing_lists(self):
        folder,baseline=self.make_disposable()
        result=self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
        self.assertFalse(folder.exists())
        self.assertTrue(result['persisted'] and result['modlist_persisted'] and result['removal_backup_verified'])
        self.assertEqual(result['removed_plugins'],['owned.esp'])
        backup=Path(result['mod_files_backup'])
        self.assertEqual((backup/'options.ini').read_bytes(),b'irreplaceable disposable bytes')
        for name,data in baseline.items(): self.assertEqual((self.root/'profile'/name).read_bytes(),data)
        self.assertEqual(self.organizer.pl.order,['Master.esm','Child.esp'])

    def test_active_dependent_refuses_before_folder_copy_or_native_remove(self):
        folder,_=self.make_disposable()
        self.registry['A']['Child.esp']['masters']=['Master.esm','Owned.esp']
        self.refresh()
        with mock.patch.object(self.organizer.ml,'removeMod',wraps=self.organizer.ml.removeMod) as removed:
            with self.assertRaisesRegex(ValueError,'missing or disabled master') as caught:
                self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
            removed.assert_not_called()
        self.assertTrue(folder.is_dir())
        self.assertFalse(caught.exception.bridge_failure['changes_may_have_occurred'])

    def test_alternate_provider_refuses_before_remove(self):
        folder,_=self.make_disposable()
        old=self.organizer.getFileOrigins
        self.organizer.getFileOrigins=lambda name:['Disposable','Other'] if name=='Owned.esp' else old(name)
        with self.assertRaisesRegex(ValueError,'Alternate plugin providers'):
            self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
        self.assertTrue(folder.exists())

    def test_corrupt_folder_copy_refuses_without_removing_original(self):
        folder,_=self.make_disposable()
        real=managed.shutil.copytree
        def corrupt(src,dst,*args,**kwargs):
            result=real(src,dst,*args,**kwargs)
            if Path(src)==folder: (Path(dst)/'options.ini').write_bytes(b'corrupt')
            return result
        with mock.patch.object(managed.shutil,'copytree',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'recovery bytes') as caught:
                self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
        self.assertTrue(folder.exists())
        self.assertFalse(caught.exception.bridge_failure['changes_may_have_occurred'])

    def test_tool_start_after_backup_blocks_native_remove(self):
        folder,_=self.make_disposable()
        real=managed.shutil.copytree
        def starts(src,dst,*args,**kwargs):
            result=real(src,dst,*args,**kwargs)
            if Path(src)==folder: self.ops.running.add('fake game')
            return result
        with mock.patch.object(managed.shutil,'copytree',side_effect=starts):
            with self.assertRaisesRegex(RuntimeError,'tool is running'):
                self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
        self.assertTrue(folder.exists())

    def test_native_failure_after_removal_keeps_verified_complete_copy(self):
        folder,_=self.make_disposable()
        real=self.organizer.ml.removeMod
        def remove_then_fail(mod):
            real(mod)
            raise OSError('fake failure after removal')
        with mock.patch.object(self.organizer.ml,'removeMod',side_effect=remove_then_fail):
            with self.assertRaises(OSError) as caught:
                self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
        self.assertFalse(folder.exists())
        details=caught.exception.bridge_failure['recovery_details']
        self.assertTrue(details['changes_may_have_occurred'] and details['removal_backup_verified'])
        self.assertEqual((Path(details['mod_files_backup'])/'options.ini').read_bytes(),b'irreplaceable disposable bytes')

    def test_save_signal_failure_reports_partial_removal_without_rollback(self):
        folder,_=self.make_disposable()
        with mock.patch.object(self.organizer.pl,'setPriority',side_effect=OSError('fake save failure')):
            with self.assertRaises(OSError) as caught:
                self.change('remove_mod',mod='Disposable',acknowledge_shared_folder=True)
        self.assertFalse(folder.exists())
        details=caught.exception.bridge_failure['recovery_details']
        self.assertTrue(details['changes_may_have_occurred'])
        self.assertFalse(details['automatic_rollback_attempted'])
        self.assertTrue(Path(details['mod_files_backup']).is_dir())


class DiagnosticsPaginationTests(OperationFixture):
    def test_casefold_diagnostics_and_true_missing_master(self):
        self.registry['A']['Child.esp']['masters']=['MASTER.ESM']
        self.assertEqual(self.ops.diagnostics({},None)['problems'],[])
        self.organizer.pl.active.discard('Master.esm')
        problems=self.ops.diagnostics({},None)['problems']
        self.assertEqual(len(problems),1)
        self.assertEqual(problems[0]['type'],'missing_or_disabled_master')

    def test_pagination_end_and_beyond_end_are_zero(self):
        folder=self.root/'mods/B'
        (folder/'one.txt').write_bytes(b'one')
        (folder/'two.txt').write_bytes(b'two')
        for offset in [2,999]:
            result=self.ops.mod_conflicts({'mod':'B','offset':offset,'max_files':1},None)
            self.assertEqual(result['scanned'],0)
            self.assertIsNone(result['next_offset'])
        result=self.ops.mod_conflicts({'mod':'B','offset':0,'max_files':1},None)
        self.assertEqual(result['scanned'],1)
        self.assertEqual(result['next_offset'],1)

    def test_invalid_cursors_are_rejected(self):
        for params in [{'offset':-1},{'offset':True},{'offset':1.5},{'max_files':False},{'max_files':0},{'max_files':2001}]:
            with self.assertRaises(ValueError):
                self.ops.mod_conflicts({'mod':'B',**params},None)


if __name__=='__main__': unittest.main(verbosity=2)
