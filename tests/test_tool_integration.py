import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_updates
import go_toolchain
import local_tool_job
import server
import tool_packages


def manager_record(manager='go-tools', unknown=False):
    item, installed, candidate = {
        'go': ('go', 'go1.27.0', 'go1.27.1'),
        'go-tools': ('gopls', 'v0.20.0', 'v0.20.1'),
        'pdtm': ('pdtm', 'v0.1.0', 'v0.1.1'),
        'pipx': ('ruff', '0.12.0', '0.12.1'),
    }[manager]
    return {'id': manager, 'name': manager, 'state': 'ok', 'checked_at': desktop_updates.now(),
            'updates_checked_at': desktop_updates.now(), 'count': 1,
            'packages': [{'id': item, 'name': item, 'installed': 'Unknown' if unknown else installed,
                          'candidate': candidate, 'updatable': True, 'version_unknown': unknown}]}


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='tools-flow-', dir=directory)
        self.workspace = Path(self.temporary.name)
        self.store = server.Store(self.workspace / 'data', self.workspace, import_existing=False, export=False)
        self.updates = desktop_updates.DesktopUpdates(self.store)
        self.updates.set_setting('pc_inventory', {'checked_at': desktop_updates.now(), 'managers': [manager_record()]})

    def tearDown(self):
        self.updates.stop()
        self.store.db.close()
        self.temporary.cleanup()


class LaunchTests(IntegrationBase):
    def test_exact_cached_candidate_launches_one_fixed_terminal_command(self):
        with mock.patch.object(desktop_updates.shutil, 'which', side_effect=lambda name, **kwargs: '/usr/bin/' + name), \
                mock.patch.object(desktop_updates.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='')) as run:
            self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1')
        command = run.call_args.args[0]
        self.assertEqual(command[0], '/usr/bin/systemd-run')
        self.assertIn('--user', command)
        self.assertIn('/usr/bin/konsole', command)
        self.assertIn(str(desktop_updates.HERE / 'local_tool_job.py'), command)
        index = command.index('install')
        self.assertEqual(command[index:index + 7], ['install', '--manager', 'go-tools', '--item', 'gopls', '--version', 'v0.20.1'])
        self.assertNotIn('--allow-unknown-version', command)
        self.assertNotIn('/usr/bin/sudo', command)
        self.assertNotIn('shell', run.call_args.kwargs)
        result_path = Path(command[command.index('--result') + 1])
        self.assertEqual(result_path.parent, self.store.directory / 'pc-jobs')
        self.assertEqual(self.updates.get_setting('pc_terminal_job')['version'], 'v0.20.1')

    def test_other_item_version_and_unchecked_inventory_never_launch(self):
        with mock.patch.object(desktop_updates.subprocess, 'run') as run:
            for arguments in [('go-tools', 'other', 'v0.20.1'), ('go-tools', 'gopls', 'v0.20.2'), ('other', 'gopls', 'v0.20.1')]:
                with self.subTest(arguments=arguments), self.assertRaises(RuntimeError):
                    self.updates.launch_tool(*arguments)
            record = manager_record()
            record['updates_checked_at'] = None
            self.updates.set_setting('pc_inventory', {'managers': [record]})
            with self.assertRaises(RuntimeError):
                self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1')
            run.assert_not_called()

    def test_unknown_version_requires_exact_boolean_confirmation(self):
        self.updates.set_setting('pc_inventory', {'managers': [manager_record(unknown=True)]})
        with mock.patch.object(desktop_updates.shutil, 'which', side_effect=lambda name, **kwargs: '/usr/bin/' + name), \
                mock.patch.object(desktop_updates.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='')) as run:
            for allow in [False, 'true', 1]:
                with self.subTest(allow=allow), self.assertRaises(RuntimeError):
                    self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1', allow)
            run.assert_not_called()
            self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1', True)
            self.assertEqual(run.call_args.args[0][-1], '--allow-unknown-version')

    def test_busy_pc_task_and_missing_terminal_never_launch(self):
        self.updates.jobs['pc'] = {'state': 'running'}
        with mock.patch.object(desktop_updates.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1')
            self.updates.jobs.clear()
            with mock.patch.object(desktop_updates.shutil, 'which', return_value=None), self.assertRaisesRegex(RuntimeError, 'Konsole'):
                self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1')
            run.assert_not_called()

    def test_terminal_spawn_error_is_persisted_instead_of_leaving_busy_state(self):
        with mock.patch.object(desktop_updates.shutil, 'which', side_effect=lambda name, **kwargs: '/usr/bin/' + name), \
                mock.patch.object(desktop_updates.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemd-run', 10)):
            with self.assertRaises(RuntimeError):
                self.updates.launch_tool('go-tools', 'gopls', 'v0.20.1')
        self.assertEqual(self.updates.jobs['pc']['state'], 'error')
        self.assertEqual(self.updates.get_setting('pc_terminal_job')['state'], 'error')

    def test_version_check_uses_only_the_check_helper_and_saves_inventory(self):
        checked = manager_record('pipx')
        with mock.patch.object(desktop_updates.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(checked))) as run, \
                mock.patch.object(self.updates, 'launch_tool') as launch:
            self.updates.check_tools('pipx')
            for thread in self.updates.threads:
                thread.join(timeout=2)
            self.assertEqual(self.updates.jobs['pc']['state'], 'done')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/python3', str(desktop_updates.HERE / 'local_tool_job.py'), 'check', '--manager', 'pipx'])
            self.assertEqual(run.call_args.kwargs['env']['GOTOOLCHAIN'], 'local')
            launch.assert_not_called()
        saved = self.updates.get_setting('pc_inventory')['managers']
        self.assertEqual(next(row for row in saved if row['id'] == 'pipx')['packages'][0]['candidate'], '0.12.1')

    def test_new_local_pin_invalidates_an_old_offered_candidate(self):
        old = manager_record('pipx')
        old['packages'][0].update(path=str(self.workspace / 'ruff'), source='PyPI', reason='')
        self.updates.set_setting('pc_inventory', {'managers': [old]})
        fresh = json.loads(json.dumps(old))
        fresh.update(updates_checked_at=None, count=None)
        fresh['packages'][0].update(updatable=False, candidate=None, reason='Pinned package; manual review required.')
        with mock.patch.object(desktop_updates.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps({'managers': [fresh]}))) as run:
            self.updates.check_pc()
            for thread in self.updates.threads:
                thread.join(timeout=2)
            self.assertEqual(self.updates.jobs['pc']['state'], 'done')
            saved = self.updates.get_setting('pc_inventory')['managers'][0]
            self.assertFalse(saved['packages'][0]['updatable'])
            self.assertIsNone(saved['packages'][0]['candidate'])
            self.assertIsNone(saved['updates_checked_at'])
            with self.assertRaises(RuntimeError):
                self.updates.launch_tool('pipx', 'ruff', '0.12.1')
            run.assert_called_once()


class TerminalTests(IntegrationBase):
    def test_local_and_remote_version_checks_never_invoke_installers(self):
        with mock.patch.object(go_toolchain, 'local_inventory', return_value=manager_record('go')) as local, \
                mock.patch.object(go_toolchain, 'check_inventory', return_value=manager_record('go')) as check_go, \
                mock.patch.object(tool_packages, 'manager_inventory', return_value=manager_record('pipx')) as check_tools, \
                mock.patch.object(go_toolchain, 'interactive_update') as install_go, mock.patch.object(tool_packages, 'update_item') as install_tool:
            local_tool_job.inventory('go', check=False, home=self.workspace)
            local_tool_job.inventory('go', check=True, home=self.workspace)
            local_tool_job.inventory('pipx', check=True, home=self.workspace)
            local.assert_called_once_with(self.workspace)
            check_go.assert_called_once_with()
            check_tools.assert_called_once_with('pipx', self.workspace, check=True)
            install_go.assert_not_called()
            install_tool.assert_not_called()

    def test_terminal_cancellation_saves_error_without_a_completed_step(self):
        path = self.store.directory / 'result.json'
        with mock.patch('builtins.input', return_value='no'), mock.patch('builtins.print'), mock.patch.object(tool_packages, 'update_item') as update:
            self.assertEqual(local_tool_job.interactive('pipx', 'ruff', '0.12.1', path), 1)
            update.assert_not_called()
        result = json.loads(path.read_text())
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['completed_steps'], [])
        self.assertIn('Cancelled', result['message'])

    def test_go_root_helper_cancellation_is_not_reported_as_success(self):
        path = self.store.directory / 'result.json'
        with mock.patch('builtins.print'), mock.patch.object(go_toolchain, 'interactive_update', side_effect=RuntimeError('Cancelled')):
            self.assertEqual(local_tool_job.interactive('go', 'go', 'go1.27.1', path), 1)
        self.assertEqual(json.loads(path.read_text())['completed_steps'], [])

    def test_unreadable_tool_confirmation_never_calls_the_installer(self):
        path = self.store.directory / 'result.json'
        for error in [UnicodeDecodeError('utf-8', b'ye\xd1s', 2, 3, 'invalid continuation byte'), EOFError()]:
            with self.subTest(error=type(error).__name__), mock.patch('builtins.input', side_effect=error), \
                    mock.patch('builtins.print'), mock.patch.object(tool_packages, 'update_item') as update:
                self.assertEqual(local_tool_job.interactive('pdtm', 'nuclei', 'v3.9.1', path), 1)
                update.assert_not_called()
            result = json.loads(path.read_text())
            self.assertEqual(result['state'], 'error')
            self.assertEqual(result['completed_steps'], [])
            self.assertIn('Cancelled', result['message'])
            self.assertNotIn('codec', result['message'])

    def test_successful_update_passes_pinned_version_and_records_backup(self):
        path = self.store.directory / 'result.json'
        result = {'state': 'done', 'version': 'v0.20.1', 'backup': str(self.workspace / 'gopls.previous')}
        with mock.patch('builtins.input', return_value='yes'), mock.patch('builtins.print'), \
                mock.patch.object(local_tool_job.Path, 'home', return_value=self.workspace), mock.patch.object(tool_packages, 'update_item', return_value=result) as update:
            self.assertEqual(local_tool_job.interactive('go-tools', 'gopls', 'v0.20.1', path, True), 0)
        update.assert_called_once_with('go-tools', 'gopls', 'v0.20.1', self.workspace, allow_unknown_version=True)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['state'], 'done')
        self.assertEqual(saved['backup'], result['backup'])
        self.assertEqual(saved['completed_steps'][0]['version'], 'v0.20.1')

    def test_missing_completion_or_wrong_version_is_not_recorded_as_done(self):
        path = self.store.directory / 'result.json'
        for result in [{}, {'state': 'done', 'version': '0.13.0'}, {'state': 'error', 'version': '0.12.1'}]:
            with self.subTest(result=result), mock.patch('builtins.input', return_value='yes'), mock.patch('builtins.print'), mock.patch.object(tool_packages, 'update_item', return_value=result):
                self.assertEqual(local_tool_job.interactive('pipx', 'ruff', '0.12.1', path), 1)
                self.assertEqual(json.loads(path.read_text())['completed_steps'], [])


class ApiTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        engine = SimpleNamespace(fingerprint='SHA256:public-key', key_options=lambda: [])
        self.app = server.Application(self.store, engine)
        self.app.desktop = self.updates
        self.http = server.make_server(self.app, 0)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        self.app.pool.shutdown(wait=True)
        super().tearDown()

    def post(self, path, body, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.request('POST', path, json.dumps(body), self.headers if headers is None else headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data)

    def test_api_rejects_missing_csrf_extra_fields_and_unknown_manager(self):
        with mock.patch.object(self.updates, 'launch_tool') as launch, mock.patch.object(self.updates, 'check_tools') as check:
            path = '/api/tool-updates/go-tools/install'
            valid = {'item': 'gopls', 'version': 'v0.20.1', 'allow_unknown_version': False}
            self.assertEqual(self.post(path, valid, {'Content-Type': 'application/json'})[0], 403)
            self.assertEqual(self.post(path, dict(valid, command='custom'))[0], 400)
            self.assertEqual(self.post(path, {'item': 'gopls', 'version': 'v0.20.1'})[0], 400)
            self.assertEqual(self.post('/api/tool-updates/go-tools/check', {'item': 'gopls'})[0], 400)
            self.assertEqual(self.post('/api/tool-updates/other/install', valid)[0], 404)
            launch.assert_not_called()
            check.assert_not_called()

    def test_api_forwards_only_manager_item_exact_version_and_explicit_flag(self):
        with mock.patch.object(self.updates, 'launch_tool') as launch, mock.patch.object(self.updates, 'check_tools') as check, \
                mock.patch.object(self.updates, 'snapshot', return_value={'pc': {}, 'jobs': {}}):
            self.assertEqual(self.post('/api/tool-updates/go-tools/install', {'item': 'gopls', 'version': 'v0.20.1', 'allow_unknown_version': False})[0], 202)
            self.assertEqual(self.post('/api/tool-updates/pipx/check', {})[0], 202)
            launch.assert_called_once_with('go-tools', 'gopls', 'v0.20.1', False)
            check.assert_called_once_with('pipx')

    def test_api_rejects_wrong_types_before_terminal_execution(self):
        with mock.patch.object(desktop_updates.subprocess, 'run') as run:
            self.assertEqual(self.post('/api/tool-updates/go-tools/install', {'item': 'gopls', 'version': 'v0.20.1', 'allow_unknown_version': 'yes'})[0], 400)
            self.assertEqual(self.post('/api/tool-updates/go-tools/install', {'item': ['gopls'], 'version': 'v0.20.1', 'allow_unknown_version': False})[0], 400)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
