import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go_toolchain as go


def metadata(version='go1.27.1', size=100, digest='a' * 64):
    return {'version': version, 'stable': True, 'files': [
        {'filename': version + '.linux-amd64.tar.gz', 'version': version, 'os': 'linux',
         'arch': 'amd64', 'kind': 'archive', 'size': size, 'sha256': digest},
    ]}


def elf(machine=62):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    header[16:18] = (2).to_bytes(2, 'little')
    header[18:20] = machine.to_bytes(2, 'little')
    return bytes(header)


class Response(io.BytesIO):
    def __init__(self, data, url=go.METADATA_URL, size=None):
        super().__init__(data)
        self.url = url
        self.headers = {} if size is None else {'Content-Length': str(size)}

    def geturl(self):
        return self.url


class MetadataTests(unittest.TestCase):
    def test_latest_stable_exact_linux_amd64_metadata_is_selected(self):
        preview = metadata('go1.28rc1')
        preview['stable'] = False
        selected = go.select_release([metadata('go1.26.7'), preview, metadata()])
        self.assertEqual(selected['version'], 'go1.27.1')
        self.assertEqual(selected['url'], 'https://go.dev/dl/go1.27.1.linux-amd64.tar.gz')
        self.assertEqual(selected['sha256'], 'a' * 64)
        self.assertGreater(go.version_tuple('go1.27.1'), go.version_tuple('go1.26.10'))

    def test_prerelease_and_custom_versions_are_rejected(self):
        for version in ['go1.28rc1', 'go1.28beta1', 'go1.27', 'go1.27.1-custom', '../../go1.27.1', 'go1.027.1', 'go1.27.1\n']:
            with self.subTest(version=version), self.assertRaises(RuntimeError):
                go.version_tuple(version)
        self.assertEqual(go.version_tuple('go1.20'), (1, 20, 0))

    def test_missing_or_invalid_checksum_size_and_filename_are_rejected(self):
        for field, value in [('sha256', None), ('sha256', 'bad'), ('size', True), ('size', go.MAX_ARCHIVE + 1), ('filename', '../../go.tar.gz'), ('version', 'go1.27.0')]:
            data = metadata()
            data['files'][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                go.select_release([data])
        duplicated = metadata()
        duplicated['files'] *= 2
        with self.assertRaises(RuntimeError):
            go.select_release([duplicated])

    def test_official_urls_and_redirects_are_exact(self):
        filename = 'go1.27.1.linux-amd64.tar.gz'
        self.assertEqual(go._allowed_url('https://dl.google.com/go/' + filename, filename), 'https://dl.google.com/go/' + filename)
        for url in ['http://go.dev/dl/' + filename, 'https://go.dev.example/dl/' + filename,
                    'https://go.dev@other.example/dl/' + filename, 'https://dl.google.com/other/' + filename,
                    'https://go.dev/dl/' + filename + '?source=other', go.METADATA_URL]:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                go._allowed_url(url, filename)
        with self.assertRaises(RuntimeError):
            go._Redirect(filename).redirect_request(None, None, 302, '', {}, 'https://other.example/archive')

    def test_metadata_read_is_bounded(self):
        with mock.patch.object(go, '_supported_platform'), mock.patch.object(go, '_open', return_value=Response(json.dumps([metadata()]).encode())):
            self.assertEqual(go.check_release()['version'], 'go1.27.1')
        with mock.patch.object(go, '_supported_platform'), mock.patch.object(go, 'MAX_METADATA', 20), \
                mock.patch.object(go, '_open', return_value=Response(b' ' * 21)), self.assertRaisesRegex(RuntimeError, 'too large'):
            go.check_release()

    def test_inventory_forces_local_toolchain_and_never_fetches_releases(self):
        with mock.patch.object(go, '_standard_version', return_value='go1.26.0'), \
                mock.patch.object(go.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='go version go1.26.0 linux/amd64\n')) as run, \
                mock.patch.object(go, '_open') as opened:
            result = go.local_inventory()
        self.assertTrue(result['managed'])
        self.assertEqual(result['packages'][0]['installed'], 'go1.26.0')
        self.assertTrue(result['packages'][0]['updatable'])
        self.assertIsNone(result['packages'][0]['candidate'])
        self.assertEqual(run.call_args.args[0], ['/usr/local/go/bin/go', 'version'])
        self.assertEqual(run.call_args.kwargs['env']['GOTOOLCHAIN'], 'local')
        self.assertEqual(run.call_args.kwargs['env']['GOENV'], 'off')
        self.assertEqual(run.call_args.kwargs['cwd'], '/')
        opened.assert_not_called()

    def test_newer_installed_go_has_no_downgrade_candidate(self):
        record = {'managed': True, 'installed_version': 'go1.28.0', 'packages': [{'installed': 'go1.28.0'}]}
        with mock.patch.object(go, 'local_inventory', return_value=record), \
                mock.patch.object(go, 'check_release', return_value=go.select_release([metadata()])):
            result = go.check_inventory()
        self.assertEqual(result['count'], 0)
        self.assertTrue(result['packages'][0]['updatable'])
        self.assertIsNone(result['packages'][0]['candidate'])
        self.assertEqual(result['packages'][0]['latest'], 'go1.27.1')


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='toolchain-', dir=directory)
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self, extra=None, version='go1.27.1', machine=62):
        output = io.BytesIO()
        entries = [('go/VERSION', version.encode() + b'\ntime 2026-09-01T00:00:00Z\n', 0o644),
                   ('go/bin/go', elf(machine), 0o755), ('go/bin/gofmt', elf(machine), 0o755),
                   ('go/pkg/tool/linux_amd64/compile', elf(machine), 0o755),
                   ('go/src/runtime/runtime2.go', b'package runtime\n', 0o644)]
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, data, mode in entries:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                archive.addfile(info, io.BytesIO(data))
            if extra:
                info, data = extra
                archive.addfile(info, io.BytesIO(data) if data else None)
        payload = output.getvalue()
        path = self.directory / 'release.tar.gz'
        path.write_bytes(payload)
        candidate = go.select_release([metadata(size=len(payload), digest=hashlib.sha256(payload).hexdigest())])
        destination = self.directory / 'extract'
        destination.mkdir(exist_ok=True)
        return path, destination, candidate

    def test_verified_archive_extracts_without_running_any_binary(self):
        path, destination, candidate = self.archive()
        previous_mask = os.umask(0o077)
        try:
            with mock.patch.object(go.subprocess, 'run') as run:
                root = go.safe_extract(path, destination, candidate)
            run.assert_not_called()
        finally:
            os.umask(previous_mask)
        self.assertEqual((root / 'VERSION').read_text().splitlines()[0], 'go1.27.1')
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        self.assertEqual((root / 'bin/go').stat().st_mode & 0o777, 0o755)

    def test_checksum_failure_precedes_extraction(self):
        path, destination, candidate = self.archive()
        candidate['sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            go.safe_extract(path, destination, candidate)
        self.assertEqual(list(destination.iterdir()), [])

    def test_unsafe_paths_links_devices_and_duplicates_are_rejected(self):
        cases = [('../outside', tarfile.REGTYPE), ('/outside', tarfile.REGTYPE),
                 ('go/../../outside', tarfile.REGTYPE), ('other/file', tarfile.REGTYPE),
                 ('go/link', tarfile.SYMTYPE), ('go/hard', tarfile.LNKTYPE),
                 ('go/device', tarfile.CHRTYPE), ('go/VERSION', tarfile.REGTYPE)]
        for index, (name, kind) in enumerate(cases):
            with self.subTest(name=name):
                extra = tarfile.TarInfo(name)
                extra.type, extra.linkname = kind, '/outside'
                path, _, candidate = self.archive(extra=(extra, b''))
                destination = self.directory / ('extract-' + str(index))
                destination.mkdir()
                with self.assertRaises(RuntimeError):
                    go.safe_extract(path, destination, candidate)
        self.assertFalse((self.directory / 'outside').exists())

    def test_wrong_version_and_architecture_are_rejected(self):
        for index, (version, machine) in enumerate([('go1.27.0', 62), ('go1.27.1', 183)]):
            path, _, candidate = self.archive(version=version, machine=machine)
            destination = self.directory / ('wrong-' + str(index))
            destination.mkdir()
            with self.assertRaises(RuntimeError):
                go.safe_extract(path, destination, candidate)

    def test_expanded_archive_size_is_bounded(self):
        path, destination, candidate = self.archive()
        with mock.patch.object(go, 'MAX_EXPANDED', 10), self.assertRaisesRegex(RuntimeError, 'expanded'):
            go.safe_extract(path, destination, candidate)

    def test_download_checks_exact_size_and_hash(self):
        path, _, candidate = self.archive()
        payload = path.read_bytes()
        downloaded = self.directory / 'downloaded.tar.gz'
        with mock.patch.object(go, '_open', return_value=Response(payload, candidate['url'], len(payload))):
            go._download(candidate, downloaded)
        self.assertEqual(downloaded.read_bytes(), payload)
        with mock.patch.object(go, '_open', return_value=Response(payload + b'x', candidate['url'])), self.assertRaisesRegex(RuntimeError, 'expected size'):
            go._download(candidate, self.directory / 'large.tar.gz')

    def test_existing_install_symlink_is_rejected_before_execution(self):
        regular = self.directory / 'VERSION'
        regular.write_text('go1.26.0\n')
        link = self.directory / 'link'
        link.symlink_to(regular)
        with self.assertRaisesRegex(RuntimeError, 'symlinked'):
            go._trusted_node(link)

    def test_atomic_replacement_preserves_previous_tree_in_sibling_backup(self):
        installed, ready = self.directory / 'go', self.directory / 'ready'
        installed.mkdir(); ready.mkdir()
        (installed / 'VERSION').write_text('go1.26.0')
        (ready / 'VERSION').write_text('go1.27.1')
        with mock.patch.object(go, 'INSTALL_ROOT', installed), mock.patch.object(go, '_standard_version', return_value='go1.26.0'), mock.patch.object(go, '_running_toolchain', return_value=[]):
            backup = go._replace_ready(ready, 'go1.26.0')
        self.assertEqual((installed / 'VERSION').read_text(), 'go1.27.1')
        self.assertEqual((backup / 'VERSION').read_text(), 'go1.26.0')
        self.assertEqual(backup.parent, installed.parent)

    def test_failed_atomic_exchange_keeps_working_go_and_prepared_candidate(self):
        installed, ready = self.directory / 'go', self.directory / 'ready'
        installed.mkdir(); ready.mkdir()
        (installed / 'VERSION').write_text('go1.26.0')
        (ready / 'VERSION').write_text('go1.27.1')
        with mock.patch.object(go, 'INSTALL_ROOT', installed), mock.patch.object(go, '_standard_version', return_value='go1.26.0'), mock.patch.object(go, '_running_toolchain', return_value=[]), mock.patch.object(go, '_exchange', side_effect=OSError('Unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'not replaced'):
                go._replace_ready(ready, 'go1.26.0')
        self.assertEqual((installed / 'VERSION').read_text(), 'go1.26.0')
        retained = next(self.directory.glob('go.backup-*'))
        self.assertEqual((retained / 'VERSION').read_text(), 'go1.27.1')

    def test_full_install_flow_uses_fresh_metadata_and_retains_old_tree_after_cleanup(self):
        archive, _, candidate = self.archive()
        payload = archive.read_bytes()
        installed = self.directory / 'go'
        installed.mkdir()
        (installed / 'VERSION').write_text('go1.26.0')
        original_fstat = os.fstat
        def root_stat(fd):
            values = list(original_fstat(fd))
            values[4] = 0
            return os.stat_result(values)
        def download(release, target):
            self.assertEqual(release['sha256'], candidate['sha256'])
            target.write_bytes(payload)
        with mock.patch.object(go, 'INSTALL_ROOT', installed), mock.patch.object(go.os, 'geteuid', return_value=0), \
                mock.patch.object(go.sys.stdin, 'isatty', return_value=True), mock.patch.object(go.os, 'fstat', side_effect=root_stat), \
                mock.patch.object(go, '_standard_version', return_value='go1.26.0'), mock.patch.object(go, '_running_toolchain', return_value=[]), \
                mock.patch.object(go, 'check_release', return_value=candidate) as release_check, mock.patch.object(go, '_download', side_effect=download), \
                mock.patch('builtins.input', return_value='yes'), mock.patch('builtins.print'), mock.patch.object(go.subprocess, 'run') as run:
            result = go._root_install('go1.27.1')
        release_check.assert_called_once_with()
        run.assert_not_called()
        self.assertEqual(result['state'], 'done')
        self.assertEqual((installed / 'VERSION').read_text().splitlines()[0], 'go1.27.1')
        self.assertEqual((Path(result['backup']) / 'VERSION').read_text(), 'go1.26.0')
        self.assertEqual(list(self.directory.glob('.go-stage-*')), [])


class InstallGuardTests(unittest.TestCase):
    def test_noninteractive_or_unprivileged_helper_does_not_fetch_or_install(self):
        with mock.patch.object(go.os, 'geteuid', return_value=1000), mock.patch.object(go, 'check_release') as check:
            with self.assertRaisesRegex(RuntimeError, 'interactive terminal'):
                go._root_install('go1.27.1')
            check.assert_not_called()

    def test_downgrade_and_changed_release_are_rejected_before_download(self):
        for previous, candidate in [('go1.28.0', metadata()), ('go1.26.0', metadata('go1.27.2'))]:
            with self.subTest(previous=previous), mock.patch.object(go.os, 'geteuid', return_value=0), mock.patch.object(go.sys.stdin, 'isatty', return_value=True), mock.patch.object(go, '_standard_version', return_value=previous), mock.patch.object(go, 'check_release', return_value=go.select_release([candidate])), mock.patch.object(go, '_download') as download:
                with self.assertRaises(RuntimeError):
                    go._root_install('go1.27.1')
                download.assert_not_called()

    def test_root_cancellation_and_active_toolchain_leave_installation_unchanged(self):
        for running, reply in [([], 'no'), ([1234], 'yes')]:
            with self.subTest(running=running), mock.patch.object(go.os, 'geteuid', return_value=0), mock.patch.object(go.sys.stdin, 'isatty', return_value=True), mock.patch.object(go, '_standard_version', return_value='go1.26.0'), mock.patch.object(go, 'check_release', return_value=go.select_release([metadata()])), mock.patch.object(go, '_running_toolchain', return_value=running), mock.patch('builtins.input', return_value=reply), mock.patch('builtins.print'), mock.patch.object(go, '_download') as download:
                with self.assertRaises(RuntimeError):
                    go._root_install('go1.27.1')
                download.assert_not_called()

    def test_terminal_wrapper_has_fixed_isolated_command_and_propagates_cancellation(self):
        with mock.patch.object(go.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='')) as run:
            with self.assertRaisesRegex(RuntimeError, 'cancelled or failed'):
                go.interactive_update('go1.27.1')
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['/usr/bin/sudo', '--', '/usr/bin/env', '-i'])
        self.assertIn('-I', command)
        self.assertEqual(command[-3:], ['install', '--version', 'go1.27.1'])
        with mock.patch.object(go.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps({'state': 'done', 'version': 'go1.27.1', 'backup': '/usr/local/go.backup-go1.26.0'}))):
            self.assertEqual(go.interactive_update('go1.27.1')['state'], 'done')


if __name__ == '__main__':
    unittest.main()
