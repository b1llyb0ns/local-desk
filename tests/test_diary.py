import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import server
from diary import Diary, DiaryError, DiaryConflict


class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'tmp')
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.diary = Diary(self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_monday_week_boundaries_and_year_rollover(self):
        for day in ['2025-12-28', '2025-12-29', '2026-01-04', '2026-01-05']:
            self.diary.save(day, {'content': day, 'revision': 0})
        week = self.diary.week('2026-01-04')
        self.assertEqual((week['week_start'], week['week_end']), ('2025-12-29', '2026-01-04'))
        self.assertEqual([r['entry_date'] for r in week['entries']], ['2025-12-29', '2026-01-04'])
        self.assertEqual(self.diary.week('2026-03-29')['week_start'], '2026-03-23')

    def test_preserves_text_and_persists_across_connections(self):
        content = '  Сделал резервную копию\n\n<b>Заметка</b>\n'
        entry = self.diary.save('2026-09-07', {'content': content, 'revision': 0})
        self.assertEqual(entry['content'], content)
        other = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        try:
            self.assertEqual(Diary(other).week('2026-09-07')['entries'], [entry])
        finally:
            other.db.close()

    def test_stale_save_cannot_overwrite_another_tab_or_cleared_entry(self):
        first = self.diary.save('2026-09-07', {'content': 'Первый вариант', 'revision': 0})
        other = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        try:
            changed = Diary(other).save('2026-09-07', {'content': 'Другой вариант', 'revision': first['revision']})
            with self.assertRaises(DiaryConflict) as caught:
                self.diary.save('2026-09-07', {'content': 'Старый черновик', 'revision': first['revision']})
            self.assertEqual(caught.exception.entry, changed)
            cleared = self.diary.save('2026-09-07', {'content': '', 'revision': changed['revision']})
            with self.assertRaises(DiaryConflict):
                Diary(other).save('2026-09-07', {'content': 'Новый', 'revision': 0})
            self.assertEqual(self.diary.week('2026-09-07')['entries'], [cleared])
        finally:
            other.db.close()

    def test_invalid_dates_and_payloads_leave_existing_text_untouched(self):
        self.diary.save('2026-09-07', {'content': 'Запись', 'revision': 0})
        for day in ['2026-02-30', '20260907', '07.09.2026', '', None]:
            with self.subTest(day=day), self.assertRaises(DiaryError):
                self.diary.week(day)
        for payload in [{}, {'content': '', 'revision': True}, {'content': [], 'revision': 1},
                        {'content': '\x00', 'revision': 1}, {'content': 'я' * 12001, 'revision': 1},
                        {'content': '', 'revision': -1}, {'content': '', 'revision': 1, 'date': '2026-09-08'}]:
            with self.subTest(payload=str(payload)[:70]), self.assertRaises(DiaryError):
                self.diary.save('2026-09-07', payload)
        self.assertEqual(self.diary.week('2026-09-07')['entries'][0]['content'], 'Запись')

    def test_http_guards_week_validation_and_conflict_response(self):
        app = server.Application(self.store, mock.Mock())
        http_server = server.make_server(app, 0)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        def request(path, method='GET', payload=None, trusted=True):
            headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'}
            if trusted: headers['X-VPS-CSRF'] = app.csrf
            connection = http.client.HTTPConnection('127.0.0.1', http_server.server_port, timeout=3)
            try:
                connection.request(method, path, json.dumps(payload or {}), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally: connection.close()
        try:
            for path in ['/api/diary', '/api/diary?week=2026-02-30', '/api/diary?week=2026-09-07&week=2026-09-08']:
                self.assertEqual(request(path)[0], 400)
            path = '/api/diary/2026-09-07'
            payload = {'content': 'Настроил сервер', 'revision': 0}
            self.assertEqual(request(path, 'PATCH', payload, trusted=False)[0], 403)
            self.assertEqual(request(path, 'PATCH', payload)[0], 200)
            status, conflict = request(path, 'PATCH', {'content': 'Другой', 'revision': 0})
            self.assertEqual(status, 409)
            self.assertEqual(conflict['entry']['content'], payload['content'])
            self.assertEqual(request('/api/diary?week=2026-09-08')[1]['entries'][0]['content'], payload['content'])
        finally:
            http_server.shutdown(); http_server.server_close(); thread.join(); app.pool.shutdown(wait=True)
