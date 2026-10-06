"""Preflight failures must never look like a partially completed SSH migration."""
import unittest
from pathlib import Path
from unittest import mock

import ssh_engine


class PreflightTests(unittest.TestCase):
    def engine(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.public_key = 'ssh-ed25519 QUJD local-key'
        engine.probe = mock.Mock()
        engine.controller = mock.Mock()
        return engine

    def test_initial_timeout_reports_no_changes_and_never_runs_controller(self):
        engine = self.engine()
        engine.probe.side_effect = ssh_engine.SSHError('Server response timed out.', 'timeout')
        credentials = {'mode': 'password', 'user': 'root', 'password': 'temporary-login'}
        with self.assertRaisesRegex(ssh_engine.SSHError, 'before key installation') as caught:
            engine.onboard({}, credentials, mock.Mock())
        self.assertEqual(caught.exception.kind, 'timeout')
        self.assertIn('not changed by this attempt', str(caught.exception))
        engine.controller.assert_not_called()
        self.assertEqual(credentials, {})

    def test_rejected_password_does_not_install_or_disable_anything(self):
        engine = self.engine()
        engine.probe.side_effect = [ssh_engine.SSHError('Key rejected.', 'credentials_needed'),
                                   ssh_engine.SSHError('Password rejected.', 'credentials_needed')]
        credentials = {'mode': 'password', 'user': 'root', 'password': 'temporary-login'}
        with self.assertRaisesRegex(ssh_engine.SSHError, 'not changed by this attempt') as caught:
            engine.onboard({}, credentials, mock.Mock())
        self.assertEqual(caught.exception.kind, 'credentials_needed')
        self.assertNotIn('temporary-login', str(caught.exception))
        self.assertEqual(engine.probe.call_count, 2)
        engine.controller.assert_not_called()
        self.assertEqual(credentials, {})

    def test_server_alive_timeout_is_classified(self):
        error = ssh_engine.explain_error('Timeout, server 192.0.2.8 not responding.\r\n')
        self.assertEqual(error.kind, 'timeout')
        self.assertEqual(error.retry_hint, 'ipqos_ef')

    def test_connection_timeout_does_not_request_ipqos_retry(self):
        error = ssh_engine.explain_error('ssh: connect to host 192.0.2.8 port 22: Connection timed out\r\n')
        self.assertEqual(error.kind, 'timeout')
        self.assertEqual(error.retry_hint, '')

    def test_login_keepalive_timeout_retries_once_and_learns_transport(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(side_effect=[
            (255, '', 'Timeout, server 192.0.2.8 not responding.'),
            (0, '{"uid":0,"sudo":true}', '')])
        record = {'host': '192.0.2.8'}
        auth = {'mode': 'password', 'password': 'temporary-login'}
        self.assertEqual(engine.probe(record, auth=auth, user='root', accept_new=True)['uid'], 0)
        self.assertEqual(record['metrics_ipqos'], 'ef')
        self.assertEqual(engine.run.call_count, 2)
        retry = engine.run.call_args.kwargs
        self.assertEqual(retry['ipqos'], 'ef')
        self.assertIs(retry['auth'], auth)
        self.assertEqual(retry['user'], 'root')
        self.assertTrue(retry['accept_new'])

    def test_login_does_not_retry_host_keys_credentials_or_connect_timeout(self):
        for error in ('REMOTE HOST IDENTIFICATION HAS CHANGED!',
                      'Host key verification failed.', 'Permission denied (password).',
                      'connect to host 192.0.2.8 port 22: Connection timed out'):
            with self.subTest(error=error):
                engine = object.__new__(ssh_engine.SSHEngine)
                engine.run = mock.Mock(return_value=(255, '', error))
                with self.assertRaises(ssh_engine.SSHError):
                    engine.probe({'host': '192.0.2.8'})
                self.assertEqual(engine.run.call_count, 1)

    def test_failed_compatibility_retry_does_not_save_transport(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(return_value=(255, '', 'Timeout, server 192.0.2.8 not responding.'))
        record = {'host': '192.0.2.8'}
        with self.assertRaises(ssh_engine.SSHError):
            engine.probe(record)
        self.assertEqual(engine.run.call_count, 2)
        self.assertNotIn('metrics_ipqos', record)
        record['metrics_ipqos'] = 'ef'
        engine.run.reset_mock()
        with self.assertRaises(ssh_engine.SSHError):
            engine.probe(record)
        self.assertEqual(engine.run.call_count, 1)

    def test_setup_connection_reuses_saved_transport_without_replaying_command(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.key = Path('/keys/id_ed25519')
        engine.known_hosts = Path('/keys/known_hosts')
        with mock.patch.object(ssh_engine, 'limited_run', return_value=(255, '', 'Connection timed out')) as execute:
            result = engine.run({'host': '192.0.2.8', 'metrics_ipqos': 'ef'}, 'true')
        self.assertEqual(result[0], 255)
        self.assertEqual(execute.call_count, 1)
        self.assertIn('IPQoS=ef', execute.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
