"""Check effective OpenSSH options without making network connections."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from ssh_engine import SSHError


@unittest.skipUnless(shutil.which('ssh'), 'OpenSSH client required')
class SSHExportTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='ssh-options-', dir=directory)
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home,
                                  import_existing=False, export=True)
        self.record = self.store.add({'name': 'Germany', 'alias': 'server-a',
                                      'host': '192.0.2.8'})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def options(self, destination):
        result = subprocess.run(['ssh', '-G', '-F', str(self.home / '.ssh/local-desk.conf'),
                                 destination], capture_output=True, text=True,
                                check=True, timeout=5)
        return dict(line.split(' ', 1) for line in result.stdout.splitlines())

    def assert_password_fallback(self):
        for destination in ('server-a', 'root@192.0.2.8'):
            with self.subTest(destination=destination):
                options = self.options(destination)
                self.assertEqual(options['hostname'], '192.0.2.8')
                self.assertEqual(options['passwordauthentication'], 'yes')
                self.assertEqual(options['kbdinteractiveauthentication'], 'yes')
                if 'preferredauthentications' in options:
                    self.assertIn('password', options['preferredauthentications'].split(','))
                self.assertEqual(options['stricthostkeychecking'], 'accept-new')
                self.assertEqual(options['identityfile'], '~/.ssh/id_ed25519')

    def test_pending_and_failed_setup_keep_password_fallback(self):
        self.assert_password_fallback()
        self.store.failure(self.record['id'], SSHError('Connection timed out.', 'timeout'))
        self.assert_password_fallback()

    def test_ready_or_reinstalled_server_does_not_disable_client_passwords(self):
        self.store.update(self.record['id'], {'state': 'ready'}, internal=True)
        self.store.export_connections()
        self.assert_password_fallback()
        self.store.failure(self.record['id'], SSHError('Key changed.', 'host_key_changed'))
        self.assert_password_fallback()

    def test_learned_transport_is_exported_and_reset_for_new_address(self):
        self.store.update(self.record['id'], {'metrics_ipqos': 'ef'}, internal=True)
        self.store.export_connections()
        for destination in ('server-a', 'root@192.0.2.8'):
            self.assertEqual(set(self.options(destination)['ipqos'].split()), {'ef'})
        self.store.update(self.record['id'], {'host': '192.0.2.9'})
        self.assertNotIn('IPQoS ef', (self.home / '.ssh/local-desk.conf').read_text())


if __name__ == '__main__':
    unittest.main()
