import tempfile
from pathlib import Path
import unittest
from unittest import mock

import server
import ssh_engine
from test_panel import FakeEngine, SNAPSHOT


class ConnectionEngineTests(unittest.TestCase):
    def test_measures_successful_authenticated_connection(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(return_value=(0, '', ''))
        with mock.patch.object(ssh_engine.time, 'monotonic', side_effect=[10, 10.025]):
            self.assertEqual(engine.check({'host': '192.0.2.8'}), 25)
        engine.run.assert_called_once_with({'host': '192.0.2.8'}, 'true', timeout=15, ipqos=None)

    def test_timeout_retries_once_and_saves_working_transport(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(side_effect=[ssh_engine.SSHError('Timed out', 'timeout'), (0, '', '')])
        record = {'host': '192.0.2.8'}
        with mock.patch.object(ssh_engine.time, 'monotonic', side_effect=[10, 25, 25.04]):
            self.assertEqual(engine.check(record), 40)
        self.assertEqual(record['metrics_ipqos'], 'ef')
        self.assertEqual(engine.run.call_count, 2)
        self.assertEqual(engine.run.call_args.kwargs['ipqos'], 'ef')

    def test_no_retry_or_access_changes_for_rejected_key(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(return_value=(255, '', 'Permission denied (publickey).'))
        with self.assertRaises(ssh_engine.SSHError) as caught:
            engine.check({'host': '192.0.2.8'})
        self.assertEqual(caught.exception.kind, 'credentials_needed')
        self.assertEqual(engine.run.call_count, 1)

    def test_learned_transport_timeout_is_not_retried(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(side_effect=ssh_engine.SSHError('Timed out', 'timeout'))
        with self.assertRaises(ssh_engine.SSHError):
            engine.check({'host': '192.0.2.8', 'metrics_ipqos': 'ef'})
        self.assertEqual(engine.run.call_count, 1)


class ConnectionJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ssh-check-', dir=Path(__file__).resolve().parents[1] / 'tmp')
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.record = self.store.add({'name': 'Germany', 'alias': 'de-1', 'host': '192.0.2.8'})
        self.engine = FakeEngine()
        self.engine.check = mock.Mock(return_value=42.5)
        self.engine.collect = mock.Mock(side_effect=AssertionError('Availability check collected metrics'))
        self.app = server.Application(self.store, self.engine)

    def tearDown(self):
        self.app.pool.shutdown(wait=True)
        self.store.db.close()
        self.tmp.cleanup()

    def check(self):
        self.app.jobs['connection'] = {'id': 'connection', 'server_id': self.record['id'], 'kind': 'check', 'state': 'queued'}
        self.app.busy[self.record['id']] = 'connection'
        self.app.work('connection', {})

    def test_check_preserves_snapshot_age_history_and_records_latency(self):
        self.store.snapshot(self.record['id'], SNAPSHOT)
        before = self.store.get(self.record['id'])
        self.check()
        after = self.store.get(self.record['id'])
        self.assertEqual(after['state'], 'ready')
        self.assertEqual(after['ssh_latency_ms'], 42.5)
        self.assertTrue(after['ssh_checked_at'])
        for key in ('snapshot', 'last_seen', 'history'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.app.jobs['connection']['state'], 'done')
        self.assertNotIn(self.record['id'], self.app.busy)
        self.engine.collect.assert_not_called()

    def test_failed_check_clears_old_latency_without_removing_snapshot(self):
        self.store.snapshot(self.record['id'], SNAPSHOT)
        self.store.update(self.record['id'], {'ssh_latency_ms': 40}, internal=True)
        self.engine.check.side_effect = ssh_engine.SSHError('Key not accepted', 'credentials_needed')
        self.check()
        after = self.store.get(self.record['id'])
        self.assertEqual(after['state'], 'credentials_needed')
        self.assertIsNone(after['ssh_latency_ms'])
        self.assertTrue(after['ssh_checked_at'])
        self.assertIsNotNone(after['snapshot'])
        self.assertEqual(self.app.jobs['connection']['state'], 'error')

    def test_all_excludes_trash_skips_busy_and_deduplicates_checks(self):
        self.store.add({'name': 'Archived', 'alias': 'de-2', 'host': '192.0.2.9', 'archived': True})
        busy = self.store.add({'name': 'Installing', 'alias': 'de-3', 'host': '192.0.2.10'})
        self.app.jobs['install'] = {'id': 'install', 'kind': 'onboard', 'state': 'running'}
        self.app.busy[busy['id']] = 'install'
        with mock.patch.object(self.app.pool, 'submit') as submit:
            first = self.app.check_all()
            second = self.app.check_all()
        self.assertEqual(first['skipped'], [busy['id']])
        self.assertEqual(len(first['jobs']), 1)
        self.assertEqual(first['jobs'][0]['id'], second['jobs'][0]['id'])
        self.assertEqual(submit.call_count, 1)

    def test_connection_change_clears_latency_and_check_date(self):
        self.check()
        updated = self.store.update(self.record['id'], {'host': '192.0.2.9'})
        self.assertIsNone(updated['ssh_latency_ms'])
        self.assertIsNone(updated['ssh_checked_at'])


if __name__ == '__main__':
    unittest.main()
