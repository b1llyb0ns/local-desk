"""Trash lifecycle contracts, using only an isolated local database and FakeEngine."""
import concurrent.futures
import datetime
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from test_panel import FakeEngine, SNAPSHOT, server


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='local-desk-trash-')
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.record = self.store.add({'name': 'Trash fixture', 'alias': 'trash-fixture', 'host': '192.0.2.10',
                                      'notes': 'private fixture notes', 'short_note': 'fixture purpose',
                                      'lease_end': '2026-09-05'})
        self.identifier = self.record['id']
        self.engine = mock.Mock(wraps=FakeEngine())
        self.engine.fingerprint = FakeEngine.fingerprint
        self.engine.accept_host_key = mock.Mock(return_value='SHA256:LOCAL-FIXTURE')
        self.app = server.Application(self.store, self.engine)
        self.app.rentals.sender = mock.Mock()
        self.app.rentals.clock = lambda: datetime.datetime(2026, 9, 5, 12, tzinfo=datetime.timezone.utc)
        self.http = None
        self.http_thread = None

    def tearDown(self):
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
            self.http_thread.join(timeout=3)
        self.app.pool.shutdown(wait=True)
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, method='GET', data=None, *, path=None, headers=None):
        if self.http is None:
            self.http = server.make_server(self.app, 0)
            self.http_thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=.01), daemon=True)
            self.http_thread.start()
        request_headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf,
                           'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0.0.0 Safari/537.36'}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=3)
        try:
            connection.request(method, path or '/api/servers/' + self.identifier,
                               body=None if data is None else json.dumps(data), headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def move_to_trash(self):
        return self.app.update_server(self.identifier, {'archived': True})

    def confirm(self):
        return {'confirm_alias': self.record['alias']}

    def assert_no_ssh(self):
        self.engine.collect.assert_not_called()
        self.engine.onboard.assert_not_called()
        self.engine.accept_host_key.assert_not_called()

    def test_trash_restore_preserves_local_details_and_lifecycle_audit(self):
        self.store.snapshot(self.identifier, SNAPSHOT)
        before = self.store.get(self.identifier)
        self.assertEqual(self.request('PATCH', {'archived': True})[0], 200)
        trashed = self.request()[1]['server']
        self.assertTrue(trashed['archived'])
        self.assertEqual(self.request('PATCH', {'archived': False})[0], 200)
        restored = self.request()[1]['server']
        self.assertFalse(restored['archived'])
        for field in ('id', 'alias', 'host', 'notes', 'short_note', 'lease_end', 'snapshot', 'history'):
            self.assertEqual(trashed[field], before[field])
            self.assertEqual(restored[field], before[field])
        messages = '\n'.join(event['message'] for event in self.store.events())
        self.assertIn('Moved local server record to Trash: trash-fixture', messages)
        self.assertIn('Restored local server record from Trash: trash-fixture', messages)
        self.assertNotIn(before['notes'], messages)
        self.assertTrue(self.app.rentals.wake_event.is_set())
        self.assert_no_ssh()

    def test_delete_requires_trash_and_exact_alias_confirmation(self):
        self.assertEqual(self.request('DELETE', self.confirm())[0], 409)
        self.move_to_trash()
        for data in ({}, {'confirm_alias': ''}, {'confirm_alias': None}, {'confirm_alias': True},
                     {'confirm_alias': 'Trash-fixture'}, {'confirm_alias': 'trash-fixture '},
                     {'confirm_alias': 'Trash fixture'}, {'confirm_alias': 'trash-fixture', 'force': True}):
            with self.subTest(data=data):
                self.assertEqual(self.request('DELETE', data)[0], 400)
                self.assertTrue(self.store.get(self.identifier)['archived'])
        self.assertEqual(self.request('DELETE', self.confirm()), (200, {'deleted': self.identifier}))
        self.assertEqual(self.request()[0], 404)
        self.assertEqual(self.request('DELETE', self.confirm())[0], 404)
        self.assert_no_ssh()

    def test_delete_keeps_existing_local_request_guards(self):
        self.move_to_trash()
        for headers in ({'X-VPS-CSRF': ''}, {'Origin': 'https://example.invalid'},
                        {'Host': 'example.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('DELETE', self.confirm(), headers=headers)[0], 403)
                self.assertTrue(self.store.get(self.identifier)['archived'])
        self.assert_no_ssh()

    def test_delete_removes_related_local_data_and_detaches_existing_events(self):
        other = self.store.add({'name': 'Keep', 'alias': 'keep-fixture', 'host': '192.0.2.11'})
        for identifier in (self.identifier, other['id']):
            self.store.snapshot(identifier, SNAPSHOT)
            self.store.event(identifier, 'info', 'A previous lifecycle event.')
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT INTO rental_notifications VALUES(?,?,?,?)',
                                      (identifier, '2026-09-05', '2026-09-05', server.utcnow()))
        self.move_to_trash()
        self.store.list()  # A warm summary must not keep returning the deleted row.
        self.app.jobs = {'gone': {'id': 'gone', 'server_id': self.identifier, 'state': 'done'},
                         'keep': {'id': 'keep', 'server_id': other['id'], 'state': 'done'}}
        self.app.delete_server(self.identifier, self.confirm())
        for table in ('history', 'rental_notifications', 'events'):
            with self.subTest(table=table), self.store.lock:
                self.assertEqual(self.store.db.execute(f'SELECT COUNT(*) FROM {table} WHERE server_id=?',
                                                      (self.identifier,)).fetchone()[0], 0)
                self.assertGreater(self.store.db.execute(f'SELECT COUNT(*) FROM {table} WHERE server_id=?',
                                                        (other['id'],)).fetchone()[0], 0)
        self.assertEqual([row['id'] for row in self.store.list()], [other['id']])
        self.assertEqual(set(self.app.jobs), {'keep'})
        events = self.store.events()
        self.assertTrue(any(event['server_id'] is None and event['message'] == 'A previous lifecycle event.' for event in events))
        deleted = events[0]
        self.assertIsNone(deleted['server_id'])
        self.assertIsNone(deleted['server_name'])
        self.assertIn('Permanently deleted local server record: trash-fixture. VPS unchanged.', deleted['message'])
        self.assertNotIn(self.record['notes'], json.dumps(events))
        self.assertNotIn('PRIVATE KEY', json.dumps(events))
        self.assert_no_ssh()

    def test_trash_and_restore_immediately_invalidate_warm_rental_cache(self):
        self.assertEqual([row['id'] for row in self.app.rentals.snapshot()['alerts']], [self.identifier])
        self.move_to_trash()
        self.assertEqual(self.app.rentals.snapshot()['alerts'], [])
        self.app.update_server(self.identifier, {'archived': False})
        self.assertEqual([row['id'] for row in self.app.rentals.snapshot()['alerts']], [self.identifier])
        self.move_to_trash()
        self.app.delete_server(self.identifier, self.confirm())
        self.assertEqual(self.app.rentals.snapshot()['alerts'], [])

    def test_trash_exports_and_restore_touch_no_keys_or_unmanaged_aliases(self):
        ssh = self.home / '.ssh'
        ssh.mkdir()
        key = ssh / 'id_fixture'
        key.write_text('PRIVATE KEY FIXTURE: never removed or rewritten\n')
        key.chmod(0o600)
        config = ssh / 'config'
        unmanaged = 'Host outside-fixture\n    HostName 192.0.2.99\n'
        config.write_text(unmanaged)
        self.store.export_enabled = True
        self.store.export_connections()
        managed = ssh / 'local-desk.conf'
        self.assertIn('Host trash-fixture', managed.read_text())
        self.move_to_trash()
        self.assertNotIn('trash-fixture', managed.read_text())
        self.app.update_server(self.identifier, {'archived': False})
        self.assertIn('Host trash-fixture', managed.read_text())
        self.move_to_trash()
        self.app.delete_server(self.identifier, self.confirm())
        self.assertNotIn('trash-fixture', managed.read_text())
        self.assertIn(unmanaged, config.read_text())
        self.assertEqual(key.read_text(), 'PRIVATE KEY FIXTURE: never removed or rewritten\n')
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assert_no_ssh()

    def test_restore_rechecks_alias_added_to_user_config_while_in_trash(self):
        self.move_to_trash()
        ssh = self.home / '.ssh'
        ssh.mkdir()
        (ssh / 'config').write_text('Host trash-fixture\n    HostName 192.0.2.99\n')
        self.store.export_enabled = True
        self.assertEqual(self.request('PATCH', {'archived': False})[0], 400)
        self.assertTrue(self.store.get(self.identifier)['archived'])
        self.assert_no_ssh()

    def test_export_failure_preserves_trashed_record_and_history(self):
        self.store.snapshot(self.identifier, SNAPSHOT)
        self.move_to_trash()
        with mock.patch.object(self.store, 'export_connections', side_effect=server.InputError('Cannot export')):
            self.assertEqual(self.request('DELETE', self.confirm())[0], 400)
        self.assertTrue(self.store.get(self.identifier)['archived'])
        self.assertEqual(len(self.store.get(self.identifier)['history']), 1)

    def test_queued_and_running_jobs_block_all_lifecycle_changes_but_allow_notes(self):
        for state in ('queued', 'running', 'done'):
            # Even a finished job remains reserved until its final event is saved.
            for archived in (False, True):
                with self.subTest(state=state, archived=archived):
                    self.store.update(self.identifier, {'archived': archived})
                    self.app.jobs['reserved'] = {'id': 'reserved', 'server_id': self.identifier, 'state': state}
                    self.app.busy[self.identifier] = 'reserved'
                    self.assertEqual(self.request('PATCH', {'archived': not archived})[0], 409)
                    self.assertEqual(self.request('DELETE', self.confirm())[0], 409)
                    self.assertEqual(self.request('PATCH', {'notes': 'draft during job'})[0], 200)
                    self.assertEqual(self.store.get(self.identifier)['archived'], archived)
                    self.app.busy.clear()
        self.assert_no_ssh()

    def test_no_new_ssh_task_or_host_key_operation_for_trashed_record(self):
        self.move_to_trash()
        for action in ('refresh', 'onboard', 'host-key'):
            with self.subTest(action=action):
                data = {'credentials': {'mode': 'managed'}} if action == 'onboard' else {}
                self.assertEqual(self.request('POST', data, path=f'/api/servers/{self.identifier}/{action}')[0], 409)
        self.assertEqual(self.app.jobs, {})
        self.assertEqual(self.app.busy, {})
        self.assert_no_ssh()

    def test_new_server_cannot_be_created_in_trash(self):
        data = {'server': {'name': 'New', 'alias': 'new-fixture', 'host': '192.0.2.12', 'archived': True},
                'credentials': {'mode': 'managed'}}
        self.assertEqual(self.request('POST', data, path='/api/servers')[0], 400)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.app.jobs, {})
        self.assert_no_ssh()

    def test_refresh_all_skips_trashed_records(self):
        self.move_to_trash()
        other = self.store.add({'name': 'Active', 'alias': 'active-fixture', 'host': '192.0.2.11'})
        with mock.patch.object(self.app.pool, 'submit') as submit:
            code, body = self.request('POST', {}, path='/api/refresh-all')
            self.assertEqual(code, 202)
            self.assertEqual([job['server_id'] for job in body['jobs']], [other['id']])
            submit.assert_called_once()
        self.assert_no_ssh()

    def test_failed_queue_submission_releases_reservation_and_credentials(self):
        credentials = {'mode': 'managed', 'password': 'short-lived fixture'}
        with mock.patch.object(self.app.pool, 'submit', side_effect=RuntimeError('pool stopped')):
            with self.assertRaisesRegex(RuntimeError, 'pool stopped'):
                self.app.submit(self.identifier, 'onboard', credentials)
        self.assertEqual(credentials, {})
        self.assertEqual(self.app.jobs, {})
        self.assertEqual(self.app.busy, {})
        self.assertTrue(self.move_to_trash()['archived'])

    def test_delete_serializes_before_submit_without_orphan_job(self):
        self.move_to_trash()
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original_delete = self.store.delete

        def paused_delete(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Delete barrier timed out')
            return original_delete(*args)

        def submit_after_delete():
            attempted.set()
            return self.app.submit(self.identifier, 'refresh')

        with mock.patch.object(self.store, 'delete', side_effect=paused_delete), concurrent.futures.ThreadPoolExecutor(2) as pool:
            deleting = pool.submit(self.app.delete_server, self.identifier, self.confirm())
            try:
                self.assertTrue(entered.wait(2))
                submitting = pool.submit(submit_after_delete)
                self.assertTrue(attempted.wait(2))
            finally:
                release.set()
            deleting.result(timeout=3)
            with self.assertRaises(KeyError):
                submitting.result(timeout=3)
        self.assertEqual(self.app.jobs, {})
        self.assertEqual(self.app.busy, {})
        self.assert_no_ssh()

    def test_move_to_trash_serializes_before_submit(self):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original_update = self.store.update

        def paused_update(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Update barrier timed out')
            return original_update(*args, **kwargs)

        def submit_after_update():
            attempted.set()
            return self.app.submit(self.identifier, 'refresh')

        with mock.patch.object(self.store, 'update', side_effect=paused_update), concurrent.futures.ThreadPoolExecutor(2) as pool:
            moving = pool.submit(self.move_to_trash)
            try:
                self.assertTrue(entered.wait(2))
                submitting = pool.submit(submit_after_update)
                self.assertTrue(attempted.wait(2))
            finally:
                release.set()
            self.assertTrue(moving.result(timeout=3)['archived'])
            with self.assertRaises(server.ConflictError):
                submitting.result(timeout=3)
        self.assertEqual(self.app.jobs, {})
        self.assert_no_ssh()

    def test_submit_reservation_precedes_competing_trash_change(self):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()

        def paused_enqueue(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Queue barrier timed out')

        def competing_trash():
            attempted.set()
            return self.move_to_trash()

        with mock.patch.object(self.app.pool, 'submit', side_effect=paused_enqueue), concurrent.futures.ThreadPoolExecutor(2) as pool:
            submitting = pool.submit(self.app.submit, self.identifier, 'refresh')
            try:
                self.assertTrue(entered.wait(2))
                moving = pool.submit(competing_trash)
                self.assertTrue(attempted.wait(2))
            finally:
                release.set()
            job = submitting.result(timeout=3)
            with self.assertRaises(server.ConflictError):
                moving.result(timeout=3)
        self.assertEqual(self.app.busy[self.identifier], job['id'])
        self.assertFalse(self.store.get(self.identifier)['archived'])
        self.assert_no_ssh()

    def test_running_fake_worker_must_finish_before_trash_and_delete(self):
        entered, release = threading.Event(), threading.Event()

        def paused_collect(record):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Worker barrier timed out')
            return FakeEngine().collect(record)

        self.engine.collect.side_effect = paused_collect
        job = self.app.submit(self.identifier, 'refresh')
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request('PATCH', {'archived': True})[0], 409)
            self.assertEqual(self.request('DELETE', self.confirm())[0], 409)
        finally:
            release.set()
        self.app.pool.shutdown(wait=True)
        self.assertEqual(self.app.jobs[job['id']]['state'], 'done')
        self.assertNotIn(self.identifier, self.app.busy)
        self.move_to_trash()
        self.app.delete_server(self.identifier, self.confirm())
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.app.jobs, {})
        self.engine.collect.assert_called_once()

    def test_host_key_reservation_blocks_lifecycle_and_is_released_on_error(self):
        entered, release = threading.Event(), threading.Event()

        def paused_host_key(record):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Host key barrier timed out')
            raise server.InputError('Fixture host-key failure')

        self.engine.accept_host_key.side_effect = paused_host_key
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            accepting = pool.submit(self.app.accept_host_key, self.identifier)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.request('PATCH', {'archived': True})[0], 409)
                self.assertEqual(self.request('DELETE', self.confirm())[0], 409)
                with self.assertRaises(server.ConflictError):
                    self.app.submit(self.identifier, 'refresh')
            finally:
                release.set()
            with self.assertRaisesRegex(server.InputError, 'Fixture host-key failure'):
                accepting.result(timeout=3)
        self.assertEqual(self.app.host_key_busy, set())
        self.assertTrue(self.move_to_trash()['archived'])

    def test_delete_waits_for_inflight_reminder_and_removes_its_delivery_row(self):
        self.move_to_trash()
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()

        def finishing_delivery():
            with self.app.rentals.delivery_lock:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Delivery barrier timed out')
                with self.store.lock, self.store.db:
                    self.store.db.execute('INSERT INTO rental_notifications VALUES(?,?,?,?)',
                                          (self.identifier, '2026-09-05', '2026-09-05', server.utcnow()))

        def competing_delete():
            attempted.set()
            self.app.delete_server(self.identifier, self.confirm())

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            delivering = pool.submit(finishing_delivery)
            try:
                self.assertTrue(entered.wait(2))
                deleting = pool.submit(competing_delete)
                self.assertTrue(attempted.wait(2))
            finally:
                release.set()
            delivering.result(timeout=3)
            deleting.result(timeout=3)
        with self.store.lock:
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rental_notifications').fetchone()[0], 0)
        self.assertEqual(self.store.list(), [])
        self.assert_no_ssh()


if __name__ == '__main__':
    unittest.main()
