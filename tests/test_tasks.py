import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import server
from tasks import Tasks, TaskError, validate


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'tmp')
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.tasks = Tasks(self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_persistence_sorting_completion_and_reopening(self):
        later = self.tasks.add({'title': 'Продлить сервер', 'due_date': '2026-09-25', 'notes': 'Тариф Standard'})
        earlier = self.tasks.add({'title': 'Проверить резервную копию', 'due_date': '2026-09-18'})
        self.tasks.update(earlier, {'completed': True})
        other_store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        try:
            persisted = Tasks(other_store).snapshot()['tasks']
            self.assertEqual([row['id'] for row in persisted], [later, earlier])
            self.assertIs(persisted[1]['completed'], True)
            self.assertEqual(persisted[0]['notes'], 'Тариф Standard')
        finally:
            other_store.db.close()
        self.tasks.update(earlier, {'completed': False, 'title': 'Проверить копию'})
        self.assertEqual(self.tasks.snapshot()['tasks'][0]['id'], earlier)
        self.tasks.delete(later)
        self.assertEqual(len(self.tasks.snapshot()['tasks']), 1)
        with self.assertRaises(KeyError):
            self.tasks.update(later, {'title': 'Продлить сервер'})
        with self.assertRaises(KeyError):
            self.tasks.delete(later)

    def test_validates_dates_titles_types_and_unknown_fields(self):
        for payload in [{}, {'title': 'Копия'}, {'due_date': '2026-09-18'},
                        {'title': '   ', 'due_date': '2026-09-18'}]:
            with self.subTest(payload=payload), self.assertRaises(TaskError):
                validate(payload, creating=True)
        for payload in [{'due_date': value} for value in ['', '2026-02-30', '20260918', '18.09', '1999-12-31', '2201-01-01', None, True]] + [
                {'completed': 1}, {'title': 'x' * 201}, {'notes': '\x00'}, {'notes': 'x' * 4001}, {'id': 'unknown'}]:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(TaskError):
                validate(payload)
        self.assertEqual(validate({'due_date': '2028-02-29'})['due_date'], '2028-02-29')

    def test_failed_update_preserves_record(self):
        identifier = self.tasks.add({'title': 'Копия', 'due_date': '2026-09-18'})
        before = self.tasks.snapshot()
        with self.assertRaises(TaskError):
            self.tasks.update(identifier, {'title': 'Архив', 'due_date': '2026-02-30'})
        self.assertEqual(self.tasks.snapshot(), before)

    def test_http_lifecycle_validation_and_csrf(self):
        engine = mock.Mock()
        app = server.Application(self.store, engine)
        http_server = server.make_server(app, 0)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, data=None, trusted=True):
            headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'}
            if trusted:
                headers['X-VPS-CSRF'] = app.csrf
            connection = http.client.HTTPConnection('127.0.0.1', http_server.server_port, timeout=3)
            try:
                connection.request(method, path, json.dumps(data or {}), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            data = {'title': 'Продлить сервер', 'due_date': '2026-09-25'}
            self.assertEqual(request('POST', '/api/tasks', data, trusted=False)[0], 403)
            self.assertEqual(request('GET', '/api/tasks')[1], {'tasks': []})
            status, result = request('POST', '/api/tasks', data)
            self.assertEqual(status, 201)
            path = '/api/tasks/' + result['tasks'][0]['id']
            self.assertEqual(request('PATCH', path, {'due_date': '2026-02-30'})[0], 400)
            self.assertIs(request('PATCH', path, {'completed': True})[1]['tasks'][0]['completed'], True)
            self.assertEqual(request('DELETE', path, trusted=False)[0], 403)
            self.assertEqual(request('DELETE', path)[1], {'tasks': []})
            self.assertEqual(request('PATCH', path, {'completed': False})[0], 404)
            engine.assert_not_called()
            self.assertEqual(engine.method_calls, [])
        finally:
            http_server.shutdown(); http_server.server_close(); thread.join(); app.pool.shutdown(wait=True)
