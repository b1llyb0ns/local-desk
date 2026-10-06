import copy
import datetime
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_listeners
from local_port_inventory import LocalPortInventory


def socket_row(address='0.0.0.0', port=8080, protocol='tcp', pid=123, name='service'):
    endpoint = f'[{address}]:{port}' if ':' in address else f'{address}:{port}'
    state = 'LISTEN' if protocol == 'tcp' else 'UNCONN'
    owners = f' users:(("{name}",pid={pid},fd=3))' if pid else ''
    rows, skipped = local_listeners.parse_ss(f'{protocol} {state} 0 128 {endpoint} *:*{owners}')
    assert skipped == 0
    return rows[0]


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime.datetime(2026, 9, 5, 12, tzinfo=datetime.timezone.utc)
        self.rows = []
        self.overrides = {}
        self.collector = mock.Mock(side_effect=self.collect)
        self.inventory = LocalPortInventory(collector=self.collector, clock=lambda: self.time)

    def collect(self):
        return dict({'checked_at': self.time.isoformat(), 'state': 'ok', 'listeners': copy.deepcopy(self.rows),
                     'count': len(self.rows), 'process_visibility': 'visible', 'process_visibility_notice': '',
                     'exposure_notice': '', 'skipped_lines': 0, 'truncated': False, 'error': None}, **self.overrides)

    def advance(self):
        self.time += datetime.timedelta(seconds=60)

    def test_constructor_and_cached_reads_do_not_collect_or_schedule_work(self):
        self.collector.assert_not_called()
        for _ in range(3):
            result = self.inventory.cached_inventory()
            self.assertEqual(result['state'], 'pending')
            self.assertEqual(result['listeners'], [])
            self.assertTrue(result['cached'])
            self.assertFalse(result['refreshing'])
        self.collector.assert_not_called()
        for name in ('thread', 'start', 'stop', 'sender', 'notify_pending', 'snapshot', 'set_enabled', 'acknowledge'):
            self.assertFalse(hasattr(self.inventory, name), name)

    def test_explicit_refresh_collects_once_and_cache_is_independent(self):
        self.rows = [socket_row(), socket_row('127.0.0.1', 8787)]
        result = self.inventory.refresh()
        self.assertEqual(result['count'], 2)
        self.assertFalse(result['cached'])
        self.assertFalse(result['stale'])
        self.assertFalse(result['refreshing'])
        self.collector.assert_called_once()
        result['listeners'][0]['processes'].clear()
        cached = self.inventory.cached_inventory()
        self.assertEqual(cached['listeners'][0]['processes'], [{'name': 'service', 'pid': 123}])
        self.collector.assert_called_once()

    def test_changes_return_only_current_rows_without_events_or_history(self):
        self.rows = [socket_row()]
        self.inventory.refresh()
        self.advance()
        self.rows = [socket_row('::', 8443, name='web-tls'), socket_row(protocol='udp', port=5353)]
        self.rows[0].update(first_seen='2026-09-01', last_seen='2026-09-04', last_known_process_names=['old'])
        result = self.inventory.refresh()
        self.assertEqual(result['count'], 2)
        self.assertEqual({row['port'] for row in result['listeners']}, {8443, 5353})
        for field in ('watch', 'events', 'unread_count', 'notifications', 'history'):
            self.assertNotIn(field, result)
        for row in result['listeners']:
            for field in ('first_seen', 'last_seen', 'last_known_process_names'):
                self.assertNotIn(field, row)
        self.advance()
        self.rows = []
        self.assertEqual(self.inventory.refresh()['listeners'], [])

    def test_potential_public_highlight_is_tcp_only_and_not_a_reachability_claim(self):
        self.rows = [socket_row(), socket_row('::'), socket_row('8.8.8.8'), socket_row('127.0.0.1'),
                     socket_row('::1'), socket_row('192.168.1.2'), socket_row('::ffff:127.0.0.1'),
                     socket_row(protocol='udp'), socket_row('8.8.8.8', protocol='udp')]
        rows = self.inventory.refresh()['listeners']
        self.assertEqual([row['potential_public_tcp'] for row in rows], [True, True, True, False, False, False, False, False, False])
        self.assertTrue(all(row['internet_reachability'] == 'not_checked' for row in rows))

    def test_hidden_process_owner_does_not_reuse_old_names(self):
        self.rows = [socket_row(name='web')]
        self.inventory.refresh()
        self.rows = [socket_row(pid=None)]
        row = self.inventory.refresh()['listeners'][0]
        self.assertEqual(row['processes'], [])
        self.assertEqual(row['process_visibility'], 'unavailable')
        self.assertNotIn('last_known_process_names', row)

    def test_failed_or_partial_refresh_keeps_previous_complete_snapshot_as_stale(self):
        self.rows = [socket_row()]
        original = self.inventory.refresh()
        for fields in ({'state': 'error'}, {'state': 'partial'}, {'truncated': True}, {'skipped_lines': 1}, {'error': 'Unavailable'}):
            with self.subTest(fields=fields):
                self.advance()
                self.rows = []
                self.overrides = fields
                result = self.inventory.refresh()
                self.assertEqual(result['listeners'], original['listeners'])
                self.assertEqual(result['checked_at'], original['checked_at'])
                self.assertEqual(result['last_attempt_at'], self.time.isoformat())
                self.assertTrue(result['stale'])
                self.assertTrue(result['cached'])
                self.assertEqual(result['state'], 'error')
                self.assertIn('previous complete snapshot', result['error'])
        self.advance()
        self.overrides = {}
        recovered = self.inventory.refresh()
        self.assertEqual(recovered['count'], 0)
        self.assertEqual(recovered['state'], 'ok')
        self.assertFalse(recovered['stale'])
        self.assertIsNone(recovered['error'])

    def test_collector_exception_without_prior_result_is_explicit_error(self):
        self.collector.side_effect = RuntimeError('Unavailable')
        result = self.inventory.refresh()
        self.assertEqual(result['listeners'], [])
        self.assertEqual(result['state'], 'error')
        self.assertFalse(result['stale'])
        self.assertTrue(result['error'])

    def test_first_partial_snapshot_is_labelled_incomplete_without_inventing_history(self):
        self.rows = [socket_row()]
        self.overrides = {'state': 'partial', 'skipped_lines': 1}
        result = self.inventory.refresh()
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['state'], 'partial')
        self.assertFalse(result['stale'])
        self.assertIn('incomplete', result['error'])

    def test_restart_starts_with_empty_memory_cache(self):
        self.rows = [socket_row()]
        self.inventory.refresh()
        restarted = LocalPortInventory(collector=self.collector)
        self.assertEqual(restarted.cached_inventory()['state'], 'pending')
        self.assertEqual(restarted.cached_inventory()['listeners'], [])
        self.collector.assert_called_once()

    def test_concurrent_refreshes_share_cache_instead_of_running_parallel_collectors(self):
        self.rows = [socket_row()]
        original = self.inventory.refresh()
        entered, release = threading.Event(), threading.Event()
        def collect():
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return self.collect()
        self.collector.reset_mock()
        self.collector.side_effect = collect
        thread = threading.Thread(target=self.inventory.refresh)
        try:
            thread.start()
            self.assertTrue(entered.wait(timeout=1))
            cached = self.inventory.refresh()
            self.assertTrue(cached['cached'])
            self.assertTrue(cached['refreshing'])
            self.assertEqual(cached['listeners'], original['listeners'])
            self.collector.assert_called_once()
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.inventory.cached_inventory()['refreshing'])


if __name__ == '__main__':
    unittest.main()
