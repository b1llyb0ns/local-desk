import concurrent.futures
import datetime as dt
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from expenses import CBR_URL, Expenses, ExpenseError, MAX_RATE_BYTES, NoRedirects, amount, fetch_rate, legacy_cost, parse_rate
from server import Application, InputError, Store, make_server


XML = '''<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="05.09.2026" name="Курсы валют">
<Valute><CharCode>USD</CharCode><Nominal>10</Nominal><Value>925,1250</Value></Valute>
</ValCurs>'''.encode('cp1251')


class ExpenseFixtures(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='costs-', dir=directory)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.now = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
        self.fetcher = mock.Mock(return_value={'usd_rub': '100', 'date': '2026-09-05'})
        self.expenses = Expenses(self.store, fetcher=self.fetcher, clock=lambda: self.now)
        self.sequence = 0

    def tearDown(self):
        self.store.db.close()
        self.temporary.cleanup()

    def add_server(self, **values):
        self.sequence += 1
        return self.store.add(dict(name=f'Node {self.sequence}', alias=f'node-{self.sequence}',
                                   host=f'192.0.2.{self.sequence}', **values))


class ExpenseTests(ExpenseFixtures):
    def test_unambiguous_legacy_costs_need_period_confirmation(self):
        for text, value, currency in [('1400₽', '1400', 'RUB'), ('5$', '5', 'USD'),
                                      ('RUB 250,5', '250.5', 'RUB'), ('USD 0', '0', 'USD'),
                                      ('1 400 ₽', '1400', 'RUB'), ('1\u00a0400 ₽', '1400', 'RUB'),
                                      ('1\u202f400,50 ₽', '1400.5', 'RUB')]:
            row = self.add_server(price=text)
            self.assertEqual((row['cost_amount'], row['cost_currency']), (value, currency))
            self.assertEqual(row['price'], text)
            self.assertTrue(row['cost_review'])
        for text in ['', '10', '5$ / year', '$ 5 ₽', 'NaN$', '-5$', '1,400 ₽', '12 34 ₽']:
            row = legacy_cost(text)
            self.assertIsNone(row['cost_amount'])
            self.assertIsNone(row['cost_currency'])
        self.assertTrue(legacy_cost('10')['cost_review'])

    def test_schema_migration_preserves_legacy_and_explicit_clear_survives_restart(self):
        original = self.add_server(price='5$', notes='Existing notes')
        with self.store.db:
            for field in ('cost_amount', 'cost_currency', 'cost_period', 'cost_review'):
                self.store.db.execute('ALTER TABLE servers DROP COLUMN ' + field)
            self.store.db.execute("DELETE FROM metadata WHERE key='expenses_migrated_v1'")
        self.store.db.close()
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        row = self.store.get(original['id'])
        self.assertEqual(row['cost_amount'], '5')
        self.assertTrue(row['cost_review'])
        self.assertEqual(row['notes'], 'Existing notes')
        self.store.update(row['id'], {'cost_amount': None})
        self.store.db.close()
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        row = self.store.get(row['id'])
        self.assertIsNone(row['cost_amount'])
        self.assertEqual(row['price'], '5$')
        self.assertFalse(row['cost_review'])

    def test_amount_validation_and_partial_server_updates(self):
        for value in [True, False, None, 'NaN', 'Infinity', float('inf'), float('nan'), '-1', '1e3',
                      '1,00', '1.0000001', '1000000000001', {}, []]:
            with self.subTest(value=str(value)), self.assertRaises(ExpenseError):
                amount(value)
        self.assertEqual(amount('0002.500000'), '2.5')
        row = self.add_server(cost_amount='5.25', cost_currency='USD', cost_period='yearly')
        changed = self.store.update(row['id'], {'cost_amount': '6'})
        self.assertEqual(changed['cost_currency'], 'USD')
        self.assertEqual(changed['cost_amount'], '6')
        for payload in [{'cost_currency': None}, {'cost_currency': True}, {'cost_period': []}, {'cost_review': 1}]:
            with self.subTest(payload=payload), self.assertRaises(InputError):
                self.store.update(row['id'], payload)
        with self.assertRaises(InputError):
            self.add_server(cost_amount='4')
        cleared = self.store.update(row['id'], {'cost_amount': None, 'cost_currency': None})
        self.assertIsNone(cleared['cost_amount'])

    def test_recurring_totals_normalization_archives_and_one_time(self):
        self.add_server(cost_amount='10', cost_currency='USD', cost_period='monthly')
        self.add_server(cost_amount='300', cost_currency='RUB', cost_period='quarterly')
        self.add_server(cost_amount='9999', cost_currency='USD', archived=True)
        self.expenses.add({'name': 'Subscription', 'amount': '120', 'currency': 'USD', 'period': 'yearly'})
        self.expenses.add({'name': 'Equipment', 'amount': '50', 'currency': 'RUB', 'period': 'once'})
        snapshot = self.expenses.snapshot(refresh=True)
        self.assertEqual(len(snapshot['servers']), 2)
        self.assertEqual(snapshot['totals']['all']['monthly'], '2100.00')
        self.assertEqual(snapshot['totals']['all']['yearly'], '25200.00')
        self.assertEqual(snapshot['totals']['all']['once'], '50.00')
        self.assertTrue(snapshot['totals']['all']['complete'])
        self.expenses.set_currency({'currency': 'USD'})
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '21.00')
        self.assertEqual(self.expenses.snapshot()['totals']['all']['once'], '0.50')
        restarted = Expenses(self.store, fetcher=self.fetcher, clock=lambda: self.now)
        self.assertEqual(restarted.snapshot()['currency'], 'USD')
        self.assertEqual(restarted.snapshot()['fx']['usd_rub'], '100')

    def test_missing_prices_and_missing_fx_never_become_full_zero_totals(self):
        row = self.add_server()
        snapshot = self.expenses.snapshot()
        total = snapshot['totals']['all']
        self.assertIsNone(total['monthly'])
        self.assertIsNone(total['yearly'])
        self.assertEqual(total['unknown_count'], 1)
        self.assertFalse(total['complete'])
        self.assertEqual(total['once'], '0.00')
        self.store.update(row['id'], {'cost_amount': '5', 'cost_currency': 'USD'})
        total = self.expenses.snapshot()['totals']['all']
        self.assertIsNone(total['monthly'])
        self.assertEqual(total['native']['USD']['monthly'], '5.00')
        self.assertFalse(total['complete'])
        self.expenses.set_currency({'currency': 'USD'})
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '5.00')
        self.store.update(row['id'], {'cost_amount': '0'})
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '0.00')
        self.fetcher.assert_not_called()

    def test_reviewed_import_is_estimate_and_confirming_clears_warning(self):
        row = self.add_server(price='500₽')
        total = self.expenses.snapshot()['totals']['all']
        self.assertEqual(total['monthly'], '500.00')
        self.assertFalse(total['complete'])
        self.assertEqual(total['review_count'], 1)
        self.store.update(row['id'], {'cost_period': 'yearly'})
        total = self.expenses.snapshot()['totals']['all']
        self.assertEqual(total['monthly'], '41.67')
        self.assertEqual(total['yearly'], '500.00')
        self.assertTrue(total['complete'])

    def test_known_subtotal_excludes_unknown_prices_but_requires_conversion(self):
        self.add_server()
        self.add_server(cost_amount='5', cost_currency='USD', cost_period='monthly')
        self.add_server(cost_amount='600', cost_currency='RUB', cost_period='yearly')
        total = self.expenses.snapshot()['totals']['all']
        self.assertIsNone(total['monthly'])
        self.assertIsNone(total['known']['monthly'])
        total = self.expenses.snapshot(refresh=True)['totals']['all']
        self.assertIsNone(total['monthly'])
        self.assertFalse(total['complete'])
        self.assertEqual(total['known']['monthly'], '550.00')
        self.assertEqual(total['known']['yearly'], '6600.00')
        self.assertEqual(total['unknown_count'], 1)

    def test_crud_notes_and_invalid_fields(self):
        identifier = self.expenses.add({'name': 'Cloud storage', 'amount': '2.5', 'currency': 'USD', 'period': 'monthly', 'notes': 'Annual review'})
        self.expenses.update(identifier, {'amount': '30', 'period': 'yearly'})
        row = self.expenses.snapshot()['items'][0]
        self.assertIsNone(row['next_charge_date'])
        self.assertEqual((row['name'], row['amount'], row['period'], row['notes']),
                         ('Cloud storage', '30', 'yearly', 'Annual review'))
        for payload in [{'amount': None}, {'name': ''}, {'currency': 'GBP'}, {'period': 'weekly'}, {'notes': 'x' * 4001}, {'id': identifier}]:
            with self.subTest(payload=payload), self.assertRaises(ExpenseError):
                self.expenses.update(identifier, payload)
        self.expenses.delete(identifier)
        self.assertEqual(self.expenses.snapshot()['items'], [])
        with self.assertRaises(KeyError):
            self.expenses.delete(identifier)

    def test_payment_date_migration_preserves_existing_expenses_and_is_idempotent(self):
        identifier = self.expenses.add({'name': 'Cloud storage', 'amount': '30', 'currency': 'USD',
                                        'period': 'yearly', 'notes': 'Annual review'})
        before = dict(self.store.db.execute('SELECT * FROM expenses WHERE id=?', (identifier,)).fetchone())
        before.pop('next_charge_date')
        with self.store.db:
            self.store.db.execute('ALTER TABLE expenses DROP COLUMN next_charge_date')
        self.store.db.close()
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        after = dict(self.store.db.execute('SELECT * FROM expenses WHERE id=?', (identifier,)).fetchone())
        self.assertIsNone(after.pop('next_charge_date'))
        self.assertEqual(after, before)
        self.expenses = Expenses(self.store, fetcher=self.fetcher, clock=lambda: self.now)
        self.expenses.update(identifier, {'next_charge_date': '2026-10-03'})
        self.store.db.close()
        self.store = Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.expenses = Expenses(self.store, fetcher=self.fetcher, clock=lambda: self.now)
        self.assertEqual(self.expenses.snapshot()['items'][0]['next_charge_date'], '2026-10-03')
        columns = [row['name'] for row in self.store.db.execute('PRAGMA table_info(expenses)')]
        self.assertEqual(columns.count('next_charge_date'), 1)
        self.fetcher.assert_not_called()

    def test_payment_date_roundtrip_clear_partial_updates_and_no_automatic_roll_forward(self):
        identifier = self.expenses.add({'name': 'Cloud storage', 'amount': '12', 'currency': 'USD',
                                        'period': 'monthly', 'next_charge_date': '2026-10-03'})
        self.assertEqual(self.expenses.snapshot()['items'][0]['next_charge_date'], '2026-10-03')
        self.expenses.update(identifier, {'notes': 'Annual review', 'period': 'yearly'})
        row = self.expenses.snapshot()['items'][0]
        self.assertEqual(row['next_charge_date'], '2026-10-03')
        self.assertEqual(row['notes'], 'Annual review')
        for empty in [None, '']:
            self.expenses.update(identifier, {'next_charge_date': '2026-10-03'})
            self.expenses.update(identifier, {'next_charge_date': empty})
            row = self.expenses.snapshot()['items'][0]
            self.assertIsNone(row['next_charge_date'])
            self.assertEqual((row['amount'], row['period'], row['notes']), ('12', 'yearly', 'Annual review'))
        self.expenses.update(identifier, {'next_charge_date': '2026-01-31'})
        self.now += dt.timedelta(days=400)
        self.assertEqual(self.expenses.snapshot()['items'][0]['next_charge_date'], '2026-01-31')
        self.fetcher.assert_not_called()

    def test_payment_date_validation_and_atomic_rejection(self):
        fields = {'name': 'Cloud storage', 'amount': '12', 'currency': 'USD', 'period': 'monthly'}
        identifier = self.expenses.add(dict(fields, next_charge_date=None))
        for value in ['2000-01-01', '2000-02-29', '2028-02-29', '2200-12-31']:
            self.expenses.update(identifier, {'next_charge_date': value})
            self.assertEqual(self.expenses.snapshot()['items'][0]['next_charge_date'], value)
        original = self.expenses.snapshot()['items'][0]
        invalid = ['1999-12-31', '2201-01-01', '2100-02-29', '2026-02-30', '2026-13-01',
                   '2026-00-01', '2026-01-00', '2026-2-03', '2026-02-3', '20260203',
                   '03.10.2026', '2026-10-03T00:00:00Z', ' 2026-10-03 ', '\x00',
                   True, False, 0, 12, [], {}, float('nan')]
        for value in invalid:
            with self.subTest(value=str(value)):
                with self.assertRaises(ExpenseError):
                    self.expenses.add(dict(fields, next_charge_date=value))
                with self.assertRaises(ExpenseError):
                    self.expenses.update(identifier, {'name': 'Storage', 'next_charge_date': value})
                self.assertEqual(self.expenses.snapshot()['items'], [original])

    def test_payment_dates_do_not_change_recurring_or_one_time_totals(self):
        self.expenses.set_currency({'currency': 'USD'})
        identifiers = [self.expenses.add({'name': period, 'amount': '12', 'currency': 'USD', 'period': period})
                       for period in ['monthly', 'quarterly', 'yearly', 'once']]
        before = self.expenses.snapshot()['totals']
        self.assertTrue(before['items']['complete'])
        for identifier, date in zip(identifiers, ['2026-01-31', '2026-09-06', '2027-01-31', '2026-09-08']):
            self.expenses.update(identifier, {'next_charge_date': date})
        after = self.expenses.snapshot()
        self.assertEqual(after['totals'], before)
        self.assertEqual(after['totals']['items']['monthly'], '17.00')
        self.assertEqual(after['totals']['items']['once'], '12.00')
        once = next(row for row in after['items'] if row['period'] == 'once')
        self.assertEqual(once['next_charge_date'], '2026-09-08')
        self.assertIsNone(once['monthly'])
        self.fetcher.assert_not_called()

    def test_cbr_bytes_nominal_and_xml_validation(self):
        self.assertEqual(parse_rate(XML), {'usd_rub': '92.5125', 'date': '2026-09-05'})
        for data in [b'', b'x' * (MAX_RATE_BYTES + 1), b'<!DOCTYPE a><ValCurs/>',
                     b'<!ENTITY a "b"><ValCurs/>', b'\x00', b'<ValCurs>',
                     XML.replace(b'925,1250', b'NaN'), XML.replace(b'<Nominal>10', b'<Nominal>0'),
                     XML.replace(b'05.09.2026', b'31.02.2026'), XML.replace(b'USD', b'EUR')]:
            with self.subTest(data=data[:40]), self.assertRaises(ExpenseError):
                parse_rate(data)

    def test_eur_feed_nominal_validation_and_legacy_price(self):
        euro = b'<Valute><CharCode>EUR</CharCode><Nominal>10</Nominal><Value>1100,0000</Value></Valute>'
        data = XML.replace(b'</ValCurs>', euro + b'</ValCurs>')
        self.assertEqual(parse_rate(data)['eur_rub'], '110')
        for broken in (data.replace(b'1100,0000', b'NaN'),
                       XML.replace(b'</ValCurs>', euro + euro + b'</ValCurs>')):
            with self.assertRaises(ExpenseError):
                parse_rate(broken)
        for price in ('€8.99', '8,99 EUR'):
            parsed = legacy_cost(price)
            self.assertEqual((parsed['cost_amount'], parsed['cost_currency']), ('8.99', 'EUR'))

    def test_eur_costs_convert_in_all_display_currencies_and_survive_restart(self):
        self.fetcher.return_value = {'usd_rub': '100', 'eur_rub': '110', 'date': '2026-09-05'}
        row = self.add_server(cost_amount='8.99', cost_currency='EUR', cost_period='monthly')
        self.expenses.add({'name': 'Storage', 'amount': '10', 'currency': 'USD', 'period': 'monthly'})
        self.expenses.refresh()
        for currency, server_total, all_total in [('RUB', '988.90', '1988.90'),
                                                   ('USD', '9.89', '19.89'),
                                                   ('EUR', '8.99', '18.08')]:
            with self.subTest(currency=currency):
                self.expenses.set_currency({'currency': currency})
                snapshot = self.expenses.snapshot()
                self.assertEqual(snapshot['totals']['servers']['monthly'], server_total)
                self.assertEqual(snapshot['totals']['all']['monthly'], all_total)
                self.assertTrue(snapshot['totals']['all']['complete'])
        self.assertEqual(self.store.get(row['id'])['cost_amount'], '8.99')
        restarted = Expenses(self.store, fetcher=self.fetcher, clock=lambda: self.now)
        self.assertEqual(restarted.snapshot()['currency'], 'EUR')
        self.assertEqual(restarted.snapshot()['fx']['eur_rub'], '110')

    def test_missing_eur_rate_keeps_native_cost_without_incorrect_conversion(self):
        self.add_server(cost_amount='8.99', cost_currency='EUR')
        snapshot = self.expenses.snapshot(refresh=True)
        self.assertIsNone(snapshot['totals']['all']['monthly'])
        self.assertIsNone(snapshot['servers'][0]['converted_amount'])
        self.assertEqual(snapshot['totals']['all']['native']['EUR']['monthly'], '8.99')
        self.expenses.set_currency({'currency': 'EUR'})
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '8.99')

    def test_eur_and_usd_rates_are_updated_together(self):
        self.fetcher.return_value = {'usd_rub': '100', 'eur_rub': '110', 'date': '2026-09-05'}
        self.expenses.refresh()
        self.fetcher.return_value = {'usd_rub': '120', 'eur_rub': 'NaN', 'date': '2026-09-06'}
        self.expenses.refresh(force=True)
        fx = self.expenses.snapshot()['fx']
        self.assertEqual((fx['usd_rub'], fx['eur_rub'], fx['date']), ('100', '110', '2026-09-05'))
        self.assertTrue(fx['stale'])
        self.fetcher.return_value = {'usd_rub': '120', 'date': '2026-09-06'}
        self.expenses.refresh(force=True)
        self.assertIsNone(self.expenses.snapshot()['fx']['eur_rub'])

    def test_old_usd_only_cache_refreshes_once_after_currency_upgrade(self):
        self.expenses._write('expenses_fx', {'usd_rub': '100', 'date': '2026-09-05',
                                           'checked_at': self.now.isoformat(), 'error': ''})
        self.fetcher.return_value = {'usd_rub': '100', 'eur_rub': '110', 'date': '2026-09-05'}
        self.expenses.refresh()
        self.expenses.refresh()
        self.assertEqual(self.fetcher.call_count, 1)
        self.assertEqual(self.expenses.snapshot()['fx']['eur_rub'], '110')

    def test_rate_transport_is_fixed_bounded_and_does_not_follow_redirects(self):
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        response.read.return_value = XML
        with mock.patch('expenses.build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(fetch_rate()['usd_rub'], '92.5125')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, CBR_URL)
            self.assertEqual(request.get_header('Accept-encoding'), 'identity')
            self.assertEqual(opener.return_value.open.call_args.kwargs, {'timeout': 10})
            response.read.assert_called_once_with(MAX_RATE_BYTES + 1)
        with self.assertRaises(ExpenseError):
            NoRedirects().redirect_request(None, None, 302, '', {}, 'https://example.com/')

    def test_invalid_refresh_keeps_last_good_rate(self):
        self.expenses.refresh()
        for rate in [{'usd_rub': 'NaN', 'date': '2026-09-05'},
                     {'usd_rub': '-1', 'date': '2026-09-05'},
                     {'usd_rub': '200', 'date': '2026-12-01'}]:
            self.fetcher.return_value = rate
            self.expenses.refresh(force=True)
            snapshot = self.expenses.snapshot()['fx']
            self.assertEqual(snapshot['usd_rub'], '100')
            self.assertTrue(snapshot['stale'])
            self.assertTrue(snapshot['error'])

    def test_lazy_daily_refresh_failure_cache_and_forced_retry(self):
        for _ in range(3):
            self.expenses.snapshot()
        self.fetcher.assert_not_called()
        self.expenses.snapshot(refresh=True)
        self.expenses.snapshot(refresh=True)
        self.assertEqual(self.fetcher.call_count, 1)
        self.assertFalse(self.expenses.snapshot()['fx']['stale'])
        self.now += dt.timedelta(days=1)
        self.assertTrue(self.expenses.snapshot()['fx']['stale'])
        self.fetcher.side_effect = OSError('Offline')
        failed = self.expenses.snapshot(refresh=True)['fx']
        self.assertEqual(failed['usd_rub'], '100')
        self.assertEqual(failed['date'], '2026-09-05')
        self.assertEqual(failed['checked_at'], self.now.isoformat())
        self.assertTrue(failed['stale'])
        self.assertTrue(failed['error'])
        self.expenses.snapshot(refresh=True)
        self.assertEqual(self.fetcher.call_count, 2)
        self.fetcher.side_effect = None
        self.expenses.refresh(force=True)
        self.assertEqual(self.fetcher.call_count, 3)
        self.assertEqual(self.expenses.snapshot()['fx']['error'], '')

    def test_malformed_cache_and_aged_rate_remain_explicit(self):
        for saved in [[], None, 'USD', {'usd_rub': 'NaN', 'date': '2026-09-05'},
                      {'usd_rub': '100', 'date': 'bad'}, {'usd_rub': True, 'date': '2026-09-05'}]:
            self.expenses._write('expenses_fx', saved)
            snapshot = self.expenses.snapshot()
            self.assertIsNone(snapshot['fx']['usd_rub'])
            self.assertTrue(snapshot['fx']['stale'])
        self.expenses._write('expenses_currency', [])
        self.assertEqual(self.expenses.snapshot()['currency'], 'RUB')
        self.expenses._write('expenses_fx', {'usd_rub': '100', 'date': '2026-08-01',
                                           'checked_at': self.now.isoformat(), 'error': ''})
        snapshot = self.expenses.snapshot()['fx']
        self.assertEqual(snapshot['usd_rub'], '100')
        self.assertEqual(snapshot['date'], '2026-08-01')
        self.assertTrue(snapshot['stale'])

    def test_warm_snapshot_avoids_recalculation_and_invalidates_after_edits_and_day_change(self):
        row = self.add_server(cost_amount='100', cost_currency='RUB')
        first = self.expenses.snapshot()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        with mock.patch.object(self.expenses, '_build_snapshot', side_effect=AssertionError('Unexpected recalculation')):
            second = self.expenses.snapshot()
        self.store.db.set_trace_callback(None)
        self.assertEqual(first, second)
        self.assertEqual(statements, ['PRAGMA data_version'])
        second['totals']['all']['monthly'] = '1.00'
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '100.00')
        self.store.update(row['id'], {'cost_amount': '200'})
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '200.00')
        self.expenses.refresh()
        self.assertFalse(self.expenses.snapshot()['fx']['stale'])
        self.now += dt.timedelta(days=1)
        self.assertTrue(self.expenses.snapshot()['fx']['stale'])

    def test_trash_restore_and_delete_refresh_warm_totals(self):
        row = self.add_server(cost_amount='100', cost_currency='RUB')
        self.expenses.add({'name': 'Storage', 'amount': '20', 'currency': 'RUB', 'period': 'monthly'})
        self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '120.00')
        # Another open panel shares the database, not this snapshot cache.
        other = Store(self.home / 'data', self.home, import_existing=False, export=False)
        try:
            other.update(row['id'], {'archived': True})
            archived = self.expenses.snapshot()
            self.assertEqual(archived['servers'], [])
            self.assertEqual(archived['totals']['servers']['monthly'], '0.00')
            self.assertEqual(archived['totals']['all']['monthly'], '20.00')
            self.assertEqual(len(archived['items']), 1)
            other.update(row['id'], {'archived': False})
            self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '120.00')
            other.update(row['id'], {'archived': True})
            other.delete(row['id'], row['alias'])
            self.assertEqual(self.expenses.snapshot()['totals']['all']['monthly'], '20.00')
        finally:
            other.db.close()

    def test_simultaneous_manual_refresh_uses_one_request(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set()
            release.wait(2)
            return {'usd_rub': '100', 'date': '2026-09-05'}
        self.fetcher.side_effect = fetch
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.expenses.refresh, True)
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.expenses.refresh, True)
            try:
                with self.assertRaises(concurrent.futures.TimeoutError):
                    second.result(timeout=0.05)
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(self.fetcher.call_count, 1)


class ExpenseHttpTests(ExpenseFixtures):
    def setUp(self):
        super().setUp()
        engine = mock.Mock(fingerprint='SHA256:PUBLIC')
        engine.key_options.return_value = []
        self.app = Application(self.store, engine)
        self.app.expenses = self.expenses
        self.http = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.app.pool.shutdown(wait=True)
        super().tearDown()

    def request(self, path, method='GET', payload=None, trusted=True):
        headers = {'Content-Type': 'application/json'}
        if trusted:
            headers['X-VPS-CSRF'] = self.app.csrf
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(payload or {}), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_expense_http_crud_and_csrf(self):
        payload = {'name': 'Storage', 'amount': '12', 'currency': 'USD', 'period': 'yearly'}
        self.assertEqual(self.request('/api/expenses', 'POST', payload, trusted=False)[0], 403)
        status, created = self.request('/api/expenses', 'POST', payload)
        self.assertEqual(status, 201)
        identifier = created['items'][0]['id']
        self.assertEqual(self.request('/api/expenses/' + identifier, 'PATCH', {'amount': True})[0], 400)
        self.assertEqual(self.request('/api/expenses/' + identifier, 'PATCH', {'amount': '24'})[0], 200)
        status, changed = self.request('/api/expenses/settings', 'PATCH', {'currency': 'USD'})
        self.assertEqual((status, changed['currency']), (200, 'USD'))
        self.assertEqual(self.request('/api/expenses/' + identifier, 'DELETE')[0], 200)
        self.assertEqual(self.request('/api/expenses/' + identifier, 'DELETE')[0], 404)
        self.fetcher.assert_not_called()

    def test_payment_date_http_create_update_clear_and_snapshot(self):
        payload = {'name': 'Storage', 'amount': '12', 'currency': 'USD', 'period': 'yearly',
                   'next_charge_date': '2026-10-03'}
        self.assertEqual(self.request('/api/expenses', 'POST', dict(payload, next_charge_date=True))[0], 400)
        status, created = self.request('/api/expenses', 'POST', payload)
        self.assertEqual(status, 201)
        self.assertEqual(created['items'][0]['next_charge_date'], '2026-10-03')
        path = '/api/expenses/' + created['items'][0]['id']
        self.assertEqual(self.request(path, 'PATCH', {'next_charge_date': '2026-02-30'})[0], 400)
        status, changed = self.request(path, 'PATCH', {'notes': 'Annual review'})
        self.assertEqual(status, 200)
        self.assertEqual(changed['items'][0]['next_charge_date'], '2026-10-03')
        status, bootstrap = self.request('/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap['expenses']['items'][0]['next_charge_date'], '2026-10-03')
        for empty in [None, '']:
            self.request(path, 'PATCH', {'next_charge_date': '2026-10-03'})
            status, changed = self.request(path, 'PATCH', {'next_charge_date': empty})
            self.assertEqual(status, 200)
            self.assertIsNone(changed['items'][0]['next_charge_date'])
        self.fetcher.assert_not_called()

    def test_bootstrap_and_server_poll_do_not_refresh_rates(self):
        for path in ['/api/bootstrap', '/api/servers', '/api/desktop-updates/status']:
            status, _ = self.request(path)
            self.assertEqual(status, 200)
        self.fetcher.assert_not_called()
        self.assertEqual(self.request('/api/expenses')[0], 200)
        self.assertEqual(self.fetcher.call_count, 1)
        self.assertEqual(self.request('/api/expenses/rates/refresh', 'POST')[0], 200)
        self.assertEqual(self.fetcher.call_count, 2)

    def test_burp_weekly_schedule_cannot_be_enabled(self):
        self.assertEqual(self.request('/api/burp/settings', 'POST', {'weekly': True})[0], 400)
        self.assertEqual(self.request('/api/burp/settings', 'POST', {'weekly': False})[0], 202)


if __name__ == '__main__':
    unittest.main()
