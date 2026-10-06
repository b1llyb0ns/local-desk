import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pc_packages as packages


class PackageInventoryTests(unittest.TestCase):
    def test_history_uses_completed_upgrades_not_last_install(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        with tempfile.TemporaryDirectory(prefix='packages-', dir=directory) as temporary:
            path = Path(temporary)
            (path / 'history.log').write_text(
                'Start-Date: 2026-09-01  10:00:00\nUpgrade: curl:amd64 (1, 2)\nEnd-Date: 2026-09-01  10:00:02\n\n'
                'Start-Date: 2026-09-02  10:00:00\nInstall: wget:amd64 (1)\nEnd-Date: 2026-09-02  10:00:02\n\n'
                'Start-Date: 2026-09-03  10:00:00\nUpgrade: curl:amd64 (2, 3)\n')
            data = packages.apt_history(path)
        self.assertTrue(data['last_upgrade_at'].startswith('2026-09-01'))
        self.assertTrue(data['last_transaction_at'].startswith('2026-09-02'))

    def test_daily_snap_inventory_never_queries_updates_or_claims_none(self):
        values = ['Name Version Rev Tracking Publisher Notes\nslack 4.52.155 260 latest/stable slack -\n',
                  'last: 2026-09-05T13:34:00+02:00\nnext: 2026-09-05T19:52:00+02:00\n', 'no changes found\n']
        with mock.patch('pc_packages.read_command', side_effect=values) as run:
            data = packages.snap_inventory('/usr/bin/snap')
        self.assertIsNone(data['count'])
        self.assertIsNone(data['last_upgrade_at'])
        self.assertEqual(data['installed_count'], 1)
        self.assertTrue(data['last_auto_refresh_at'].startswith('2026-09-05T11:34'))
        self.assertFalse(any('--list' in call.args[0] for call in run.call_args_list))

    def test_snap_explicit_check_normalizes_versions_and_completed_refresh(self):
        values = ['Name Version Rev Tracking Publisher Notes\nslack 4.52 260 latest/stable slack -\n', '',
                  'ID Status Spawn Ready Summary\n7 Done 2026-09-04T10:00:00+02:00 2026-09-04T10:00:02+02:00 Refresh snap slack\n',
                  'Name Version Rev Publisher Notes\nslack 4.53 261 slack -\n']
        with mock.patch('pc_packages.read_command', side_effect=values):
            data = packages.snap_inventory('/usr/bin/snap', True)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['packages'][0]['installed'], '4.52')
        self.assertEqual(data['packages'][0]['candidate'], '4.53')
        self.assertEqual(data['last_upgrade_at'], '2026-09-04T08:00:02+00:00')

    def test_flatpak_local_inventory_does_not_contact_remotes(self):
        with mock.patch('pc_packages.read_command', return_value='org.gnome.Builder\t49.0\tuser\n') as run:
            data = packages.flatpak_inventory('/usr/bin/flatpak')
        self.assertEqual(data['installed_count'], 1)
        self.assertIsNone(data['count'])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('remote-ls', run.call_args.args[0])

    def test_only_installed_managers_are_included_apt_first(self):
        with mock.patch('local_tool_job.inventory', return_value=[]), mock.patch('pc_packages.shutil.which', side_effect=lambda binary, **kwargs: '/usr/bin/' + binary if binary in {'apt-get', 'snap'} else None), mock.patch('pc_packages.manager_inventory', side_effect=lambda manager: {'id': manager, 'count': 2}) as inventory:
            data = packages.inventory()
        self.assertEqual([entry['id'] for entry in data['managers']], ['apt', 'snap'])
        self.assertEqual(data['count'], 2)
        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(data['inventory_version'], 2)

    def test_tool_managers_follow_system_managers(self):
        extra = [{'id': name} for name in ('go', 'pdtm', 'go-tools', 'pipx')]
        with mock.patch('local_tool_job.inventory', return_value=extra), mock.patch('pc_packages.shutil.which', return_value='/usr/bin/package'), mock.patch('pc_packages.manager_inventory', side_effect=lambda manager: {'id': manager, 'count': 2}):
            data = packages.inventory()
        self.assertEqual([entry['id'] for entry in data['managers']], ['apt', 'snap', 'flatpak', 'go', 'pdtm', 'go-tools', 'pipx'])

    def test_manager_error_isolated_and_unknown_manager_rejected(self):
        with mock.patch('pc_packages.manager_binary', return_value='/usr/bin/snap'), mock.patch('pc_packages.snap_inventory', side_effect=subprocess.TimeoutExpired('snap', 10)):
            data = packages.manager_inventory('snap')
        self.assertEqual(data['state'], 'error')
        self.assertIsNone(data['count'])
        with self.assertRaises(RuntimeError):
            packages.manager_binary('apt; reboot')

    def test_declined_terminal_confirmation_runs_nothing(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        with tempfile.TemporaryDirectory(prefix='packages-', dir=directory) as temporary:
            result = Path(temporary) / 'result.json'
            with mock.patch('pc_packages.manager_binary', return_value='/usr/bin/snap'), mock.patch('builtins.input', return_value='no'), mock.patch('builtins.print'), mock.patch('pc_packages.subprocess.run') as run:
                self.assertEqual(packages.interactive('upgrade', result, 'snap'), 1)
                run.assert_not_called()
            self.assertEqual(json.loads(result.read_text())['completed_steps'], [])

    def test_snap_terminal_command_is_fixed_and_explicitly_confirmed(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        with tempfile.TemporaryDirectory(prefix='packages-', dir=directory) as temporary:
            result = Path(temporary) / 'result.json'
            with mock.patch('pc_packages.manager_binary', return_value='/usr/bin/snap'), mock.patch('builtins.input', return_value='yes'), mock.patch('builtins.print'), mock.patch('pc_packages.subprocess.run', return_value=mock.Mock(returncode=0)) as run:
                self.assertEqual(packages.interactive('upgrade', result, 'snap'), 0)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo', '/usr/bin/snap', 'refresh'])
            self.assertEqual(json.loads(result.read_text())['completed_steps'][0]['manager'], 'snap')

    def test_unreadable_terminal_confirmation_runs_no_package_commands(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        for error in [UnicodeDecodeError('utf-8', b'ye\xd1s', 2, 3, 'invalid continuation byte'), EOFError()]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory(prefix='packages-', dir=directory) as temporary:
                result = Path(temporary) / 'result.json'
                with mock.patch('pc_packages.manager_binary', return_value='/usr/bin/snap'), \
                        mock.patch('builtins.input', side_effect=error), mock.patch('builtins.print'), \
                        mock.patch('pc_packages.subprocess.run') as run:
                    self.assertEqual(packages.interactive('upgrade', result, 'snap'), 1)
                    run.assert_not_called()
                saved = json.loads(result.read_text())
                self.assertEqual(saved['state'], 'error')
                self.assertEqual(saved['completed_steps'], [])
                self.assertIn('Cancelled', saved['message'])
                self.assertNotIn('codec', saved['message'])


if __name__ == '__main__':
    unittest.main()
