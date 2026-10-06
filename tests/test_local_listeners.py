import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_listeners as listeners


class ParseTests(unittest.TestCase):
    def test_exposure_classes_do_not_claim_internet_reachability(self):
        cases = {
            '127.0.0.1': 'local-only', '::1': 'local-only',
            '::ffff:127.0.0.1': 'local-only', '192.168.1.5': 'private-LAN',
            '10.0.0.1': 'private-LAN', '169.254.2.3': 'private-LAN',
            'fd00::1': 'private-LAN', 'fe80::1%eth0': 'private-LAN',
            '8.8.8.8': 'public-address', '2001:4860::8888': 'public-address',
            '0.0.0.0': 'wildcard', '::': 'wildcard', '*': 'wildcard',
            '0.0.0.0%eth0': 'wildcard', '192.0.2.5': 'special-use',
            '100.64.0.1': 'special-use', '224.0.0.1': 'special-use',
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                actual, reason = listeners.classify_exposure(address)
                self.assertEqual(actual, expected)
                self.assertTrue(reason)
                if expected != 'local-only':
                    self.assertIn('unknown', reason)
        rows, _ = listeners.parse_ss('tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*')
        self.assertEqual(rows[0]['exposure_class'], 'wildcard')
        self.assertEqual(rows[0]['internet_reachability'], 'not_checked')

    def test_ipv4_loopback_and_specific_bind(self):
        rows, skipped = listeners.parse_ss(
            'tcp LISTEN 0 128 127.0.0.1:8787 0.0.0.0:* users:(("python3",pid=123,fd=6))\n'
            'tcp LISTEN 0 4096 192.168.1.5:443 0.0.0.0:*\n'
        )
        self.assertEqual(skipped, 0)
        self.assertEqual(rows[1]['address'], '127.0.0.1')
        self.assertEqual(rows[1]['family'], 'ipv4')
        self.assertEqual(rows[1]['exposure'], 'loopback')
        self.assertEqual(rows[1]['processes'], [{'name': 'python3', 'pid': 123}])
        self.assertEqual(rows[0]['exposure'], 'specific_interface')

    def test_ipv6_loopback_wildcard_and_zone(self):
        rows, skipped = listeners.parse_ss(
            'tcp LISTEN 0 128 [::1]:631 [::]:*\n'
            'tcp LISTEN 0 128 [::]:443 [::]:*\n'
            'udp UNCONN 0 0 [fe80::1%eth0]:5353 [::]:*\n'
            'tcp LISTEN 0 128 ::1:25 :::*\n'
        )
        self.assertEqual(skipped, 0)
        by_port = {row['port']: row for row in rows}
        self.assertTrue(all(row['family'] == 'ipv6' for row in rows))
        self.assertEqual(by_port[631]['exposure'], 'loopback')
        self.assertEqual(by_port[443]['exposure'], 'all_interfaces')
        self.assertEqual(by_port[5353]['address'], 'fe80::1%eth0')
        self.assertEqual(by_port[5353]['exposure'], 'specific_interface')
        self.assertEqual(by_port[25]['address'], '::1')

    def test_mapped_loopback_and_scoped_wildcard(self):
        rows, skipped = listeners.parse_ss(
            'tcp LISTEN 0 128 [::ffff:127.0.0.1]:8080 [::]:*\n'
            'udp UNCONN 0 0 0.0.0.0%eth0:5353 0.0.0.0:*\n'
        )
        self.assertEqual(skipped, 0)
        self.assertEqual(rows[0]['exposure'], 'loopback')
        self.assertEqual(rows[1]['exposure'], 'specific_interface')

    def test_multiple_processes_deduplicate_fd_owners_but_not_sockets(self):
        line = ('tcp LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* '
                'users:(("worker",pid=300,fd=8),("worker",pid=300,fd=9),'
                '("main process",pid=200,fd=8))\n')
        rows, skipped = listeners.parse_ss(line + line)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['processes'], [
            {'name': 'main process', 'pid': 200}, {'name': 'worker', 'pid': 300},
        ])

    def test_wildcard_family_is_only_inferred_when_available(self):
        rows, skipped = listeners.parse_ss(
            'tcp LISTEN 0 128 *:8000 *:*\n'
            'tcp LISTEN 0 128 *:8001 [::]:*\n'
            'tcp6 LISTEN 0 128 *:8002 *:*\n'
            'udp UNCONN 0 0 0.0.0.0:8003 0.0.0.0:*\n'
        )
        self.assertEqual(skipped, 0)
        self.assertEqual([row['family'] for row in rows], ['unspecified', 'ipv6', 'ipv6', 'ipv4'])
        self.assertTrue(all(row['exposure'] == 'all_interfaces' for row in rows))

    def test_udp_and_hidden_process_information(self):
        rows, skipped = listeners.parse_ss('udp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:*\n')
        self.assertEqual(skipped, 0)
        self.assertEqual(rows[0]['protocol'], 'udp')
        self.assertEqual(rows[0]['state'], 'UNCONN')
        self.assertEqual(rows[0]['processes'], [])
        self.assertEqual(rows[0]['process_visibility'], 'unavailable')

    def test_malformed_rows_are_counted_and_unix_is_ignored(self):
        output = '\n'.join([
            'u_str LISTEN 0 128 /run/app.sock 123 * 0',
            'tcp LISTEN 0',
            'tcp LISTEN zero 128 127.0.0.1:80 0.0.0.0:*',
            'tcp LISTEN 0 128 127.0.0.1:65536 0.0.0.0:*',
            'tcp LISTEN 0 128 [::1:80 [::]:*',
            'tcp LISTEN 0 128 example.invalid:80 0.0.0.0:*',
            'tcp LISTEN 0 128 *:* *:*',
            'tcp ESTAB 0 0 127.0.0.1:80 127.0.0.1:42000',
            'tcp LISTEN 0 128 [fe80::1%]:80 [::]:*',
            'tcp LISTEN 0 128 127.0.0.1:22 0.0.0.0:*',
        ])
        rows, skipped = listeners.parse_ss(output)
        self.assertEqual(skipped, 8)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['port'], 22)

    def test_quoted_process_name_is_unescaped_without_evaluation(self):
        rows, _ = listeners.parse_ss(
            r'tcp LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("a\"b",pid=23,fd=1))'
        )
        self.assertEqual(rows[0]['processes'], [{'name': 'a"b', 'pid': 23}])


class CollectionTests(unittest.TestCase):
    def pipe_process(self, stdout=b'', stderr=b''):
        process = mock.MagicMock()
        for name, data in [('stdout', stdout), ('stderr', stderr)]:
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, data)
            finally:
                os.close(write_fd)
            stream = os.fdopen(read_fd, 'rb')
            self.addCleanup(stream.close)
            setattr(process, name, stream)
        process.__enter__.return_value = process
        process.wait.return_value = 0
        process.poll.return_value = 0
        return process

    def test_fixed_command_has_locale_and_no_shell_or_stdin(self):
        process = self.pipe_process(b'udp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:*\n')
        with mock.patch.object(listeners, '_ss_path', return_value='/usr/bin/ss'), \
                mock.patch.object(listeners.subprocess, 'Popen', return_value=process) as popen:
            output, error, truncated = listeners._run_ss()
        self.assertIn('5353', output)
        self.assertIsNone(error)
        self.assertFalse(truncated)
        self.assertEqual(popen.call_args.args[0], ['/usr/bin/ss', '-H', '-lntup'])
        self.assertEqual(popen.call_args.kwargs['env']['LC_ALL'], 'C')
        self.assertFalse(popen.call_args.kwargs['shell'])
        self.assertEqual(popen.call_args.kwargs['stdin'], listeners.subprocess.DEVNULL)

    def test_output_cap_discards_incomplete_socket_row_and_kills_child(self):
        line = b'tcp LISTEN 0 128 127.0.0.1:22 0.0.0.0:*\n'
        process = self.pipe_process(line * 3)
        process.poll.return_value = None
        with mock.patch.object(listeners, '_ss_path', return_value='/usr/bin/ss'), \
                mock.patch.object(listeners, 'OUTPUT_LIMIT', len(line) + 10), \
                mock.patch.object(listeners.subprocess, 'Popen', return_value=process):
            output, error, truncated = listeners._run_ss()
        self.assertEqual(output, line.decode())
        self.assertTrue(truncated)
        self.assertIn('output limit', error)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)

    def test_timeout_kills_child(self):
        process = self.pipe_process()
        process.poll.return_value = None
        with mock.patch.object(listeners, '_ss_path', return_value='/usr/bin/ss'), \
                mock.patch.object(listeners.subprocess, 'Popen', return_value=process), \
                mock.patch.object(listeners.time, 'monotonic', side_effect=[0, 4]):
            output, error, truncated = listeners._run_ss()
        self.assertEqual(output, '')
        self.assertIn('timed out', error)
        self.assertTrue(truncated)
        process.kill.assert_called_once()

    def test_stderr_warning_is_not_reported_as_complete_success(self):
        process = self.pipe_process(stderr=b'Permission information unavailable\n')
        with mock.patch.object(listeners, '_ss_path', return_value='/usr/bin/ss'), \
                mock.patch.object(listeners.subprocess, 'Popen', return_value=process):
            _, error, truncated = listeners._run_ss()
        self.assertIn('warning', error)
        self.assertFalse(truncated)

    def test_snapshot_distinguishes_hidden_owners_partial_and_missing_utility(self):
        line = 'tcp LISTEN 0 128 127.0.0.1:22 0.0.0.0:*\n'
        with mock.patch.object(listeners, '_run_ss', return_value=(line, None, False)):
            result = listeners.snapshot()
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['process_visibility'], 'limited')
        self.assertIn('Other owners may be hidden', result['process_visibility_notice'])
        self.assertIn('does not establish reachability', result['exposure_notice'])
        with mock.patch.object(listeners, '_run_ss', return_value=(line + 'invalid\n', None, False)):
            result = listeners.snapshot()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['skipped_lines'], 1)
        with mock.patch.object(listeners, '_run_ss', side_effect=RuntimeError('ss unavailable')):
            result = listeners.snapshot()
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['listeners'], [])
        self.assertEqual(result['error'], 'ss unavailable')

    def test_empty_success_is_not_a_collection_error(self):
        with mock.patch.object(listeners, '_run_ss', return_value=('', None, False)):
            result = listeners.snapshot()
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['process_visibility'], 'unknown')


if __name__ == '__main__':
    unittest.main()
