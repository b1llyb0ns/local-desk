import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tool_packages as tools


class ToolPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).resolve().parents[1] / 'tmp'
        temporary.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='packages-', dir=temporary)
        self.home = Path(self.tmp.name)
        self.bin = self.home / 'go/bin'
        self.bin.mkdir(parents=True)
        self.original = self.bin / 'gau'
        self.original.write_bytes(b'previous binary')
        self.original.chmod(0o755)
        self.path_patch = mock.patch('tool_packages._active_path', return_value=None)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def metadata(self, path, module):
        return {'module': module, 'version': 'v2.2.4' if Path(path) == self.original else 'v2.2.5',
                'custom': False, 'replaced': False, 'path': tools.MODULES[Path(path).name][1]}

    def pipx_environment(self, spec='wappalyzer'):
        venv = self.home / '.local/share/pipx/venvs/wappalyzer'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/wappalyzer').write_bytes(b'previous application')
        metadata = {'main_package': {'package': 'wappalyzer', 'package_or_url': spec, 'package_version': '2.0.2',
                                     'apps': ['wappalyzer'], 'pip_args': [], 'pinned': False}, 'injected_packages': {}}
        (venv / 'pipx_metadata.json').write_text(json.dumps(metadata))
        (self.home / '.local/bin').mkdir(parents=True)
        (self.home / '.local/bin/wappalyzer').symlink_to(venv / 'bin/wappalyzer')
        return venv

    def test_parse_go_module_linker_version_and_custom_replacements(self):
        module = tools.MODULES['httpx'][0]
        data = tools.parse_go_metadata('path command-line-arguments\ndep ' + module + ' (devel)\nbuild -ldflags="-s -X main.version=1.10.0 -X main.commit=abc"', module)
        self.assertEqual(data['version'], '(devel)')
        self.assertEqual(data['linker_version'], '1.10.0')
        self.assertFalse(data['custom'])
        self.assertTrue(tools.parse_go_metadata('mod github.com/example/httpx v1.0.0', module)['custom'])
        self.assertTrue(tools.parse_go_metadata('=> ../private-copy', module)['replaced'])
        self.assertTrue(tools.parse_go_metadata('build vcs.modified=true', module)['custom'])

    def test_local_inventory_never_fetches_metadata_or_runs_tools(self):
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._fetch_json') as fetch, mock.patch('tool_packages._run_install') as install:
            data = tools.inventory(self.home)
        self.assertEqual([manager['id'] for manager in data], ['go-tools'])
        self.assertEqual(data[0]['installed_count'], 1)
        self.assertIsNone(data[0]['count'])
        self.assertEqual(data[0]['packages'][0]['path'], str(self.original))
        fetch.assert_not_called()
        install.assert_not_called()

    def test_go_metadata_command_only_invokes_go_reader(self):
        output = 'path github.com/lc/gau/v2/cmd/gau\nmod github.com/lc/gau/v2 v2.2.4'
        with mock.patch('tool_packages._go_binary', return_value='/usr/local/go/bin/go'), mock.patch('tool_packages.subprocess.run', return_value=mock.Mock(returncode=0, stdout=output)) as run:
            tools._metadata(self.original, tools.MODULES['gau'][0])
        self.assertEqual(run.call_args.args[0], ['/usr/local/go/bin/go', 'version', '-m', str(self.original)])
        self.assertEqual(run.call_args.kwargs['env']['GOTOOLCHAIN'], 'local')

    def test_known_nuclei_devel_requires_explicit_unknown_version_confirmation(self):
        pdtm = self.home / '.pdtm/go/bin'
        pdtm.mkdir(parents=True)
        (pdtm / 'nuclei').write_bytes(b'installed binary')
        metadata = {'module': tools.MODULES['nuclei'][0], 'version': '(devel)', 'custom': False,
                    'replaced': False, 'path': 'command-line-arguments'}
        with mock.patch('tool_packages._metadata', return_value=metadata), mock.patch('tool_packages._update_go', return_value={'state': 'done'}) as install:
            row = tools.manager_inventory('pdtm', self.home)['packages'][0]
            self.assertTrue(row['version_unknown'])
            self.assertTrue(row['updatable'])
            with self.assertRaisesRegex(RuntimeError, 'Explicitly confirm'):
                tools.update_item('pdtm', 'nuclei', 'v3.5.0', self.home)
            install.assert_not_called()
            self.assertEqual(tools.update_item('pdtm', 'nuclei', 'v3.5.0', self.home, True)['state'], 'done')

    def test_development_custom_and_unknown_tools_remain_visible_but_disabled(self):
        (self.bin / 'ffuf').write_bytes(b'binary')
        (self.bin / 'local-helper').write_bytes(b'binary')
        def metadata(path, module):
            value = self.metadata(path, module)
            value['version'] = 'v2.0.0-20260829133750-0f654620eef6'
            return value
        with mock.patch('tool_packages._metadata', side_effect=metadata), mock.patch('tool_packages._fetch_json') as fetch:
            rows = tools.manager_inventory('go-tools', self.home, True)['packages']
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(not row['updatable'] for row in rows))
        fetch.assert_not_called()

    def test_duplicate_paths_are_not_silently_replaced(self):
        pdtm = self.home / '.pdtm/go/bin'
        pdtm.mkdir(parents=True)
        (pdtm / 'gau').write_bytes(b'other binary')
        with mock.patch('tool_packages._metadata', side_effect=self.metadata):
            data = tools.inventory(self.home)
        rows = [row for manager in data for row in manager['packages']]
        self.assertEqual(len(set(row['id'] for row in rows)), 2)
        self.assertTrue(all(not row['updatable'] for row in rows))
        self.assertTrue(all('Duplicate' in row['reason'] for row in rows))

    def test_shadowing_and_symlink_layout_disable_updates(self):
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._active_path', return_value='/usr/bin/gau'):
            row = tools.inventory(self.home)[0]['packages'][0]
        self.assertFalse(row['updatable'])
        self.assertIn('PATH', row['reason'])
        (self.bin / 'katana').symlink_to(self.original)
        with mock.patch('tool_packages._metadata', side_effect=self.metadata):
            row = next(row for row in tools.inventory(self.home)[0]['packages'] if row['name'] == 'katana')
        self.assertFalse(row['updatable'])

    def test_explicit_check_uses_official_exact_module_only(self):
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._fetch_json', return_value={'Version': 'v2.2.5'}) as fetch:
            data = tools.manager_inventory('go-tools', self.home, True)
        self.assertEqual(fetch.call_args.args[0], 'https://proxy.golang.org/github.com/lc/gau/v2/@latest')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['packages'][0]['candidate'], 'v2.2.5')
        self.assertEqual(data['actions'], ['check'])

    def test_partial_check_failure_is_not_reported_as_all_up_to_date(self):
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._fetch_json', side_effect=OSError('offline')):
            data = tools.manager_inventory('go-tools', self.home, True)
        self.assertEqual(data['state'], 'partial')
        self.assertIn('failed', data['error'])
        self.assertIsNone(data['packages'][0]['candidate'])
        self.assertIn('check_error', data['packages'][0])

    def test_metadata_hosts_and_nonstable_versions_rejected(self):
        for url in ['http://pypi.org/a', 'https://pypi.org.evil.example/a', 'https://user@pypi.org/a', 'https://example.com/a']:
            with self.assertRaises(RuntimeError):
                tools._official_url(url)
        with mock.patch('tool_packages._fetch_json', return_value={'Version': 'v2.3.0-rc1'}):
            with self.assertRaises(RuntimeError):
                tools._latest({'module': 'github.com/lc/gau/v2'}, 'go-tools')

    def test_failed_go_build_preserves_original(self):
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._run_install', return_value=1):
            with self.assertRaisesRegex(RuntimeError, 'not replaced'):
                tools.update_item('go-tools', 'gau', 'v2.2.5', self.home)
        self.assertEqual(self.original.read_bytes(), b'previous binary')
        self.assertEqual(list(self.bin.iterdir()), [self.original])

    def test_go_success_is_pinned_verified_atomic_and_retains_backup(self):
        def build(command, **kwargs):
            self.assertEqual(command[-1], 'github.com/lc/gau/v2/cmd/gau@v2.2.5')
            self.assertEqual(kwargs['env']['GOTOOLCHAIN'], 'local')
            self.assertEqual(kwargs['env']['GOPROXY'], 'https://proxy.golang.org')
            self.assertNotEqual(kwargs['env']['GOBIN'], str(self.bin))
            (Path(kwargs['env']['GOBIN']) / 'gau').write_bytes(b'updated binary')
            return 0
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._run_install', side_effect=build):
            result = tools.update_item('go-tools', 'gau', 'v2.2.5', self.home)
        self.assertEqual(self.original.read_bytes(), b'updated binary')
        self.assertEqual(Path(result['backup']).read_bytes(), b'previous binary')
        self.assertEqual(result['path'], str(self.original))

    def test_wrong_built_version_or_concurrent_edit_preserves_installed_binary(self):
        def build(command, **kwargs):
            (Path(kwargs['env']['GOBIN']) / 'gau').write_bytes(b'wrong binary')
            return 0
        def metadata(path, module):
            result = self.metadata(path, module)
            result['version'] = 'v2.2.4'
            return result
        with mock.patch('tool_packages._metadata', side_effect=metadata), mock.patch('tool_packages._run_install', side_effect=build):
            with self.assertRaisesRegex(RuntimeError, 'did not match'):
                tools.update_item('go-tools', 'gau', 'v2.2.5', self.home)
        self.assertEqual(self.original.read_bytes(), b'previous binary')
        def concurrent(command, **kwargs):
            build(command, **kwargs)
            self.original.write_bytes(b'user replacement')
            return 0
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._run_install', side_effect=concurrent):
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                tools.update_item('go-tools', 'gau', 'v2.2.5', self.home)
        self.assertEqual(self.original.read_bytes(), b'user replacement')

    def test_unknown_item_injection_and_downgrade_cannot_launch_installer(self):
        with mock.patch('tool_packages._metadata', side_effect=self.metadata), mock.patch('tool_packages._run_install') as run:
            for manager, item, version in [('go-tools', '../gau', 'v2.2.5'), ('go-tools', 'gau', 'v2.2.3'),
                                           ('go-tools', 'gau', 'v2.2.5;id'), ('go-tools;id', 'gau', 'v2.2.5')]:
                with self.assertRaises(RuntimeError):
                    tools.update_item(manager, item, version, self.home)
            run.assert_not_called()

    def test_pipx_metadata_detects_plain_and_preserves_version_pin(self):
        self.pipx_environment('wappalyzer==2.0.2')
        with mock.patch('tool_packages._fetch_json') as fetch:
            row = tools.manager_inventory('pipx', self.home, True)['packages'][0]
        self.assertFalse(row['updatable'])
        self.assertIn('pinned', row['reason'])
        fetch.assert_not_called()

    def test_pipx_local_source_is_visible_and_not_updated(self):
        self.pipx_environment('/work/local/wappalyzer')
        with mock.patch('tool_packages._fetch_json') as fetch, mock.patch('tool_packages._run_install') as run:
            row = tools.manager_inventory('pipx', self.home, True)['packages'][0]
            with self.assertRaises(RuntimeError):
                tools.update_item('pipx', 'wappalyzer', '2.0.3', self.home)
        self.assertFalse(row['updatable'])
        fetch.assert_not_called()
        run.assert_not_called()

    def test_pipx_failed_update_restores_environment_and_launchers(self):
        venv = self.pipx_environment()
        def fail(command, **kwargs):
            self.assertIn('upgrade', command)
            self.assertNotIn('--force', command)
            self.assertNotIn('--pip-args', command)
            self.assertNotIn('--index-url', command)
            constraint = kwargs['env']['PIP_CONSTRAINT']
            self.assertEqual(Path(constraint).read_text(), 'wappalyzer==2.0.3\n')
            (venv / 'bin/wappalyzer').write_bytes(b'partial update')
            (self.home / '.local/bin/normalizer').symlink_to(venv / 'bin/normalizer')
            return 1
        with mock.patch('tool_packages._run_install', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'restored'):
                tools.update_item('pipx', 'wappalyzer', '2.0.3', self.home)
        self.assertEqual((venv / 'bin/wappalyzer').read_bytes(), b'previous application')
        self.assertTrue((self.home / '.local/bin/wappalyzer').is_symlink())
        self.assertFalse((self.home / '.local/bin/normalizer').is_symlink())

    def test_pipx_success_checks_selected_version_and_keeps_backup(self):
        venv = self.pipx_environment()
        def succeed(command, **kwargs):
            metadata = json.loads((venv / 'pipx_metadata.json').read_text())
            metadata['main_package']['package_version'] = '2.0.3'
            (venv / 'pipx_metadata.json').write_text(json.dumps(metadata))
            (venv / 'bin/wappalyzer').write_bytes(b'updated application')
            self.assertEqual(command[:3], ['/usr/bin/pipx', 'upgrade', 'wappalyzer'])
            self.assertEqual(kwargs['env']['PIP_INDEX_URL'], 'https://pypi.org/simple')
            return 0
        with mock.patch('tool_packages._run_install', side_effect=succeed):
            result = tools.update_item('pipx', 'wappalyzer', '2.0.3', self.home)
        self.assertEqual((venv / 'bin/wappalyzer').read_bytes(), b'updated application')
        self.assertEqual((Path(result['backup']) / 'venv/bin/wappalyzer').read_bytes(), b'previous application')
        metadata = json.loads((venv / 'pipx_metadata.json').read_text())
        self.assertEqual(metadata['main_package']['pip_args'], [])
        self.assertTrue(tools.manager_inventory('pipx', self.home)['packages'][0]['updatable'])

    def test_pipx_failure_restores_backup_even_when_environment_was_removed(self):
        venv = self.pipx_environment()
        def remove_environment(command, **kwargs):
            shutil.rmtree(venv)
            return 1
        with mock.patch('tool_packages._run_install', side_effect=remove_environment):
            with self.assertRaisesRegex(RuntimeError, 'restored'):
                tools.update_item('pipx', 'wappalyzer', '2.0.3', self.home)
        self.assertEqual((venv / 'bin/wappalyzer').read_bytes(), b'previous application')
        self.assertTrue((self.home / '.local/bin/wappalyzer').resolve().is_file())
        self.assertTrue(tools.manager_inventory('pipx', self.home)['packages'][0]['updatable'])

    def test_installer_cancellation_stops_child_process_group(self):
        process = mock.Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired('go', 1200), 0]
        with mock.patch('tool_packages.subprocess.Popen', return_value=process) as popen, mock.patch('tool_packages.os.killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                tools._run_install(['/usr/local/go/bin/go', 'install', 'github.com/lc/gau/v2/cmd/gau@v2.2.5'])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        kill.assert_called_once_with(12345, tools.signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
