import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rental_alerts import RentalAlerts
from server import Store, InputError, validate_fields


class RentalTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='rental-test-', dir=directory)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.now = dt.datetime(2026, 9, 5, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        self.sender = mock.Mock()
        self.alerts = RentalAlerts(self.store, sender=self.sender, clock=lambda: self.now)
        self.sequence = 0

    def tearDown(self):
        self.alerts.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, days=None, **fields):
        self.sequence += 1
        values = dict(name=f'Server {self.sequence}', alias=f'node-{self.sequence}',
                      host=f'192.0.2.{self.sequence}')
        values.update(fields)
        if days is not None:
            values['lease_end'] = (self.now.date() + dt.timedelta(days=days)).isoformat()
        return self.store.add(values)

    def test_thresholds_and_local_calendar_date(self):
        for days in [8, 7, 4, 3, 1, 0, -1]:
            self.add(days)
        data = self.alerts.snapshot()
        self.assertEqual(data['today'], '2026-09-05')  # UTC is still Sep 4.
        self.assertEqual([r['days'] for r in data['alerts']], [-1, 0, 1, 3, 4, 7])
        self.assertEqual([r['level'] for r in data['alerts']], ['overdue', 'today', 'urgent', 'urgent', 'soon', 'soon'])
        self.assertEqual(data['alerts'][2]['message'], 'Expires in 1 day')
        self.sender.assert_not_called()  # Listing never delivers or contacts SSH.

    def test_missing_year_and_archive_are_not_assumed(self):
        self.add(lease_hint='09-05')
        self.add()
        self.add(0, archived=True)
        data = self.alerts.snapshot()
        self.assertEqual(len(data['undated']), 2)
        self.assertEqual(data['alerts'], [])
        self.assertEqual(self.alerts.check(), 0)
        self.sender.assert_not_called()
        self.assertIsNone(self.store.list()[0]['lease_end'])

    def test_reminders_once_daily_survive_restart_and_refresh(self):
        self.add(7)
        self.assertEqual(self.alerts.check(), 1)
        for _ in range(3):
            self.assertEqual(self.alerts.check(), 0)
        restarted = RentalAlerts(self.store, sender=self.sender, clock=lambda: self.now)
        self.assertEqual(restarted.check(), 0)
        self.now += dt.timedelta(days=1)
        self.assertEqual(restarted.check(), 1)
        self.assertEqual(self.sender.call_count, 2)

    def test_renewal_new_date_and_archive(self):
        row = self.add(0)
        self.assertEqual(self.alerts.check(), 1)
        self.store.update(row['id'], {'lease_end': '2026-10-05'})
        self.assertEqual(self.alerts.snapshot()['alerts'], [])
        self.assertEqual(self.alerts.check(), 0)
        self.store.update(row['id'], {'lease_end': '2026-09-06'})
        self.assertEqual(self.alerts.check(), 1)  # Different saved date.
        self.store.update(row['id'], {'archived': True})
        self.now += dt.timedelta(days=1)
        self.assertEqual(self.alerts.check(), 0)
        self.assertEqual(self.sender.call_count, 2)

    def test_failed_delivery_retries_without_marking_sent(self):
        self.add(3)
        self.sender.side_effect = RuntimeError('Desktop unavailable')
        with self.assertRaises(RuntimeError):
            self.alerts.check()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM rental_notifications').fetchone()[0], 0)
        self.assertEqual(self.alerts.snapshot()['notifications']['error'], 'Desktop unavailable')
        self.sender.side_effect = None
        self.assertEqual(self.alerts.check(), 1)
        self.assertEqual(self.alerts.snapshot()['notifications']['error'], '')

    def test_disable_and_preview_do_not_change_dates_or_dedup(self):
        self.add(1)
        before = self.store.list()
        self.alerts.set_enabled(False)
        self.assertEqual(self.alerts.check(), 0)
        self.sender.assert_not_called()
        self.alerts.preview()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.store.list(), before)
        self.assertFalse(self.alerts.snapshot()['notifications']['enabled'])
        self.alerts.set_enabled(True)
        self.assertEqual(self.alerts.check(), 1)

    def test_bounded_batch_and_plaintext_names(self):
        for _ in range(8):
            self.add(-2, name='<b>VPS</b> & notes', notes='private-long-note', short_note='private-short-note')
        self.assertEqual(self.alerts.check(), 8)
        title, body, urgency = self.sender.call_args.args
        self.assertEqual(urgency, 'critical')
        self.assertIn('8 VPS', title)
        self.assertIn('&lt;b&gt;VPS&lt;/b&gt; &amp; notes', body)
        self.assertIn('3 more', body)
        self.assertNotIn('private-', body)
        self.assertEqual(self.alerts.check(), 0)

    def test_leap_day_and_offline_catch_up(self):
        self.now = dt.datetime(2028, 2, 22, 12, tzinfo=dt.timezone.utc)
        self.add(7)
        self.assertEqual(self.alerts.snapshot()['alerts'][0]['lease_end'], '2028-02-29')
        self.now += dt.timedelta(days=9)
        self.assertEqual(self.alerts.snapshot()['alerts'][0]['days'], -2)
        self.assertEqual(self.alerts.check(), 1)

    def test_unavailable_notification_program(self):
        self.add(0)
        with mock.patch('rental_alerts.shutil.which', return_value=None):
            alerts = RentalAlerts(self.store, clock=lambda: self.now)
            self.assertFalse(alerts.snapshot()['notifications']['available'])
            self.assertEqual(alerts.check(), 0)
            with self.assertRaisesRegex(RuntimeError, 'not installed'):
                alerts.preview()

    def test_desktop_sender_reports_failure_and_uses_no_shell(self):
        self.alerts.program = '/usr/bin/notify-send'
        with mock.patch('rental_alerts.subprocess.run', return_value=mock.Mock(returncode=0)) as run:
            self.alerts.desktop_notification('Reminder', 'Server name', 'normal')
            command = run.call_args.args[0]
            self.assertIn('--', command)
            self.assertNotIn('shell', run.call_args.kwargs)
        with mock.patch('rental_alerts.subprocess.run', return_value=mock.Mock(returncode=1)):
            with self.assertRaises(RuntimeError):
                self.alerts.desktop_notification('Reminder', 'Server name', 'normal')

    def test_quick_notes_roundtrip_independent_from_long_notes(self):
        row = self.add(notes='Detailed\nnotes', short_note='Initial short note')
        self.store.update(row['id'], {'short_note': '<i>Plain text</i> · заметка'})
        reopened = Store(self.home / 'data', self.home, import_existing=False, export=False)
        try:
            item = reopened.get(row['id'])
            self.assertEqual(item['short_note'], '<i>Plain text</i> · заметка')
            self.assertEqual(item['notes'], 'Detailed\nnotes')
        finally:
            reopened.db.close()
        self.store.update(row['id'], {'short_note': ''})
        self.assertEqual(self.store.get(row['id'])['short_note'], '')

    def test_quick_note_limits(self):
        self.assertEqual(validate_fields({'short_note': 'x' * 160})['short_note'], 'x' * 160)
        for value in ['x' * 161, 'two\nlines', 'two\tparts', 'a\u2028b', None, 123]:
            with self.subTest(value=str(value)[:30]), self.assertRaises(InputError):
                validate_fields({'short_note': value})

    def test_old_database_migrates_without_touching_existing_fields(self):
        row = self.add(lease_hint='09-18', notes='Existing notes')
        original = self.store.get(row['id'])
        original.pop('short_note')
        with self.store.db:
            self.store.db.execute('ALTER TABLE servers DROP COLUMN short_note')
        reopened = Store(self.home / 'data', self.home, import_existing=False, export=False)
        try:
            updated = reopened.get(row['id'])
            self.assertEqual(updated.pop('short_note'), '')
            self.assertEqual(updated, original)
        finally:
            reopened.db.close()


if __name__ == '__main__':
    unittest.main()
