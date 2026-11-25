"""Command-line and reusable client for an already-running MO2."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


MUTATING_ACTIONS = {'create_separator', 'set_mod_priority', 'set_mod_active',
                    'set_plugin_active', 'set_plugin_order', 'install_archive',
                    'update_archive', 'write_config', 'restore_config', 'hide_file',
                    'unhide_file', 'restore_order', 'remove_empty_separator',
                    'installer_choose', 'remove_mod', 'select_profile'}


class BridgeOperationError(RuntimeError):
    """A failed or uncertain operation with enough information to recover it."""
    def __init__(self, failure):
        self.failure = dict(failure)
        self.bridge_failure = self.failure
        self.job_id = self.failure.get('job_id')
        self.backup_id = self.failure.get('backup_id')
        message = self.failure['message']
        references = [f'{key}={self.failure[key]}' for key in ('job_id', 'backup_id') if self.failure.get(key)]
        if references:
            message += ' (' + ', '.join(references) + ')'
        if self.failure.get('pending'):
            message += '. Query this job ID; do not submit a duplicate operation.'
        super().__init__(message)


class BridgePendingError(BridgeOperationError):
    """The connection failed while an accepted operation may still be running."""


class BridgeHTTPError(RuntimeError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def failure_details(error):
    details = getattr(error, 'bridge_failure', None)
    result = dict(details) if isinstance(details, dict) else {}
    result.setdefault('message', str(error))
    result.setdefault('error_type', type(error).__name__)
    return result


class BridgeClient:
    def __init__(self, mo2_path=None):
        if not mo2_path:
            mo2_path = os.environ.get('MO2_PATH')
        if not mo2_path:
            config = Path.home() / '.mo2-codex-bridge' / 'config.json'
            if config.exists():
                mo2_path = json.loads(config.read_text(encoding='utf-8')).get('mo2_path')
        self.mo2 = Path(mo2_path).expanduser().resolve() if mo2_path else None

    def request(self, endpoint, body=None):
        if self.mo2 is None:
            raise RuntimeError('Configure the bridge with setup.py --mo2 <MO2-folder>, or supply --mo2 / MO2_PATH')
        session_path = self.mo2 / 'plugins/data/mo2_codex_bridge/session.json'
        if not session_path.exists():
            raise RuntimeError('MO2 bridge is offline. Open MO2 with the bridge plugin installed.')
        session = json.loads(session_path.read_text(encoding='utf-8'))
        if session.get('host') != '127.0.0.1' or type(session.get('port')) is not int or not 1 <= session['port'] <= 65535:
            raise RuntimeError('Invalid local bridge session')
        request = urllib.request.Request(f"http://127.0.0.1:{session['port']}{endpoint}",
                                         data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
                                         headers={'Authorization': 'Bearer ' + session['token'],
                                                  'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.load(error).get('error', str(error))
            except (ValueError, AttributeError):
                message = str(error)
            raise BridgeHTTPError(message, error.code) from None
        except urllib.error.URLError:
            raise RuntimeError('Cannot reach MO2 bridge. Check its connection before retrying.') from None
        except TimeoutError:
            raise TimeoutError('MO2 is not answering yet; an installer may still be open') from None

    def _pending_error(self, error, job_id, action, phase):
        details = failure_details(error)
        related_id = details.get('job_id')
        if related_id and related_id != job_id:
            details['related_job_id'] = related_id
        details.update(job_id=job_id, action=action, phase=phase, pending=True,
                       changes_may_have_occurred=action in MUTATING_ACTIONS)
        if isinstance(error, BridgeHTTPError):
            details['http_status'] = error.status
        return BridgePendingError(details)

    def submit(self, action, params=None, request_id=None):
        job_id = uuid.uuid4().hex if request_id is None else request_id
        try:
            return self.request('/rpc', {'action': action, 'params': params or {}, 'request_id': job_id})
        except BridgeHTTPError as error:
            if error.status != 403:
                # Other HTTP failures may occur after enqueue, or reference an existing ID.
                raise self._pending_error(error, job_id, action, 'submit') from None
            details = failure_details(error)
            details.update(job_id=job_id, action=action, phase='submit', pending=False,
                           changes_may_have_occurred=False, http_status=error.status)
            raise BridgeOperationError(details) from None
        except Exception as error:
            raise self._pending_error(error, job_id, action, 'submit') from None

    def job(self, job_id):
        if not isinstance(job_id, str) or not job_id.replace('-', '').isalnum():
            raise ValueError('Invalid job ID')
        try:
            return self.request('/jobs/' + job_id)
        except Exception as error:
            details = failure_details(error)
            details.update(job_id=job_id, phase='poll', pending=True)
            if isinstance(error, BridgeHTTPError):
                details['http_status'] = error.status
            # A job query does not reveal whether the original operation changed files.
            details.setdefault('changes_may_have_occurred', True)
            raise BridgePendingError(details) from None

    def _failed_job(self, job, action):
        details = job.get('failure')
        details = dict(details) if isinstance(details, dict) else {}
        details.setdefault('message', job.get('error', 'Bridge operation failed'))
        details.setdefault('error_type', 'BridgeJobError')
        details.setdefault('changes_may_have_occurred', action in MUTATING_ACTIONS)
        details.setdefault('phase', 'operation')
        details.update(job_id=job['job_id'], action=action, pending=False)
        return BridgeOperationError(details)

    def call(self, action, params=None, timeout=30, request_id=None):
        job_id = uuid.uuid4().hex if request_id is None else request_id
        receipt = self.submit(action, params, job_id)
        if not isinstance(receipt, dict) or receipt.get('job_id') != job_id:
            raise self._pending_error(ValueError('Invalid submission receipt'), job_id, action, 'submit')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                job = self.job(job_id)
                if not isinstance(job, dict) or job.get('job_id') != job_id:
                    raise ValueError('Invalid job response')
                if job.get('recovery_required'):
                    details = {key: job[key] for key in ('backup_id', 'recovery_required') if key in job}
                    details.update(message=job.get('message', 'Inspect the preserved mutation receipt and recovery backups; do not resubmit.'),
                                   error_type='RecoveryRequired', job_id=job_id, action=action,
                                   phase='receipt_recovery', pending=True, changes_may_have_occurred=action in MUTATING_ACTIONS)
                    raise BridgePendingError(details)
                status = job['status']
                if status not in {'queued', 'running', 'done', 'failed'}:
                    raise ValueError('Invalid job status')
                if status == 'done':
                    result = job['result']
            except Exception as error:
                raise self._pending_error(error, job_id, action, 'poll') from None
            if status == 'done':
                return result
            if status == 'failed':
                raise self._failed_job(job, action)
            if action in {'install_archive', 'update_archive'} and status == 'running':
                try:
                    self.advance_quick_install((params or {}).get('name'))
                except Exception as error:
                    raise self._pending_error(error, job_id, action, 'installer_helper') from None
            time.sleep(0.15)
        return {'pending': True, 'job_id': job_id,
                'message': 'Operation may still run. Query this job ID; do not submit a duplicate.'}

    def advance_quick_install(self, expected_name):
        receipt = self.submit('installer_state')
        job = self.job(receipt['job_id'])
        if job['status'] == 'failed':
            raise self._failed_job(job, 'installer_state')
        if job['status'] != 'done':
            return
        dialog = job['result']
        if not dialog.get('installer_open') or dialog.get('title') != 'Quick Install':
            return
        controls = dialog['controls']
        target = next((x for x in controls if x.get('name') == 'nameCombo'), None)
        button = next((x for x in controls if x.get('name') == 'okBtn' and x.get('enabled')), None)
        if target and button and target.get('value') == expected_name:
            selection = self.submit('installer_choose', {'dialog_id': dialog['dialog_id'],
                         'dialog_revision': dialog['dialog_revision'], 'control_id': button['id']})
            completed = self.job(selection['job_id'])
            if completed['status'] == 'failed':
                raise self._failed_job(completed, 'installer_choose')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mo2', default=None)
    parser.add_argument('action')
    parser.add_argument('--params-file')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--request-id')
    args = parser.parse_args()
    params = json.loads(Path(args.params_file).read_text(encoding='utf-8-sig')) if args.params_file else {}
    client = BridgeClient(args.mo2)
    try:
        result = client.job(params['job_id']) if args.action == 'job' else client.call(args.action, params, args.timeout, args.request_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as error:
        result = {'error': str(error)}
        if isinstance(getattr(error, 'bridge_failure', None), dict):
            result['failure'] = error.bridge_failure
        print(json.dumps(result, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
