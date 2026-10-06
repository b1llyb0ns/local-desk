from http.client import HTTPConnection
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from test_panel import FakeEngine

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'


class RuntimeServerTests(unittest.TestCase):
    def test_main_uses_request_wait_and_cleans_up_on_interrupt(self):
        http = mock.Mock()
        http.server_address = ('127.0.0.1', 8787)
        http.handle_request.side_effect = [None, KeyboardInterrupt]
        app = mock.Mock()
        args = SimpleNamespace(port=8787, data_dir='/unused')
        with mock.patch.object(server.argparse.ArgumentParser, 'parse_args', return_value=args), \
                mock.patch.object(server.os, 'umask'), mock.patch.object(server, 'Store'), \
                mock.patch.object(server, 'SSHEngine'), mock.patch.object(server, 'Application', return_value=app), \
                mock.patch.object(server, 'make_server', return_value=http), mock.patch('builtins.print'):
            server.main()
        self.assertEqual(http.handle_request.call_count, 2)
        http.serve_forever.assert_not_called()
        http.server_close.assert_called_once()
        app.rentals.stop.assert_called_once()
        app.desktop.stop.assert_called_once()
        app.pool.shutdown.assert_called_once_with(wait=True)

    def test_request_wait_has_no_periodic_timeout(self):
        with socketserver.TCPServer(('127.0.0.1', 0), socketserver.BaseRequestHandler) as http:
            selector = mock.MagicMock()
            selector.__enter__.return_value = selector
            selector.select.side_effect = KeyboardInterrupt
            with mock.patch.object(socketserver, '_ServerSelector', return_value=selector), self.assertRaises(KeyboardInterrupt):
                http.handle_request()
            selector.select.assert_called_once_with(None)

    def test_blocking_wait_dispatches_real_request(self):
        root = Path(__file__).resolve().parents[1] / 'tmp'
        with tempfile.TemporaryDirectory(prefix='http-runtime-', dir=root) as directory:
            home = Path(directory)
            store = server.Store(home / 'data', home, import_existing=False, export=False)
            app = server.Application(store, FakeEngine())
            http = server.make_server(app, 0)
            # A test-only deadline prevents a failed assertion from hanging.
            http.timeout = 3
            worker = threading.Thread(target=http.handle_request, daemon=True)
            worker.start()
            connection = None
            try:
                connection = HTTPConnection('127.0.0.1', http.server_port, timeout=3)
                connection.request('GET', '/api/jobs', headers={'User-Agent': UA})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'{"jobs":[]}')
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            finally:
                if connection is not None:
                    connection.close()
                worker.join(timeout=3)
                http.server_close()
                app.pool.shutdown(wait=True)
                store.db.close()


if __name__ == '__main__':
    unittest.main()
