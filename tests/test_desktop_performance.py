"""Local fixtures only: polling costs, revisions and terminal reconciliation."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_updates as updates
from server import Store


class DesktopPerformanceTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='desktop-performance-', dir=directory)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.desktop = updates.DesktopUpdates(self.store)
        self.desktop.detect_burp = mock.Mock(return_value={
            'launcher': '/fixture/burp.desktop', 'jar': '/fixture/burp.jar',
            'version': '2026.7', 'custom_launcher': False, 'installed': True})
        self.desktop.running_burp = mock.Mock(return_value=[])
        self.which_patch = mock.patch('desktop_updates.shutil.which', side_effect=lambda name: '/usr/bin/' + name)
        self.which = self.which_patch.start()
        self.inventory = {'count': 1, 'managers': [{'id': 'apt', 'count': 1,
                          'packages': [{'name': 'openssl', 'installed': '1.0', 'candidate': '1.1'}]}]}
        self.desktop.set_setting('pc_inventory', self.inventory)
        self.desktop.set_setting('pc_operations', {})
        self.desktop.set_setting('pc_error', '')

    def tearDown(self):
        self.which_patch.stop()
        self.desktop.stop()
        self.store.db.close()
        self.temporary.cleanup()

    def revisions(self):
        return self.desktop.status()['revisions']

    def assert_bumped(self, before, after, kind):
        other = 'burp' if kind == 'pc' else 'pc'
        self.assertEqual(before[other], after[other])
        self.assertEqual(before[kind].split(':')[0], after[kind].split(':')[0])
        self.assertEqual(int(before[kind].split(':')[1]) + 1, int(after[kind].split(':')[1]))

    def terminal_fixture(self, data=None, create_file=True):
        path = self.home / 'terminal.json'
        job = {'state': 'running', 'operation': 'upgrade', 'manager': 'apt',
               'result_path': str(path), 'started_at': updates.now(), 'message': 'Waiting'}
        self.desktop.set_setting('pc_terminal_job', job)
        result = dict(job, pid=4242, completed_steps=[])
        result.update(data or {})
        if create_file:
            path.write_text(json.dumps(result))
        return path, result

    def test_pc_snapshot_skips_all_burp_discovery_and_settings(self):
        with mock.patch.object(self.desktop, 'get_setting', wraps=self.desktop.get_setting) as setting:
            result = self.desktop.snapshot(view='pc')
        self.assertEqual(set(result), {'pc', 'jobs', 'pc_error', 'terminal_available', 'revisions'})
        self.assertEqual(result['pc']['count'], 1)
        self.desktop.detect_burp.assert_not_called()
        self.desktop.running_burp.assert_not_called()
        self.assertFalse(any(call.args[0].startswith('burp_') for call in setting.call_args_list))

    def test_burp_snapshot_omits_package_inventory_and_history(self):
        with mock.patch.object(self.desktop, 'get_setting', wraps=self.desktop.get_setting) as setting, \
                mock.patch.object(self.desktop, 'with_package_history', side_effect=AssertionError('Inventory read')):
            result = self.desktop.snapshot(view='burp')
        self.assertEqual(set(result), {'burp', 'jobs', 'terminal_available', 'revisions'})
        self.assertEqual(result['burp']['version'], '2026.7')
        self.assertFalse({'pc_inventory', 'pc_operations', 'pc_error'} & {call.args[0] for call in setting.call_args_list})
        self.desktop.running_burp.assert_called_once_with()

    def test_status_is_small_and_never_reads_inventory_or_checks_binaries(self):
        self.desktop.jobs['pc'] = {'state': 'running', 'message': 'Checking'}
        before = self.store.db.total_changes
        with mock.patch.object(self.desktop, 'get_setting', wraps=self.desktop.get_setting) as setting, \
                mock.patch.object(self.desktop, 'with_package_history', side_effect=AssertionError('Inventory read')):
            result = self.desktop.status()
        self.assertEqual(set(result), {'jobs', 'revisions', 'pc_error'})
        self.assertEqual({call.args[0] for call in setting.call_args_list}, {'pc_terminal_job', 'pc_error'})
        self.assertEqual(self.store.db.total_changes, before)
        self.which.assert_not_called()
        self.desktop.detect_burp.assert_not_called()
        self.desktop.running_burp.assert_not_called()

    def test_legacy_snapshot_preserves_both_sections_and_returned_objects_are_independent(self):
        self.desktop.jobs['pc'] = {'state': 'done', 'completed_steps': [{'manager': 'apt'}]}
        result = self.desktop.snapshot()
        self.assertEqual(set(result), {'pc', 'burp', 'jobs', 'pc_error', 'terminal_available', 'revisions'})
        result['pc']['managers'][0]['packages'][0]['name'] = 'changed'
        result['jobs']['pc']['completed_steps'].clear()
        result['revisions']['pc'] = 'changed'
        repeated = self.desktop.snapshot(view='pc')
        self.assertEqual(repeated['pc']['managers'][0]['packages'][0]['name'], 'openssl')
        self.assertEqual(repeated['jobs']['pc']['completed_steps'], [{'manager': 'apt'}])
        self.assertNotEqual(repeated['revisions']['pc'], 'changed')

    def test_unknown_view_is_rejected_before_reading_or_discovery(self):
        with mock.patch.object(self.desktop, 'read_terminal_result') as terminal:
            with self.assertRaises(ValueError):
                self.desktop.snapshot(view='custom')
        terminal.assert_not_called()
        self.desktop.detect_burp.assert_not_called()

    def test_identical_settings_do_not_write_or_bump_even_with_legacy_json_format(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE metadata SET value=? WHERE key=?',
                                  (json.dumps(self.inventory, indent=2), 'desktop_pc_inventory'))
        before, changes = self.revisions(), self.store.db.total_changes
        reordered = dict(reversed(list(self.inventory.items())))
        self.assertFalse(self.desktop.set_setting('pc_inventory', reordered))
        self.assertEqual(self.revisions(), before)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertTrue(self.desktop.set_setting('pc_error', 'Offline'))
        self.assert_bumped(before, self.revisions(), 'pc')
        before = self.revisions()
        self.desktop.set_setting('pc_error', '')
        self.assert_bumped(before, self.revisions(), 'pc')

    def test_revisions_change_only_for_relevant_persisted_data(self):
        for key, value, kind in [
            ('pc_inventory', {'managers': []}, 'pc'),
            ('pc_operations', {'apt': {'upgrade': '2026-09-05'}}, 'pc'),
            ('burp_release', {'version': '2026.8'}, 'burp'),
            ('burp_attempt', updates.now(), 'burp'),
            ('burp_error', 'Offline', 'burp'),
            ('burp_last_install', {'version': '2026.8'}, 'burp'),
        ]:
            with self.subTest(key=key):
                before = self.revisions()
                self.desktop.set_setting(key, value)
                self.assert_bumped(before, self.revisions(), kind)
        before = self.revisions()
        self.desktop.set_setting('burp_notified', '2026.8')
        self.desktop.set_setting('pc_terminal_job', {'state': 'done', 'message': 'Complete'})
        self.desktop.jobs['pc'] = {'state': 'running', 'message': 'Starting'}
        self.desktop.progress('pc', 'Downloading', 50)
        self.assertEqual(self.revisions(), before)
        self.assertEqual(self.desktop.status()['jobs']['pc']['percent'], 50)

    def test_restart_has_a_new_revision_identity_without_rewriting_settings(self):
        before, changes = self.revisions(), self.store.db.total_changes
        reopened = updates.DesktopUpdates(self.store)
        try:
            after = reopened.status()['revisions']
            self.assertNotEqual(before['pc'], after['pc'])
            self.assertNotEqual(before['burp'], after['burp'])
            self.assertEqual(self.store.db.total_changes, changes)
        finally:
            reopened.stop()

    def test_history_merge_preserves_caller_data_without_json_roundtrip(self):
        self.desktop.set_setting('pc_operations', {'apt': {'upgrade': '2026-09-05T12:00:00+00:00'}})
        original = copy.deepcopy(self.inventory)
        with mock.patch.object(updates.json, 'dumps', side_effect=AssertionError('Unexpected serialization')):
            merged = self.desktop.with_package_history(self.inventory)
            result = self.desktop.snapshot(view='pc')
        self.assertEqual(self.inventory, original)
        self.assertEqual(merged['managers'][0]['last_upgrade_at'], '2026-09-05T12:00:00+00:00')
        self.assertEqual(result['pc'], merged)

    def test_terminal_availability_has_only_a_short_ui_cache(self):
        self.which.side_effect = ['/usr/bin/konsole', '/usr/bin/systemd-run', None]
        with mock.patch.object(updates.time, 'monotonic', side_effect=[100, 104.9, 105]):
            self.assertTrue(self.desktop.snapshot(view='pc')['terminal_available'])
            self.assertTrue(self.desktop.snapshot(view='pc')['terminal_available'])
            self.assertEqual(self.which.call_count, 2)
            self.assertFalse(self.desktop.snapshot(view='pc')['terminal_available'])
        self.assertEqual(self.which.call_count, 3)

    def test_launch_rechecks_terminal_even_when_ui_cache_says_available(self):
        self.assertTrue(self.desktop.snapshot(view='pc')['terminal_available'])
        self.which.side_effect = None
        self.which.return_value = None
        with mock.patch('desktop_updates.manager_binary', return_value='/usr/bin/apt-get'), \
                mock.patch('desktop_updates.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Konsole'):
                self.desktop.launch_manager('apt', 'upgrade')
        run.assert_not_called()

    def test_burp_install_still_checks_running_processes_freshly(self):
        self.desktop.set_setting('burp_release', {'version': '2026.8'})
        self.desktop.running_burp.side_effect = [[], [4242]]
        self.assertEqual(self.desktop.snapshot(view='burp')['burp']['running'], [])
        with mock.patch.object(self.desktop, 'submit') as submit:
            with self.assertRaisesRegex(RuntimeError, 'Close Burp'):
                self.desktop.install_burp('2026.8', preserve_launch=True)
        self.assertEqual(self.desktop.running_burp.call_count, 2)
        submit.assert_not_called()

    def test_unchanged_terminal_result_is_not_reread_or_rewritten_but_pid_is_checked(self):
        self.terminal_fixture()
        original_read = Path.read_text
        with mock.patch.object(Path, 'read_text', autospec=True, side_effect=original_read) as read, \
                mock.patch.object(Path, 'exists', return_value=True) as exists:
            self.desktop.status()
            changes, revision = self.store.db.total_changes, self.revisions()
            for _ in range(4):
                self.desktop.status()
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.store.db.total_changes, changes)
            self.assertEqual(self.revisions(), revision)
            self.assertGreaterEqual(exists.call_count, 5)
            exists.return_value = False
            self.assertEqual(self.desktop.status()['jobs']['pc']['state'], 'error')
            self.assertEqual(self.store.db.total_changes, changes + 1)
            self.assertEqual(self.revisions(), revision)

    def test_waiting_for_initial_result_does_not_write_on_every_poll(self):
        self.terminal_fixture(create_file=False)
        changes = self.store.db.total_changes
        for _ in range(4):
            self.assertEqual(self.desktop.status()['jobs']['pc']['state'], 'running')
        self.assertEqual(self.store.db.total_changes, changes)

    def test_terminal_steps_and_done_are_reconciled_once_and_trigger_one_inventory_check(self):
        at = '2026-09-05T12:00:00+00:00'
        first = {'manager': 'apt', 'operation': 'refresh-lists', 'finished_at': at}
        before = self.revisions()
        path, result = self.terminal_fixture({'completed_steps': [first]})
        with mock.patch.object(Path, 'exists', return_value=True), mock.patch.object(self.desktop, 'check_pc') as check:
            self.desktop.status()
            self.assert_bumped(before, self.revisions(), 'pc')
            changes = self.store.db.total_changes
            self.desktop.status()
            self.assertEqual(self.store.db.total_changes, changes)
            result.update(state='done', completed_steps=[first, {
                'manager': 'pipx', 'operation': 'upgrade', 'item': 'sqlmap', 'version': '1.10.9', 'finished_at': at}])
            path.write_text(json.dumps(result))
            final = self.desktop.status()
            self.assertEqual(final['jobs']['pc']['state'], 'done')
            check.assert_called_once_with()
            changes = self.store.db.total_changes
            self.desktop.status()
            self.assertEqual(self.store.db.total_changes, changes)
            check.assert_called_once_with()
        history = self.desktop.get_setting('pc_operations')
        self.assertEqual(history['apt']['refresh-lists'], at)
        self.assertEqual(history['pipx']['items']['sqlmap'], {'at': at, 'version': '1.10.9'})
        self.assertEqual(self.desktop.get_setting('pc_terminal_job')['state'], 'done')


if __name__ == '__main__':
    unittest.main()
