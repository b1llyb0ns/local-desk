"""Cache/retry fixtures only; no real desktop notifications or production DB."""
from contextlib import closing
import datetime as dt
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rental_alerts import RentalAlerts
from server import Store


class RentalPerformanceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'tmp'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='rental-performance-', dir=root)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.now = dt.datetime(2026, 9, 5, 12, tzinfo=dt.timezone(dt.timedelta(hours=2), 'CEST'))
        self.sender = mock.Mock()
        self.alerts = RentalAlerts(self.store, sender=self.sender, clock=lambda: self.now)
        self.row = self.store.add({'name': 'Fixture', 'alias': 'fixture', 'host': '192.0.2.1', 'lease_end': '2026-09-06'})
        self.statements = []
        self.store.db.set_trace_callback(self.statements.append)

    def tearDown(self):
        self.alerts.stop()
        self.store.db.set_trace_callback(None)
        self.store.db.close()
        self.tmp.cleanup()

    def selects(self):
        return [statement for statement in self.statements if statement.lstrip().upper().startswith('SELECT')]

    def test_snapshot_batches_metadata_and_warm_calls_skip_selects(self):
        first = self.alerts.snapshot()
        self.assertEqual(len(self.selects()), 2)
        changes = self.store.db.total_changes
        self.statements.clear()
        for _ in range(20):
            self.assertEqual(self.alerts.snapshot(), first)
        self.assertEqual(self.selects(), [])
        self.assertEqual(self.statements, ['PRAGMA data_version'] * 20)
        self.assertEqual(self.store.db.total_changes, changes)

    def test_cached_response_shells_are_private(self):
        self.store.add({'name': 'Undated', 'alias': 'undated', 'host': '192.0.2.2'})
        result = self.alerts.snapshot()
        result['alerts'][0]['name'] = 'Mutated'
        result['undated'][0]['name'] = 'Mutated'
        result['notifications']['enabled'] = False
        result['thresholds'].clear()
        result['alerts'].append({'name': 'Invented'})
        fresh = self.alerts.snapshot()
        self.assertEqual([row['name'] for row in fresh['alerts']], ['Fixture'])
        self.assertEqual(fresh['undated'][0]['name'], 'Undated')
        self.assertTrue(fresh['notifications']['enabled'])
        self.assertEqual(fresh['thresholds'], [7, 3, 0])

    def test_successful_check_skips_table_work_after_one_reconciliation(self):
        self.assertEqual(self.alerts.check(), 1)
        self.assertEqual(self.alerts.check(), 0)
        self.statements.clear()
        changes = self.store.db.total_changes
        for _ in range(20):
            self.assertEqual(self.alerts.check(), 0)
        self.assertEqual(self.selects(), [])
        self.assertEqual(self.statements, ['PRAGMA data_version'] * 20)
        self.assertEqual(self.store.db.total_changes, changes)
        self.sender.assert_called_once()

    def test_disabled_and_unavailable_fast_paths_preserve_reenable(self):
        self.alerts.set_enabled(False)
        self.statements.clear()
        self.assertEqual(self.alerts.check(), 0)
        self.assertEqual(len(self.selects()), 1)
        self.assertIn('FROM metadata', self.selects()[0])
        self.statements.clear()
        self.assertEqual(self.alerts.check(), 0)
        self.assertEqual(self.selects(), [])
        self.alerts.available = False
        self.statements.clear()
        self.assertEqual(self.alerts.check(), 0)
        self.assertEqual(self.statements, [])
        self.alerts.available = True
        self.alerts.set_enabled(True)
        self.assertEqual(self.alerts.check(), 1)
        self.sender.assert_called_once()

    def test_local_and_external_edits_invalidate_both_fast_paths(self):
        self.store.update(self.row['id'], {'lease_end': '2026-10-06'})
        self.assertEqual(self.alerts.check(), 0)
        self.assertEqual(self.alerts.snapshot()['alerts'], [])
        self.store.update(self.row['id'], {'name': 'Locally renamed'})
        self.alerts.snapshot()
        with closing(sqlite3.connect(self.store.path)) as external, external:
            external.execute('UPDATE servers SET name=?,lease_end=? WHERE id=?',
                             ('External edit', '2026-09-05', self.row['id']))
        self.assertEqual(self.alerts.snapshot()['alerts'][0]['name'], 'External edit')
        self.assertEqual(self.alerts.check(), 1)
        self.assertIn('External edit', self.sender.call_args.args[1])
        self.store.update(self.row['id'], {'archived': True})
        self.assertEqual(self.alerts.snapshot()['alerts'], [])

    def test_midnight_and_timezone_changes_invalidate_without_db_writes(self):
        self.assertEqual(self.alerts.check(), 1)
        self.assertEqual(self.alerts.check(), 0)
        first = self.alerts.snapshot()
        self.now += dt.timedelta(days=1)
        second = self.alerts.snapshot()
        self.assertNotEqual(first['today'], second['today'])
        self.assertEqual(second['alerts'][0]['days'], 0)
        self.assertEqual(self.alerts.check(), 1)
        self.assertEqual(self.sender.call_count, 2)
        self.now = self.now.astimezone(dt.timezone.utc)
        self.assertEqual(self.alerts.snapshot()['timezone'], 'UTC')
        self.now = self.now.astimezone(dt.timezone(dt.timedelta(hours=1), 'UTC'))
        self.statements.clear()
        self.alerts.snapshot()
        self.assertEqual(len(self.selects()), 2, 'Offset changes invalidate even when timezone names match')
        self.assertEqual(self.alerts.check(), 0)

    def test_error_and_last_sent_metadata_invalidate_snapshot(self):
        self.alerts.snapshot()
        self.alerts.save_setting('rental_notification_error', 'Unavailable')
        self.assertEqual(self.alerts.snapshot()['notifications']['error'], 'Unavailable')
        self.alerts.save_setting('rental_notification_error', '')
        self.alerts.save_setting('rental_notification_last_sent', '2026-09-05T12:00:00+02:00')
        result = self.alerts.snapshot()['notifications']
        self.assertEqual(result['error'], '')
        self.assertEqual(result['last_sent_at'], '2026-09-05T12:00:00+02:00')

    def test_failure_always_retries_without_rewriting_identical_error(self):
        self.sender.side_effect = RuntimeError('Unavailable')
        with self.assertRaises(RuntimeError):
            self.alerts.check()
        changes = self.store.db.total_changes
        with self.assertRaises(RuntimeError):
            self.alerts.check()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM rental_notifications').fetchone()[0], 0)
        self.sender.side_effect = None
        self.assertEqual(self.alerts.check(), 1)
        self.assertEqual(self.alerts.snapshot()['notifications']['error'], '')

    def test_edit_during_sender_is_not_hidden_by_post_delivery_generation(self):
        other = self.store.add({'name': 'Other', 'alias': 'other', 'host': '192.0.2.2', 'lease_end': '2026-10-01'})
        self.sender.side_effect = lambda *args: self.store.update(other['id'], {'lease_end': '2026-09-05'})
        self.assertEqual(self.alerts.check(), 1)
        self.sender.side_effect = None
        self.assertEqual(self.alerts.check(), 1)
        self.assertIn('Other', self.sender.call_args.args[1])
        self.assertEqual(self.alerts.check(), 0)

    def test_snapshot_can_run_while_notification_sender_waits(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def sender(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('Fixture timeout')
        def notify():
            try:
                self.alerts.check()
            except Exception as error:
                errors.append(error)
        def read():
            try:
                self.alerts.snapshot()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        self.sender.side_effect = sender
        worker = threading.Thread(target=notify)
        reader = threading.Thread(target=read)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(finished.wait(1), 'Snapshot must not wait for the notification sender')
        finally:
            release.set()
            worker.join(timeout=3)
            if reader.ident is not None:
                reader.join(timeout=3)
        self.assertEqual(errors, [])

    def test_snapshot_cache_is_not_retained_above_record_limit(self):
        with self.store.lock, self.store.db:
            self.store.db.executemany('''INSERT INTO servers(id,alias,name,host,created_at,updated_at)
                VALUES(?,?,?,?,?,?)''', [(str(index), 'extra-' + str(index), 'Extra', 'host-' + str(index), '', '')
                                        for index in range(256)])
        self.alerts.snapshot()
        self.assertIsNone(self.alerts._snapshot_cache)


if __name__ == '__main__':
    unittest.main()
