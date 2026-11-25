"""Recovery tests use fake requests and extracted plugin methods, never live MO2."""
import ast
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client import BridgeClient, BridgeHTTPError, BridgeOperationError, BridgePendingError
import mcp_server


class ClientRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = BridgeClient(str(ROOT / 'fake-offline-mo2'))

    def test_lost_post_returns_original_generated_id_without_resubmitting(self):
        requests = []
        def request(endpoint, body=None):
            requests.append((endpoint, body))
            raise TimeoutError('receipt lost after server acceptance')
        self.client.request = request
        with self.assertRaises(BridgePendingError) as raised:
            self.client.call('set_mod_active', {'mod': 'Example', 'active': False})
        self.assertEqual(len(requests), 1)
        job_id = requests[0][1]['request_id']
        self.assertEqual(raised.exception.job_id, job_id)
        self.assertIn(job_id, str(raised.exception))
        self.assertEqual(raised.exception.failure['phase'], 'submit')
        self.assertTrue(raised.exception.failure['changes_may_have_occurred'])
        self.assertTrue(raised.exception.failure['pending'])

    def test_caller_supplied_id_is_preserved_on_ambiguous_post(self):
        self.client.request = Mock(side_effect=RuntimeError('connection lost'))
        with self.assertRaises(BridgePendingError) as raised:
            self.client.submit('hide_file', {}, request_id='stable-operation')
        self.assertEqual(raised.exception.job_id, 'stable-operation')
        self.assertEqual(self.client.request.call_count, 1)

    def test_pre_enqueue_auth_rejection_is_not_reported_as_changed(self):
        self.client.request = Mock(side_effect=BridgeHTTPError('unauthorized local request', 403))
        with self.assertRaises(BridgeOperationError) as raised:
            self.client.submit('hide_file', {}, request_id='rejected-operation')
        self.assertNotIsInstance(raised.exception, BridgePendingError)
        self.assertFalse(raised.exception.failure['pending'])
        self.assertFalse(raised.exception.failure['changes_may_have_occurred'])
        self.assertEqual(raised.exception.failure['http_status'], 403)

    def test_http_failures_after_enqueue_remain_uncertain(self):
        for status in (400, 500):
            with self.subTest(status=status):
                self.client.request = Mock(side_effect=BridgeHTTPError('failed after enqueue', status))
                with self.assertRaises(BridgePendingError) as raised:
                    self.client.submit('installer_choose', {}, request_id='accepted-operation')
                self.assertEqual(raised.exception.job_id, 'accepted-operation')
                self.assertTrue(raised.exception.failure['pending'])
                self.assertTrue(raised.exception.failure['changes_may_have_occurred'])
                self.assertEqual(raised.exception.failure['http_status'], status)
                self.assertEqual(self.client.request.call_count, 1)

    def test_poll_failure_keeps_primary_id_without_resubmitting(self):
        self.client.submit = Mock(return_value={'job_id': 'original-job'})
        self.client.job = Mock(side_effect=RuntimeError('poll connection lost'))
        with self.assertRaises(BridgePendingError) as raised:
            self.client.call('write_config', {}, request_id='original-job')
        self.assertEqual(raised.exception.job_id, 'original-job')
        self.assertEqual(raised.exception.failure['phase'], 'poll')
        self.assertEqual(self.client.submit.call_count, 1)

    def test_direct_job_query_failure_keeps_queried_id(self):
        self.client.request = Mock(side_effect=BridgeHTTPError('Unknown job ID', 404))
        with self.assertRaises(BridgePendingError) as raised:
            self.client.job('known-job')
        self.assertEqual(raised.exception.job_id, 'known-job')
        self.assertEqual(raised.exception.failure['http_status'], 404)

    def test_installer_helper_failure_keeps_main_and_helper_ids(self):
        self.client.submit = Mock(return_value={'job_id': 'install-job'})
        self.client.job = Mock(return_value={'job_id': 'install-job', 'status': 'running'})
        helper = BridgeOperationError({'message': 'dialog changed', 'error_type': 'ValueError',
                                      'job_id': 'helper-job', 'changes_may_have_occurred': False})
        self.client.advance_quick_install = Mock(side_effect=helper)
        with self.assertRaises(BridgePendingError) as raised:
            self.client.call('install_archive', {'name': 'Example'}, request_id='install-job')
        failure = raised.exception.failure
        self.assertEqual(failure['job_id'], 'install-job')
        self.assertEqual(failure['related_job_id'], 'helper-job')
        self.assertEqual(failure['phase'], 'installer_helper')
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertEqual(self.client.submit.call_count, 1)

    def test_failed_job_preserves_recovery_metadata(self):
        self.client.submit = Mock(return_value={'job_id': 'failed-job'})
        self.client.job = Mock(return_value={'job_id': 'failed-job', 'status': 'failed',
            'error': 'RuntimeError: saving failed', 'failure': {
                'message': 'saving failed', 'error_type': 'RuntimeError',
                'backup_id': 'backup-123', 'changes_may_have_occurred': True,
                'phase': 'operation'}})
        with self.assertRaises(BridgeOperationError) as raised:
            self.client.call('write_config', {}, request_id='failed-job')
        failure = raised.exception.failure
        self.assertEqual(failure['job_id'], 'failed-job')
        self.assertEqual(failure['backup_id'], 'backup-123')
        self.assertIn('backup-123', str(raised.exception))
        self.assertFalse(failure['pending'])
        self.assertTrue(failure['changes_may_have_occurred'])

    def test_legacy_failed_job_remains_runtime_error_with_job_id(self):
        self.client.submit = Mock(return_value={'job_id': 'legacy-job'})
        self.client.job = Mock(return_value={'job_id': 'legacy-job', 'status': 'failed', 'error': 'legacy failure'})
        with self.assertRaisesRegex(RuntimeError, 'legacy failure') as raised:
            self.client.call('write_config', {}, request_id='legacy-job')
        self.assertEqual(raised.exception.job_id, 'legacy-job')
        self.assertTrue(raised.exception.failure['changes_may_have_occurred'])

    def test_normal_deadline_pending_keeps_single_submission(self):
        self.client.submit = Mock(return_value={'job_id': 'pending-job'})
        result = self.client.call('write_config', {}, timeout=0, request_id='pending-job')
        self.assertTrue(result['pending'])
        self.assertEqual(result['job_id'], 'pending-job')
        self.assertEqual(self.client.submit.call_count, 1)

    def test_bad_receipt_or_job_payload_keeps_recovery_id(self):
        self.client.submit = Mock(return_value={'unexpected': True})
        with self.assertRaises(BridgePendingError) as raised:
            self.client.call('write_config', {}, request_id='bad-receipt')
        self.assertEqual(raised.exception.job_id, 'bad-receipt')
        self.client.submit = Mock(return_value={'job_id': 'bad-job'})
        for payload in [None, {'job_id': 'bad-job', 'status': 'done'},
                        {'job_id': 'other-job', 'status': 'done', 'result': {}},
                        {'job_id': 'bad-job', 'status': 'unexpected'}]:
            with self.subTest(payload=payload):
                self.client.job = Mock(return_value=payload)
                with self.assertRaises(BridgePendingError) as raised:
                    self.client.call('write_config', {}, request_id='bad-job')
                self.assertEqual(raised.exception.job_id, 'bad-job')

    def test_success_is_unchanged(self):
        self.client.submit = Mock(return_value={'job_id': 'successful-job'})
        self.client.job = Mock(return_value={'job_id': 'successful-job', 'status': 'done', 'result': {'saved': True}})
        self.assertEqual(self.client.call('write_config', {}, request_id='successful-job'), {'saved': True})

    def test_read_failure_does_not_claim_it_changed_mods(self):
        self.client.request = Mock(side_effect=TimeoutError('read connection lost'))
        with self.assertRaises(BridgePendingError) as raised:
            self.client.call('snapshot', {}, request_id='read-job')
        self.assertFalse(raised.exception.failure['changes_may_have_occurred'])


class MCPRecoveryTests(unittest.TestCase):
    def test_real_stdio_serializes_failure_and_continues(self):
        worker = """import sys
sys.path.insert(0, sys.argv[1])
import mcp_server
from client import BridgeOperationError
class FakeClient:
    def __init__(self, *args): pass
    def call(self, *args, **kwargs):
        raise BridgeOperationError({'message':'partial failure','error_type':'OSError',
            'job_id':'stdio-job','backup_id':'stdio-backup','changes_may_have_occurred':True,
            'phase':'operation','pending':False})
mcp_server.BridgeClient=FakeClient
sys.argv=['mcp_server.py','--mo2','fake-offline']
mcp_server.main()
"""
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'mo2_change',
             'arguments': {'action': 'write_config', 'params': {'expected_profile': 'Test', 'expected_revision': 'revision'}}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}]
        result = subprocess.run([sys.executable, '-B', '-c', worker, str(ROOT)], executable=sys.executable,
            input='\n'.join(json.dumps(x) for x in messages)+'\n',capture_output=True,text=True,timeout=10,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(replies[0]['result']['isError'])
        failure = json.loads(replies[0]['result']['content'][0]['text'])
        self.assertEqual(failure['job_id'], 'stdio-job')
        self.assertEqual(failure['backup_id'], 'stdio-backup')
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertEqual(replies[1]['result'], {})

    def test_existing_validation_error_remains_readable(self):
        reply = mcp_server.dispatch({'method':'tools/call','params':{'name':'mo2_change',
            'arguments':{'action':'write_config','params':{}}}}, object())
        self.assertTrue(reply['isError'])
        self.assertIn('snapshot', reply['content'][0]['text'])


class PluginRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT/'mo2_codex_bridge/plugin.py').read_text(encoding='utf-8'))
        plugin = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == 'BridgePlugin')
        names = {'enqueue','get_job','tick','fail_job','audit_job'}
        methods = [x for x in plugin.body if isinstance(x, ast.FunctionDef) and x.name in names]
        extracted = ast.ClassDef(name='ExtractedPlugin', bases=[], keywords=[], body=methods, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[extracted], type_ignores=[]))
        cls.dialogs = types.SimpleNamespace(state=lambda owner:{'installer_open':False},
            choose=lambda owner,params:{'selection_applied':True},accept_quick_install=lambda owner:None)
        from mo2_codex_bridge.job_store import JobStore, public_job
        ns = dict(json=json,queue=queue,time=time,uuid=uuid,dialogs=cls.dialogs,JobStore=JobStore,public_job=public_job,
            WRITE_ACTIONS={'write_config','installer_choose'},
            QApplication=types.SimpleNamespace(activeModalWidget=lambda:None))
        exec(compile(module,str(ROOT/'mo2_codex_bridge/plugin.py'),'exec'),ns)
        cls.plugin_type=ns['ExtractedPlugin']

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.owner=self.plugin_type()
        self.owner.data=Path(self.temporary.name)
        self.owner.jobs={}
        self.owner.pending=queue.Queue(maxsize=64)
        self.owner.busy=False
        self.owner.active_job=None
        self.owner.name=lambda:'Test bridge'
        self.owner.isActive=lambda:True
        self.owner.organizer=types.SimpleNamespace(pluginSetting=lambda *args:True)
        self.owner.ops=types.SimpleNamespace(dispatch=lambda *args:{'saved':True})

    def tearDown(self):
        self.temporary.cleanup()

    def test_partial_dispatch_failure_reaches_job_and_audit(self):
        error=OSError('partial config failure')
        error.bridge_failure={'message':str(error),'error_type':'OSError','backup_id':'original-backup',
                              'changes_may_have_occurred':True,'phase':'operation'}
        def dispatch(*args): raise error
        self.owner.ops.dispatch=dispatch
        self.owner.enqueue({'action':'write_config','params':{},'request_id':'failed-operation'})
        self.owner.tick()
        job=self.owner.get_job('failed-operation')
        self.assertEqual(job['status'],'failed')
        self.assertIn('partial config failure',job['error'])
        self.assertEqual(job['failure']['job_id'],'failed-operation')
        self.assertEqual(job['failure']['backup_id'],'original-backup')
        self.assertTrue(job['failure']['changes_may_have_occurred'])
        audit=json.loads((self.owner.data/'audit.jsonl').read_text())
        self.assertEqual(audit['backup_id'],'original-backup')
        self.assertEqual(audit['failure'],job['failure'])
        self.assertFalse(self.owner.busy)
        self.assertIsNone(self.owner.active_job)

    def test_pre_dispatch_rejection_reports_no_changes(self):
        self.owner.organizer.pluginSetting=lambda *args:False
        self.owner.enqueue({'action':'write_config','request_id':'preflight-rejection'})
        self.owner.tick()
        failure=self.owner.get_job('preflight-rejection')['failure']
        self.assertFalse(failure['changes_may_have_occurred'])
        self.assertEqual(failure['phase'],'preflight')

    def test_queue_expiration_has_structured_no_change_outcome(self):
        self.owner.enqueue({'action':'write_config','request_id':'expired-operation'})
        self.owner.jobs['expired-operation']['created']-=61
        self.owner.tick()
        job=self.owner.get_job('expired-operation')
        self.assertEqual(job['status'],'failed')
        self.assertFalse(job['failure']['changes_may_have_occurred'])
        self.assertEqual(job['failure']['phase'],'queued')
        audit=json.loads((self.owner.data/'audit.jsonl').read_text())
        self.assertEqual(audit['failure']['job_id'],'expired-operation')

    def test_installer_control_failure_preserves_job_and_audit(self):
        def choose(*args): raise ValueError('installer changed')
        with patch.object(self.dialogs,'choose',choose):
            self.owner.enqueue({'action':'installer_choose','request_id':'installer-choice'})
        job=self.owner.get_job('installer-choice')
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['failure']['job_id'],'installer-choice')
        self.assertEqual(job['failure']['phase'],'installer_control')
        audit=json.loads((self.owner.data/'audit.jsonl').read_text())
        self.assertEqual(audit['failure'],job['failure'])


    def test_post_selection_audit_failure_preserves_success_and_warning(self):
        original_open = Path.open
        def fail_audit(path, *args, **kwargs):
            if path.name == 'audit.jsonl':
                raise PermissionError('simulated audit write denial')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_audit):
            receipt = self.owner.enqueue({'action':'installer_choose','request_id':'selected-before-audit-failed'})
        self.assertEqual(receipt['status'], 'done')
        job = self.owner.get_job(receipt['job_id'])
        self.assertEqual(job['status'], 'done')
        self.assertTrue(job['result']['selection_applied'])
        self.assertFalse(job['audit_recorded'])
        self.assertFalse(job['result']['audit_recorded'])
        self.assertIn('audit write denial', job['result']['audit_error'])
        client = BridgeClient(str(ROOT/'fake-offline-mo2'))
        client.submit = Mock(return_value=receipt)
        client.job = Mock(return_value=job)
        result = client.call('installer_choose', {}, request_id=receipt['job_id'])
        self.assertTrue(result['selection_applied'])
        self.assertFalse(result['audit_recorded'])
        self.assertEqual(client.submit.call_count, 1)

    def test_operation_and_audit_failure_keep_original_recovery_metadata(self):
        error=OSError('original partial failure')
        error.bridge_failure={'message':str(error),'error_type':'OSError','backup_id':'recovery-before-audit',
                              'changes_may_have_occurred':True,'phase':'operation'}
        def dispatch(*args): raise error
        self.owner.ops.dispatch=dispatch
        self.owner.enqueue({'action':'write_config','request_id':'partial-before-audit-failed'})
        original_open = Path.open
        def fail_audit(path, *args, **kwargs):
            if path.name == 'audit.jsonl':
                raise OSError('simulated full audit disk')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_audit):
            self.owner.tick()
        job=self.owner.get_job('partial-before-audit-failed')
        self.assertEqual(job['status'],'failed')
        self.assertIn('original partial failure',job['error'])
        self.assertEqual(job['failure']['backup_id'],'recovery-before-audit')
        self.assertFalse(job['failure']['audit_recorded'])
        self.assertIn('full audit disk',job['failure']['audit_error'])
        self.assertTrue(job['failure']['changes_may_have_occurred'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
