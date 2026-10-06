import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go_toolchain


IUTF8 = getattr(termios, 'IUTF8', 0x4000)


class ConfirmationTests(unittest.TestCase):
    def test_only_an_explicit_yes_is_accepted(self):
        for reply, expected in [('yes', True), (' YES ', True), ('no', False), ('', False),
                                ('yes please', False), ('ye\udcd1s', False)]:
            with self.subTest(reply=repr(reply)), mock.patch.object(sys, 'stdin', io.StringIO()), \
                    mock.patch('builtins.input', return_value=reply):
                self.assertEqual(go_toolchain.confirm_yes('Type yes: '), expected)

    def test_unreadable_input_is_declined_without_a_codec_error(self):
        for error in [UnicodeDecodeError('utf-8', b'ye\xd1s', 2, 3, 'invalid continuation byte'), EOFError()]:
            with self.subTest(error=type(error).__name__), mock.patch.object(sys, 'stdin', io.StringIO()), \
                    mock.patch.object(sys, 'stderr', io.StringIO()) as output, \
                    mock.patch('builtins.input', side_effect=error):
                self.assertFalse(go_toolchain.confirm_yes('Type yes: '))
                self.assertTrue(output.getvalue().strip())
                self.assertNotIn('codec', output.getvalue())

    def test_unreadable_root_confirmation_stops_before_installation(self):
        candidate = {'version': 'go1.27.1', 'url': 'https://go.dev/dl/go1.27.1.linux-amd64.tar.gz',
                     'sha256': 'a' * 64}
        for error in [UnicodeDecodeError('utf-8', b'ye\xd1s', 2, 3, 'invalid continuation byte'), EOFError()]:
            with self.subTest(error=type(error).__name__), mock.patch.object(sys, 'stdin', io.StringIO()) as terminal, \
                    mock.patch.object(terminal, 'isatty', return_value=True), \
                    mock.patch.object(go_toolchain.os, 'geteuid', return_value=0), \
                    mock.patch.object(go_toolchain, '_standard_version', return_value='go1.27.0'), \
                    mock.patch.object(go_toolchain, 'check_release', return_value=candidate), \
                    mock.patch.object(go_toolchain, '_running_toolchain', return_value=[]), \
                    mock.patch.object(go_toolchain, '_download') as download, \
                    mock.patch.object(go_toolchain.os, 'open', side_effect=AssertionError('Unexpected installation')) as open_file, \
                    mock.patch('builtins.input', side_effect=error), mock.patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
                    go_toolchain._root_install('go1.27.1')
                download.assert_not_called()
                open_file.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux terminal input flags')
    def test_original_terminal_flags_are_restored_for_every_exit(self):
        outcomes = ['yes', 'no', UnicodeDecodeError('utf-8', b'ye\xd1s', 2, 3, 'invalid continuation byte'),
                    EOFError(), KeyboardInterrupt(), RuntimeError('Input unavailable')]
        for initially_enabled in (False, True):
            for outcome in outcomes:
                with self.subTest(initially_enabled=initially_enabled, outcome=repr(outcome)):
                    master, slave = pty.openpty()
                    try:
                        attributes = termios.tcgetattr(slave)
                        attributes[0] = attributes[0] | IUTF8 if initially_enabled else attributes[0] & ~IUTF8
                        termios.tcsetattr(slave, termios.TCSANOW, attributes)
                        original = termios.tcgetattr(slave)

                        def read_answer(prompt):
                            self.assertTrue(termios.tcgetattr(slave)[0] & IUTF8)
                            if isinstance(outcome, BaseException):
                                raise outcome
                            return outcome

                        with os.fdopen(os.dup(slave), 'r', encoding='utf-8', errors='strict') as terminal, \
                                mock.patch.object(sys, 'stdin', terminal), \
                                mock.patch.object(sys, 'stderr', io.StringIO()), \
                                mock.patch('builtins.input', side_effect=read_answer):
                            if isinstance(outcome, (KeyboardInterrupt, RuntimeError)):
                                with self.assertRaises(type(outcome)):
                                    go_toolchain.confirm_yes('Type yes: ')
                            else:
                                self.assertEqual(go_toolchain.confirm_yes('Type yes: '), outcome == 'yes')
                        self.assertEqual(termios.tcgetattr(slave), original)
                    finally:
                        os.close(master)
                        os.close(slave)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux UTF-8 erase handling')
class TerminalInputTests(unittest.TestCase):
    def read_reply(self, payload, helper=True, errors='strict'):
        code = '''
import json
import sys
import termios
import go_toolchain
original = termios.tcgetattr(0)
result = {}
try:
    result['accepted'] = (go_toolchain.confirm_yes('Type yes: ') if sys.argv[1] == 'helper'
                          else input('Type yes: ').strip().lower() == 'yes')
except Exception as error:
    result.update(error=type(error).__name__, message=str(error))
result['restored'] = termios.tcgetattr(0) == original
print(json.dumps(result), flush=True)
'''
        master, slave = pty.openpty()
        process = None
        try:
            attributes = termios.tcgetattr(slave)
            attributes[0] &= ~IUTF8
            attributes[3] |= termios.ICANON | termios.ECHO | termios.ECHOE
            attributes[6][termios.VERASE] = b'\x7f'
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            original = termios.tcgetattr(slave)
            environment = dict(os.environ, PYTHONIOENCODING='utf-8:' + errors,
                               PYTHONDONTWRITEBYTECODE='1', LC_ALL='C.UTF-8')
            process = subprocess.Popen([sys.executable, '-c', code, 'helper' if helper else 'input'],
                                       cwd=Path(__file__).resolve().parents[1], env=environment,
                                       stdin=slave, stdout=slave, stderr=slave)
            output = bytearray()
            deadline = time.monotonic() + 5
            while b'Type yes: ' not in output:
                self.assertLess(time.monotonic(), deadline, bytes(output))
                self.assertIsNone(process.poll(), bytes(output))
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 4096))
            os.write(master, payload)
            while True:
                self.assertLess(time.monotonic(), deadline, bytes(output))
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 4096))
                elif process.poll() is not None:
                    break
            self.assertEqual(process.wait(timeout=1), 0, bytes(output))
            self.assertEqual(termios.tcgetattr(slave), original)
            records = [json.loads(line) for line in bytes(output).splitlines() if line.startswith(b'{')]
            self.assertEqual(len(records), 1, bytes(output))
            return records[0]
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)

    def test_cyrillic_backspace_reproduces_the_error_and_accepts_the_corrected_answer(self):
        payload = b'ye\xd1\x81\x7fs\n'
        previous = self.read_reply(payload, helper=False)
        self.assertEqual(previous['error'], 'UnicodeDecodeError')
        self.assertEqual(previous['message'], "'utf-8' codec can't decode byte 0xd1 in position 2: invalid continuation byte")
        self.assertEqual(self.read_reply(payload), {'accepted': True, 'restored': True})

    def test_malformed_bytes_never_become_confirmation(self):
        for errors in ('strict', 'surrogateescape'):
            with self.subTest(errors=errors):
                self.assertEqual(self.read_reply(b'ye\xd1s\n', errors=errors),
                                 {'accepted': False, 'restored': True})


if __name__ == '__main__':
    unittest.main()
