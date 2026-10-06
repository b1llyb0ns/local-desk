import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from country_flags import CountryFlags, lookup_countries, public_ip
from server import Store


class CountryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='countries-', dir=Path(__file__).resolve().parents[1] / 'tmp')
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.record = self.store.add({'name': 'Sydney', 'alias': 'au-1', 'host': '1.1.1.1'})
        self.now = 1000
        self.lookup = mock.Mock(return_value={'1.1.1.1': 'AU'})
        self.flags = CountryFlags(self.store, lookup=self.lookup, clock=lambda: self.now)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_only_public_literal_ips_are_looked_up(self):
        for host in ('127.0.0.1', '10.0.0.8', '::1', '192.0.2.8', 'host.example', 'invalid'):
            self.assertIsNone(public_ip(host))
        self.assertEqual(public_ip('1.1.1.1'), '1.1.1.1')
        self.store.add({'name': 'Local', 'alias': 'local-box', 'host': '10.0.0.8'})
        self.assertEqual(self.flags.snapshot(), {'1.1.1.1': 'AU'})
        self.lookup.assert_called_once_with(['1.1.1.1'])

    def test_cache_survives_restart_and_expires_after_thirty_days(self):
        self.flags.snapshot()
        restarted = CountryFlags(self.store, lookup=self.lookup, clock=lambda: self.now)
        self.assertEqual(restarted.snapshot(), {'1.1.1.1': 'AU'})
        self.assertEqual(self.lookup.call_count, 1)
        self.now += 30 * 86400 + 1
        self.flags.snapshot()
        self.assertEqual(self.lookup.call_count, 2)

    def test_new_ip_is_looked_up_without_reusing_old_country(self):
        self.flags.snapshot()
        self.store.update(self.record['id'], {'host': '8.8.8.8'})
        self.lookup.return_value = {'8.8.8.8': 'US'}
        self.assertEqual(self.flags.snapshot(), {'8.8.8.8': 'US'})
        self.lookup.assert_called_with(['8.8.8.8'])

    def test_outage_preserves_cached_flag_and_delays_retry(self):
        self.flags.snapshot()
        self.now += 30 * 86400 + 1
        self.lookup.side_effect = OSError('Unavailable')
        self.assertEqual(self.flags.snapshot(), {'1.1.1.1': 'AU'})
        self.flags.snapshot()
        self.assertEqual(self.lookup.call_count, 2)
        self.now += 3601
        self.flags.snapshot()
        self.assertEqual(self.lookup.call_count, 3)

    def test_unknown_country_has_no_flag_and_is_cached(self):
        self.lookup.return_value = {}
        self.assertEqual(self.flags.snapshot(), {})
        self.flags.snapshot()
        self.lookup.assert_called_once()

    def test_response_is_bounded_and_only_requested_country_codes_are_used(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps([
            {'ip': '1.1.1.1', 'country': 'AU'}, {'ip': '8.8.8.8', 'country': 'US'},
            {'ip': '1.1.1.1', 'country': '<b>AU</b>'}]).encode()
        with mock.patch('country_flags.build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(lookup_countries(['1.1.1.1']), {'1.1.1.1': 'AU'})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.country.is/')
            self.assertEqual(json.loads(request.data), ['1.1.1.1'])
            self.assertTrue(request.get_header('User-agent').startswith('Mozilla/'))
            response.read.assert_called_once_with(65537)
            response.read.return_value = b'x' * 65537
            with self.assertRaises(ValueError):
                lookup_countries(['1.1.1.1'])


if __name__ == '__main__':
    unittest.main()
