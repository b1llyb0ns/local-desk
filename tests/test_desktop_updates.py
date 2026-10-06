import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_updates as updates
import pc_packages
from server import Store


def jar_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('META-INF/MANIFEST.MF', 'Main-Class: example.Main\n')
        archive.writestr('example/Main.class', b'\xca\xfe\xba\xbe' + struct.pack('>HH', 0, 65))
    return buffer.getvalue()


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


class DesktopTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='desktop-test-', dir=directory)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.updates = updates.DesktopUpdates(self.store)
        self.updates.running_burp = mock.Mock(return_value=[])
        self.updates.burp_directory.mkdir(parents=True)
        self.old = self.updates.burp_directory / 'burpsuite_desktop_v2026.7.3.jar'
        self.old.write_bytes(b'old-file')
        self.updates.launcher.parent.mkdir(parents=True)
        self.original = '[Desktop Entry]\nName=Burp Suite\nExec=/usr/bin/java -javaagent:custom-agent.jar -noverify -jar ' + str(self.old) + '\n'
        self.updates.launcher.write_text(self.original)
        self.alias_original = "alias burp='/usr/bin/java -javaagent:custom-agent.jar -noverify -jar " + str(self.old) + "'\n"
        self.updates.aliases.write_text(self.alias_original)
        self.payload = jar_bytes()
        self.release = {'version': '2026.8', 'sha256': hashlib.sha256(self.payload).hexdigest(),
                        'download_url': 'https://portswigger.net/burp/releases/startdownload?product=desktop&version=2026.8&type=jar',
                        'checked_at': updates.now()}
        self.updates.set_setting('burp_release', self.release)
        self.updates.jobs['burp'] = {'state': 'running'}

    def tearDown(self):
        self.updates.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def test_metadata_parser_checks_version_and_checksum(self):
        listing = '<input id="CurrentVersion" value="2026.8">'
        details = '<option value=Jar buildCategoryId=desktop sha256Checksum=' + self.release['sha256'] + '>JAR</option><a download=release href="/burp/releases/startdownload?product=desktop&amp;version=2026.8&amp;type=jar">JAR</a>'
        data = updates.stable_release(mock.Mock(side_effect=[listing, details]))
        self.assertEqual(data['version'], '2026.8')
        self.assertEqual(data['sha256'], self.release['sha256'])
        with self.assertRaises(RuntimeError):
            updates.stable_release(mock.Mock(side_effect=[listing, details.replace('version=2026.8', 'version=2026.9')]))
        with self.assertRaises(RuntimeError):
            updates.stable_release(mock.Mock(side_effect=[listing, '<html>No checksum</html>']))

    def test_downloads_and_redirects_reject_non_official_sources(self):
        for url in ['http://portswigger.net/burp', 'https://portswigger.net.evil.test/file', 'https://elsewhere.test/file', 'https://user@portswigger.net/file']:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                updates.official_url(url)
        handler = updates.OfficialRedirect()
        with self.assertRaises(RuntimeError):
            handler.redirect_request(None, None, 302, '', {}, 'https://elsewhere.test/file')

    def test_version_compare_and_detection_never_run_launcher(self):
        self.assertTrue(updates.newer('2026.10', '2026.9'))
        self.assertFalse(updates.newer('2026.7.3', '2026.8'))
        self.assertFalse(updates.newer('2026.8.0', '2026.8'))
        with mock.patch('desktop_updates.subprocess.run') as run:
            detected = self.updates.detect_burp()
            self.assertEqual(detected['version'], '2026.7.3')
            self.assertTrue(detected['custom_launcher'])
            run.assert_not_called()

    def test_verified_install_keeps_custom_launcher_and_updates_jar_paths(self):
        with mock.patch('desktop_updates.open_official', return_value=Response(self.payload)), mock.patch('desktop_updates.verify_java', return_value=21):
            self.updates.perform_install(self.release)
        self.assertEqual(self.old.read_bytes(), b'old-file')
        self.assertEqual(self.updates.detect_burp()['version'], '2026.8')
        launcher = self.updates.launcher.read_text()
        self.assertIn('javaagent', launcher)
        self.assertIn('noverify', launcher)
        self.assertIn('/usr/bin/java', launcher)
        self.assertIn('/burpsuite_desktop_v2026.8.jar', launcher)
        alias = self.updates.aliases.read_text()
        self.assertIn('javaagent', alias)
        self.assertIn('/burpsuite_desktop_v2026.8.jar', alias)
        self.assertEqual(self.updates.get_setting('burp_last_install')['sha256'], self.release['sha256'])
        backups = list((self.store.directory / 'burp-backups').glob('*.txt'))
        self.assertEqual(len(backups), 2)
        self.assertIn(self.original, [path.read_text() for path in backups])
        self.assertIn(self.alias_original, [path.read_text() for path in backups])

    def test_invalid_download_or_java_does_not_replace_launcher(self):
        for data, java_error in [(b'not-the-release', None), (self.payload, RuntimeError('Java too old'))]:
            with self.subTest(java_error=java_error), mock.patch('desktop_updates.open_official', return_value=Response(data)), mock.patch('desktop_updates.verify_java', side_effect=java_error):
                with self.assertRaises(RuntimeError):
                    self.updates.perform_install(self.release)
            self.assertEqual(self.updates.launcher.read_text(), self.original)
            self.assertEqual(self.updates.aliases.read_text(), self.alias_original)
            self.assertEqual(self.old.read_bytes(), b'old-file')
            self.assertEqual(list(self.updates.burp_directory.glob('*.part')), [])

    def test_concurrent_launcher_edit_is_preserved(self):
        changed = self.original + '# user edit\n'
        def verify(path):
            self.updates.launcher.write_text(changed)
        with mock.patch('desktop_updates.open_official', return_value=Response(self.payload)), mock.patch('desktop_updates.verify_java', side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, 'launcher or Burp alias changed'):
                self.updates.perform_install(self.release)
        self.assertEqual(self.updates.launcher.read_text(), changed)

    def test_custom_alias_recovers_a_replaced_standard_desktop_launcher(self):
        managed = self.updates.burp_directory / 'managed/burpsuite_desktop_v2026.8.jar'
        managed.parent.mkdir()
        managed.write_bytes(b'new-file')
        self.updates.launcher.write_text('[Desktop Entry]\nExec=/usr/bin/java -jar ' + str(managed) + '\n')
        detected = self.updates.detect_burp()
        self.assertEqual(detected['launch_source'], 'alias')
        self.assertEqual(detected['version'], '2026.7.3')
        self.assertTrue(detected['custom_launcher'])

    def test_install_requires_explicit_confirmation_and_closed_burp(self):
        with self.assertRaisesRegex(RuntimeError, 'Confirm'):
            self.updates.install_burp('2026.8', False)
        with self.assertRaises(RuntimeError):
            self.updates.install_burp('2027.1', True)
        self.updates.running_burp.return_value = [1234]
        with self.assertRaisesRegex(RuntimeError, 'Close Burp'):
            self.updates.install_burp('2026.8', True)

    def test_automatic_downgrade_is_rejected(self):
        with mock.patch.object(self.updates, 'detect_burp', return_value={'version': '2026.9'}):
            with self.assertRaisesRegex(RuntimeError, 'downgrades'):
                self.updates.install_burp('2026.8', True)

    def test_java_validation_does_not_execute_jar(self):
        jar = self.home / 'test.jar'
        jar.write_bytes(self.payload)
        with mock.patch('desktop_updates.subprocess.run', return_value=mock.Mock(returncode=0, stdout='', stderr='openjdk version "21.0.1"')) as run:
            self.assertEqual(updates.verify_java(jar), 21)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/java', '-version'])
        with mock.patch('desktop_updates.subprocess.run', return_value=mock.Mock(returncode=0, stdout='', stderr='openjdk version "17.0.1"')):
            with self.assertRaisesRegex(RuntimeError, 'Java 21'):
                updates.verify_java(jar)

    def test_interactive_package_button_uses_fixed_user_terminal_command(self):
        with mock.patch('desktop_updates.shutil.which', side_effect=lambda name, **kwargs: '/usr/bin/' + name), mock.patch('desktop_updates.subprocess.run', return_value=mock.Mock(returncode=0)) as run:
            self.updates.launch_apt('upgrade')
            command = run.call_args.args[0]
            self.assertEqual(command[0], '/usr/bin/systemd-run')
            self.assertIn('--user', command)
            self.assertIn('/usr/bin/konsole', command)
            self.assertIn('upgrade', command)
            self.assertNotIn('-y', command)
            self.assertNotIn('shell', run.call_args.kwargs)
            with self.assertRaises(RuntimeError):
                self.updates.launch_apt('upgrade')

    def test_terminal_helper_requires_apt_confirmation_and_no_removals(self):
        path = self.store.directory / 'result.json'
        with mock.patch('pc_packages.subprocess.run', return_value=mock.Mock(returncode=0)) as run, mock.patch('builtins.print'), mock.patch('builtins.input', return_value='yes'):
            self.assertEqual(pc_packages.interactive('upgrade', path), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn('--no-remove', commands[1])
        self.assertIn('APT::Update::Error-Mode=any', commands[0])
        self.assertNotIn('-y', commands[1])
        self.assertEqual(json.loads(path.read_text())['state'], 'done')

    def test_apt_failure_stops_before_upgrade(self):
        path = self.store.directory / 'result.json'
        with mock.patch('pc_packages.subprocess.run', return_value=mock.Mock(returncode=100)) as run, mock.patch('builtins.print'), mock.patch('builtins.input', return_value='yes'):
            self.assertEqual(pc_packages.interactive('upgrade', path), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(json.loads(path.read_text())['state'], 'error')

    def test_automatic_checks_default_to_disabled_and_legacy_setting_is_migrated(self):
        self.assertIs(self.updates.get_setting('burp_weekly'), False)
        self.updates.set_setting('burp_weekly', True)
        reopened = updates.DesktopUpdates(self.store)
        try:
            with mock.patch('desktop_updates.open_official') as fetch, mock.patch('desktop_updates.stable_release') as release:
                self.assertIs(reopened.get_setting('burp_weekly'), False)
                data = reopened.snapshot(view='burp')
                self.assertIs(data['burp']['weekly'], False)
                self.assertIsNone(data['burp']['next_check'])
                fetch.assert_not_called()
                release.assert_not_called()
        finally:
            reopened.stop()

    def test_background_checks_never_contact_burp_even_with_enabled_legacy_setting(self):
        current = dt.datetime(2026, 9, 5, 12, tzinfo=dt.timezone.utc)
        self.updates.set_setting('pc_inventory', {'checked_at': current.isoformat(), 'inventory_version': 2, 'managers': []})
        self.updates.set_setting('burp_weekly', True)
        with mock.patch.object(self.updates, 'check_pc') as pc, \
                mock.patch.object(self.updates, 'check_burp') as burp, \
                mock.patch.object(self.updates, 'install_burp') as install, \
                mock.patch('desktop_updates.open_official') as fetch, \
                mock.patch('desktop_updates.stable_release') as release:
            for cached, attempt in [({}, None), ({'checked_at': '2025-01-01T00:00:00+00:00'}, None),
                                    ({}, '2025-01-01T00:00:00+00:00'), ({'checked_at': 'invalid'}, 'invalid')]:
                self.updates.set_setting('burp_release', cached)
                self.updates.set_setting('burp_attempt', attempt)
                for elapsed in [dt.timedelta(), dt.timedelta(hours=1), dt.timedelta(days=7), dt.timedelta(days=365)]:
                    with self.subTest(cached=cached, attempt=attempt, elapsed=elapsed):
                        self.updates.check_due(current + elapsed)
            burp.assert_not_called()
            install.assert_not_called()
            fetch.assert_not_called()
            release.assert_not_called()
            self.assertEqual(pc.call_count, 8)

    def test_service_start_never_checks_burp(self):
        self.updates.set_setting('burp_weekly', True)
        self.updates.set_setting('burp_release', {})
        with mock.patch.object(self.updates.stop_event, 'is_set', side_effect=[False, True]), \
                mock.patch.object(self.updates.stop_event, 'wait') as wait, \
                mock.patch.object(self.updates, 'check_pc') as pc, \
                mock.patch.object(self.updates, 'check_burp') as burp, \
                mock.patch('desktop_updates.open_official') as fetch:
            self.updates.start()
            self.updates.threads[-1].join(timeout=2)
            self.assertFalse(self.updates.threads[-1].is_alive())
            pc.assert_called_once_with()
            burp.assert_not_called()
            fetch.assert_not_called()
            wait.assert_called_once_with(3600)

    def test_daily_package_inventory_schedule_is_preserved(self):
        current = dt.datetime(2026, 9, 5, 12, tzinfo=dt.timezone.utc)
        self.updates.set_setting('pc_inventory', {'checked_at': current.isoformat(), 'inventory_version': 2, 'managers': []})
        with mock.patch.object(self.updates, 'check_pc') as pc, mock.patch.object(self.updates, 'check_burp') as burp:
            self.updates.check_due(current + dt.timedelta(hours=23, minutes=59))
            pc.assert_not_called()
            self.updates.check_due(current + dt.timedelta(days=1))
            pc.assert_called_once_with()
            burp.assert_not_called()

    def test_manual_check_fetches_once_without_installing(self):
        self.updates.jobs.pop('burp')
        with mock.patch('desktop_updates.stable_release', return_value=self.release) as release, \
                mock.patch.object(self.updates, 'perform_install') as install:
            self.updates.check_burp()
            self.updates.threads[-1].join(timeout=2)
            self.assertFalse(self.updates.threads[-1].is_alive())
            release.assert_called_once_with()
            install.assert_not_called()
        self.assertEqual(self.updates.jobs['burp']['state'], 'done')
        self.assertEqual(self.updates.get_setting('burp_release'), self.release)
        self.assertIsNotNone(self.updates.get_setting('burp_attempt'))
        self.assertEqual(self.updates.launcher.read_text(), self.original)

    def test_package_check_failure_does_not_trigger_a_burp_check(self):
        self.updates.set_setting('burp_release', {})
        self.updates.jobs['pc'] = {'state': 'running'}
        with mock.patch.object(self.updates, 'check_pc', side_effect=RuntimeError('Busy')), mock.patch.object(self.updates, 'check_burp') as burp:
            self.updates.check_due()
            burp.assert_not_called()

    def test_manager_actions_reject_missing_or_unknown_manager(self):
        with mock.patch('desktop_updates.manager_binary', side_effect=RuntimeError('Not installed')), mock.patch('desktop_updates.subprocess.run') as run:
            with self.assertRaises(RuntimeError):
                self.updates.launch_manager('flatpak', 'upgrade')
            with self.assertRaises(RuntimeError):
                self.updates.check_manager('flatpak')
            run.assert_not_called()
        with mock.patch('desktop_updates.manager_binary', return_value='/usr/bin/snap'):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
                self.updates.launch_manager('snap', 'refresh-lists')

    def test_snapshot_reads_cached_managers_without_package_commands(self):
        self.updates.set_setting('pc_inventory', {'managers': [{'id': 'apt', 'count': 3}, {'id': 'snap', 'count': None}]})
        self.updates.set_setting('pc_operations', {'apt': {'refresh-lists': '2026-09-05T12:00:00+00:00'}})
        with mock.patch('desktop_updates.subprocess.run') as run:
            data = self.updates.snapshot()
            self.assertEqual(data['pc']['last_refresh_at'], '2026-09-05T12:00:00+00:00')
            self.assertEqual(len(data['pc']['managers']), 2)
            self.assertEqual(data['pc']['managers'][0]['last_refresh_source'], 'Successful command from Local Desk')
            run.assert_not_called()

    def test_refresh_success_survives_later_upgrade_failure(self):
        path = self.store.directory / 'result.json'
        with mock.patch('pc_packages.subprocess.run', side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=1)]), mock.patch('builtins.print'), mock.patch('builtins.input', return_value='yes'):
            self.assertEqual(pc_packages.interactive('upgrade', path), 1)
        result = json.loads(path.read_text())
        self.assertEqual([step['operation'] for step in result['completed_steps']], ['refresh-lists'])
        self.updates.set_setting('pc_terminal_job', {'state': 'running', 'result_path': str(path)})
        with mock.patch.object(self.updates, 'check_pc') as check:
            self.updates.read_terminal_result()
            check.assert_not_called()
        self.assertIn('refresh-lists', self.updates.get_setting('pc_operations')['apt'])
        self.assertNotIn('upgrade', self.updates.get_setting('pc_operations')['apt'])

    def test_new_manager_snapshot_is_bootstrapped_without_waiting_a_day(self):
        self.updates.set_setting('pc_inventory', {'checked_at': updates.now()})
        with mock.patch.object(self.updates, 'check_pc') as check:
            self.updates.check_due()
            check.assert_called_once()

    def test_manager_check_preserves_other_managers_and_full_inventory_age(self):
        self.updates.jobs.pop('pc', None)
        earlier = '2026-09-01T12:00:00+00:00'
        self.updates.set_setting('pc_inventory', {'checked_at': earlier, 'count': 3, 'managers': [{'id': 'apt', 'count': 3}]})
        result = {'id': 'snap', 'name': 'Snap', 'state': 'ok', 'count': 1, 'packages': [{'name': 'slack'}], 'updates_checked_at': updates.now()}
        with mock.patch('desktop_updates.manager_binary', return_value='/usr/bin/snap'), mock.patch('desktop_updates.subprocess.run', return_value=mock.Mock(returncode=0, stdout=json.dumps(result))) as run:
            self.updates.check_manager('snap')
            for thread in self.updates.threads:
                thread.join(timeout=2)
            self.assertEqual(run.call_args.args[0][-3:], ['check', '--manager', 'snap'])
        data = self.updates.get_setting('pc_inventory')
        self.assertEqual(data['checked_at'], earlier)
        self.assertEqual(data['count'], 3)
        self.assertEqual([manager['id'] for manager in data['managers']], ['apt', 'snap'])
        self.assertEqual(data['managers'][1]['count'], 1)

    def test_daily_cache_preserves_manual_snap_check_until_new_upgrade(self):
        pending_at = '2026-09-04T12:00:00+00:00'
        self.updates.set_setting('pc_inventory', {'managers': [{'id': 'snap', 'count': 1, 'packages': [{'name': 'slack'}], 'updates_checked_at': pending_at}]})
        result = {'count': 3, 'managers': [{'id': 'apt', 'count': 3}, {'id': 'snap', 'state': 'ok', 'count': None}]}
        with mock.patch('desktop_updates.subprocess.run', return_value=mock.Mock(returncode=0, stdout=json.dumps(result))):
            self.updates.check_pc()
            for thread in self.updates.threads:
                thread.join(timeout=2)
        data = self.updates.get_setting('pc_inventory')
        self.assertEqual(data['managers'][1]['count'], 1)
        self.assertEqual(data['managers'][1]['updates_checked_at'], pending_at)
        self.updates.set_setting('pc_operations', {'snap': {'upgrade': '2026-09-05T12:00:00+00:00'}})
        data = self.updates.with_package_history(data)
        self.assertIsNone(data['managers'][1]['count'])
        self.assertIsNone(data['managers'][1]['updates_checked_at'])


if __name__ == '__main__':
    unittest.main()
