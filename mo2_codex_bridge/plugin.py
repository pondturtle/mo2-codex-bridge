"""MO2 tool plugin: authenticated localhost jobs, dispatched on the Qt GUI thread."""
import json
import os
import queue
import secrets
import time
import uuid
from pathlib import Path

import mobase
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QLabel, QPushButton, QVBoxLayout

from .common import VERSION, write_json
from .operations import Operations, WRITE_ACTIONS
from .transport import LocalTransport
from .job_store import JobStore, public_job
from .running_apps import RunningApplications
from .nexus_downloads import NexusDownloads
from . import dialogs


class BridgePlugin(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self.jobs = {}
        self.pending = queue.Queue(maxsize=64)
        self.busy = False
        self.server = None
        self.dialog = None

    def name(self):
        return 'MO2 Codex Bridge'

    def localizedName(self):
        return self.name()

    def author(self):
        return 'Community fork based on the MIT-licensed bridge by shadowpendant69'

    def description(self):
        return 'Connects Codex to the running MO2 for mod management and troubleshooting.'

    def version(self):
        numbers = tuple(int(part) for part in VERSION.split('-', 1)[0].split('.'))
        return mobase.VersionInfo(*numbers, mobase.ReleaseType.FINAL)

    def isActive(self):
        value = self.organizer.pluginSetting(self.name(), 'enabled')
        return True if value is None else bool(value)

    def settings(self):
        return [mobase.PluginSetting('enabled', 'Enable the local Codex connection', True),
                mobase.PluginSetting('allow_changes', 'Allow Codex to change mods and configuration', True)]

    def init(self, organizer):
        self.organizer = organizer
        self.data = Path(__file__).resolve().parents[1] / 'data' / 'mo2_codex_bridge'
        self.data.mkdir(parents=True, exist_ok=True)
        self.ops = Operations(organizer, mobase, self.data, self.refresh_sync, self.select_profile, self.wait_until)
        self.jobs = JobStore(self.data)
        try:
            mo2_version = organizer.appVersion().displayString()
        except Exception:
            mo2_version = None
        self.ops.running = RunningApplications(on_skse_event=self.audit_skse_event, mo2_version=mo2_version)
        self.ops.running.managed_game = lambda: organizer.managedGame()
        self.ops.job_retention = self.jobs.describe
        try:
            about = organizer.onAboutToRun(self.before_run)
            finished = organizer.onFinishedRun(self.finished_run)
            if about is False or finished is False:
                self.ops.running.global_uncertainty = 'Required MO2 launch callback registration failed'
        except Exception as error:
            self.ops.running.global_uncertainty = 'MO2 launch callbacks unavailable: ' + str(error)
        organizer.onUserInterfaceInitialized(self.start)
        return True

    def displayName(self):
        return self.name()

    def tooltip(self):
        return 'Show the local connection and control whether Codex can make changes'

    def icon(self):
        return QIcon()

    def display(self):
        if self.dialog is None:
            self.dialog = QDialog(self._parentWidget())
            self.dialog.setWindowTitle(self.name())
            layout = QVBoxLayout(self.dialog)
            label = QLabel('Codex can inspect and manage this MO2 while it stays open.\n'
                           'Configuration changes and ordering changes are backed up.\n'
                           'Close games and other MO2 tools before making changes.')
            label.setWordWrap(True)
            layout.addWidget(label)
            allowed = QCheckBox('Allow changes to mods and configuration')
            allowed.setChecked(self.organizer.pluginSetting(self.name(), 'allow_changes') is not False)
            allowed.toggled.connect(lambda checked: self.organizer.setPluginSetting(self.name(), 'allow_changes', checked))
            layout.addWidget(allowed)
            self.connection_label = QLabel()
            layout.addWidget(self.connection_label)
            close = QPushButton('Close')
            close.clicked.connect(self.dialog.close)
            layout.addWidget(close)
        self.connection_label.setText('Connection ready' if self.server is not None else 'Connection unavailable')
        self.dialog.show()
        self.dialog.raise_()

    def before_run(self, executable, *args):
        # A nested native dialog/event loop must not launch a game in the
        # middle of a bridge mutation.
        if self.busy:
            return False
        self.ops.running.starting(str(executable))
        return True

    def finished_run(self, executable, *args):
        self.ops.running.finished(str(executable), args[0] if args else None)

    def select_profile(self, name):
        combo = self.main_window.findChild(QComboBox, 'profileBox')
        if combo is None or combo.findText(name) < 0:
            raise ValueError('MO2 profile selector does not contain this profile')
        combo.setCurrentIndex(combo.findText(name))

    def wait_until(self, predicate, timeout_ms):
        """Allow MO2's delayed writer to run while the bridge remains busy."""
        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        result, errors = [False], []

        def check():
            try:
                result[0] = bool(predicate())
            except Exception as error:
                errors.append(error)
            if result[0] or errors:
                loop.quit()

        poll.timeout.connect(check)
        try:
            poll.start(50)
            deadline.start(timeout_ms)
            loop.exec()
        finally:
            poll.stop()
            deadline.stop()
        if errors:
            raise errors[0]
        return result[0]

    def refresh_sync(self):
        """Wait for MO2's documented asynchronous refresh without blocking Qt events."""
        loop = QEventLoop()
        done = [False]
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        self.organizer.refresh(True)

        def completed():
            done[0] = True
            loop.quit()

        self.organizer.onNextRefresh(completed, True)
        if not done[0]:
            timeout.start(15000)
            loop.exec()
        timeout.stop()
        if not done[0]:
            raise RuntimeError('MO2 refresh timed out. Inspect live state before retrying.')

    def start(self, main_window):
        try:
            self.start_connection(main_window)
        except Exception as error:
            write_json(self.data / 'startup-error.json', {'error': f'{type(error).__name__}: {error}'})

    def start_connection(self, main_window):
        if self.server is not None or not self.isActive():
            return
        self.token = secrets.token_urlsafe(48)
        self.main_window = main_window
        self.ops.nexus = NexusDownloads(self.organizer, self.data, window=main_window)
        self.server = LocalTransport(self, main_window)
        write_json(self.data / 'session.json', {'version': VERSION, 'host': '127.0.0.1',
                                               'port': self.server.server_port, 'token': self.token,
                                               'mo2_path': str(Path(__file__).resolve().parents[2])})
        self.timer = QTimer(main_window)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        QApplication.instance().aboutToQuit.connect(self.stop)

    def get_job(self, job_id):
        if not isinstance(self.jobs, JobStore):
            self.jobs = JobStore(self.data, self.jobs)
        job = self.jobs.get(job_id)
        return public_job(job) if job else None

    def fail_job(self, job, error, *, phase, changes_may_have_occurred=False):
        details = getattr(error, 'bridge_failure', None)
        failure = dict(details) if isinstance(details, dict) else {}
        failure.setdefault('message', str(error))
        failure.setdefault('error_type', type(error).__name__)
        failure.setdefault('phase', phase)
        failure.setdefault('changes_may_have_occurred', changes_may_have_occurred)
        failure.update(job_id=job['job_id'], pending=False)
        job.update(status='failed', error=f'{type(error).__name__}: {error}', failure=failure)

    def audit_skse_event(self, event):
        # Use the existing diagnostic stream. These bounded records are
        # historical evidence only and are never read back into the guard.
        with (self.data / 'audit.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                     'job_id': None, 'action': 'skse_launch_tracking',
                                     'status': event['event'], 'backup_id': None,
                                     'error': None, 'failure': None,
                                     'process_id': os.getpid(), 'monotonic_ns': time.monotonic_ns(),
                                     'tracking': event}) + '\n')

    def audit_job(self, job):
        failure = job.get('failure', {})
        try:
            with (self.data / 'audit.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                         'job_id': job['job_id'], 'action': job['action'], 'status': job['status'],
                                         'error': job.get('error'),
                                         'backup_id': failure.get('backup_id') or job.get('result', {}).get('backup_id'),
                                         'failure': failure or None}) + '\n')
            job['audit_recorded'] = True
        except Exception as error:
            # Logging must not replace an already completed operation's outcome.
            warning = {'audit_recorded': False, 'audit_error': f'{type(error).__name__}: {error}'}
            job.update(warning)
            if failure:
                failure.update(warning)
            if job['status'] == 'done' and isinstance(job.get('result'), dict):
                job['result'] = dict(job['result'], **warning)
        finally:
            if isinstance(self.jobs, JobStore):
                self.jobs.complete(job)

    def enqueue(self, request):
        if not isinstance(request, dict):
            raise ValueError('Request must be a JSON object')
        action, params = request.get('action'), request.get('params', {})
        job_id = request.get('request_id', uuid.uuid4().hex)
        if (not isinstance(job_id, str) or not 1 <= len(job_id) <= 80
                or not job_id.replace('-', '').isascii() or not job_id.replace('-', '').isalnum()):
            raise ValueError('Invalid request ID')
        if not isinstance(action, str) or not isinstance(params, dict):
            raise ValueError('Invalid request structure')
        params = json.loads(json.dumps(params, ensure_ascii=False, allow_nan=False))
        if not isinstance(self.jobs, JobStore):
            self.jobs = JobStore(self.data, self.jobs)
        with self.jobs.lock:
            previous = self.jobs.lookup(job_id, action, params)
            if previous is not None:
                receipt = {'job_id': job_id, 'status': previous['status']}
                if previous.get('recovery_required'):
                    receipt.update(recovery_required=True, message=previous['message'])
                return receipt
            immediate = action in {'installer_state', 'installer_choose'}
            if not immediate and self.pending.full():
                raise ValueError('Bridge request queue is full; no work enqueued')
            job = {'job_id': job_id, 'action': action, 'params': params,
                   'created': time.monotonic(), 'status': 'running' if immediate else 'queued'}
            # A persisted mutation claim precedes any native action or enqueue.
            self.jobs.reserve(job, mutation=action in WRITE_ACTIONS or action == 'installer_choose')
            if immediate:
                try:
                    result = dialogs.state(self) if action == 'installer_state' else dialogs.choose(self, params)
                    job.update(status='done', result=result)
                except Exception as error:
                    self.fail_job(job, error, phase='installer_control',
                                  changes_may_have_occurred=action == 'installer_choose')
                self.audit_job(job)
                return {'job_id': job_id, 'status': job['status']}
            try:
                self.pending.put_nowait(job_id)
            except queue.Full:
                self.fail_job(job, RuntimeError('Bridge queue filled before execution'), phase='queued')
                self.audit_job(job)
                raise
            return {'job_id': job_id, 'status': 'queued'}

    def tick(self):
        poll = getattr(getattr(self.ops, 'running', None), 'poll', None)
        if callable(poll):
            poll()
        if self.busy:
            dialogs.accept_quick_install(self)
            return
        try:
            job_id = self.pending.get_nowait()
        except queue.Empty:
            return
        job = self.jobs[job_id]
        if time.monotonic() - job['created'] > 60:
            self.fail_job(job, RuntimeError('Queued request expired without execution'), phase='queued')
            self.audit_job(job)
            return
        operation_started = False
        job['status'] = 'running'
        self.busy = True
        self.active_job = job
        try:
            if not self.isActive():
                raise RuntimeError('The connection is disabled in MO2 settings')
            if job['action'] in WRITE_ACTIONS:
                if self.organizer.pluginSetting(self.name(), 'allow_changes') is False:
                    raise RuntimeError('Changes are disabled in MO2 Codex Bridge settings')
                if QApplication.activeModalWidget() is not None:
                    raise RuntimeError('Close the current MO2 dialog before modifying mods')
            operation_started = True
            result = self.ops.dispatch(job['action'], job['params'])
            job.update(status='done', result=result)
        except Exception as error:
            self.fail_job(job, error, phase='operation' if operation_started else 'preflight',
                          changes_may_have_occurred=operation_started and job['action'] in WRITE_ACTIONS)
        finally:
            self.busy = False
            self.active_job = None
            self.audit_job(job)

    def stop(self):
        if self.server is not None:
            self.timer.stop()
            self.server.close()
            self.server = None
        if self.ops.nexus is not None:
            self.ops.nexus.close()
            self.ops.nexus = None
        session = self.data / 'session.json'
        if session.exists():
            session.unlink()
