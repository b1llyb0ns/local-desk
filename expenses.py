"""Local expenses and lazy, cached official USD/EUR reference rates in RUB."""
import datetime as dt
import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import re
import threading
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
import xml.etree.ElementTree as ET


CBR_URL = 'https://www.cbr.ru/scripts/XML_daily.asp'
MAX_RATE_BYTES = 256 * 1024
CURRENCIES = {'USD', 'EUR', 'RUB'}
PERIODS = {'monthly', 'quarterly', 'yearly', 'once'}
COST_FIELDS = {'cost_amount', 'cost_currency', 'cost_period', 'cost_review'}


class ExpenseError(ValueError):
    pass


def decimal_text(value):
    return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ExpenseError('Enter a non-negative cost amount.')
    text = str(value).strip()
    if len(text) > 40 or not re.fullmatch(r'\d+(?:\.\d{1,6})?', text):
        raise ExpenseError('Use a non-negative amount with up to six decimal places.')
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ExpenseError('Invalid cost amount.')
    if not number.is_finite() or number > Decimal('1000000000000'):
        raise ExpenseError('Cost amount is too large.')
    return decimal_text(number)


def charge_date(value):
    if value is None or value == '':
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        date = dt.date.fromisoformat(value)
        if not 2000 <= date.year <= 2200:
            raise ValueError()
    except ValueError:
        raise ExpenseError('Choose a full payment date between 2000 and 2200.')
    return value


def cost_fields(payload):
    result = {}
    for key in COST_FIELDS & payload.keys():
        value = payload[key]
        if key == 'cost_amount':
            result[key] = None if value is None else amount(value)
        elif key == 'cost_currency':
            if value is not None and (not isinstance(value, str) or value not in CURRENCIES):
                raise ExpenseError('Choose USD, EUR or RUB.')
            result[key] = value
        elif key == 'cost_period':
            if not isinstance(value, str) or value not in PERIODS:
                raise ExpenseError('Choose monthly, quarterly, yearly or once.')
            result[key] = value
        else:
            if not isinstance(value, bool):
                raise ExpenseError('Invalid cost review value.')
            result[key] = int(value)
    if result and 'cost_review' not in result:
        result['cost_review'] = 0
    return result


def validate_cost_record(record):
    if record.get('cost_amount') is not None and record.get('cost_currency') not in CURRENCIES:
        raise ExpenseError('Choose a currency for the cost amount.')


def legacy_cost(price):
    """Only an unambiguous currency is inferred; billing frequency needs review."""
    price = str(price or '').strip()
    result = {'cost_amount': None, 'cost_currency': None,
              'cost_period': 'monthly', 'cost_review': int(bool(price))}
    match = re.fullmatch(r'(?:(USD|EUR|RUB|\$|€|₽)\s*)?((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(USD|EUR|RUB|\$|€|₽)?', price, re.I)
    if not match or bool(match[1]) == bool(match[3]):
        return result
    currency = (match[1] or match[3]).upper()
    try:
        result['cost_amount'] = amount(re.sub(r'[ \u00a0\u202f]', '', match[2]).replace(',', '.'))
    except ExpenseError:
        return result
    result['cost_currency'] = {'$': 'USD', '€': 'EUR', '₽': 'RUB'}.get(currency, currency)
    return result


def initialize(store):
    with store.lock, store.db:
        columns = {row['name'] for row in store.db.execute('PRAGMA table_info(servers)')}
        for name, definition in [('cost_amount', 'TEXT'), ('cost_currency', 'TEXT'),
                                 ('cost_period', "TEXT NOT NULL DEFAULT 'monthly'"),
                                 ('cost_review', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                store.db.execute('ALTER TABLE servers ADD COLUMN ' + name + ' ' + definition)
        store.db.execute('''CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, amount TEXT NOT NULL,
            currency TEXT NOT NULL, period TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        if 'next_charge_date' not in {row['name'] for row in store.db.execute('PRAGMA table_info(expenses)')}:
            store.db.execute('ALTER TABLE expenses ADD COLUMN next_charge_date TEXT')
        if not store.db.execute("SELECT 1 FROM metadata WHERE key='expenses_migrated_v1'").fetchone():
            for row in store.db.execute('SELECT id,price,cost_amount,cost_currency FROM servers').fetchall():
                if row['cost_amount'] is None and row['cost_currency'] is None:
                    fields = legacy_cost(row['price'])
                    store.db.execute('UPDATE servers SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                                     [*fields.values(), row['id']])
            store.db.execute("INSERT INTO metadata VALUES ('expenses_migrated_v1','1')")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ExpenseError('The official rate feed redirected; cached rate retained.')


def fetch_rate():
    request = Request(CBR_URL, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
        'Accept': 'application/xml,text/xml', 'Accept-Encoding': 'identity'})
    with build_opener(NoRedirects()).open(request, timeout=10) as response:
        if response.status != 200:
            raise ExpenseError('The official rate feed returned an unexpected status.')
        data = response.read(MAX_RATE_BYTES + 1)
    return parse_rate(data)


def parse_rate(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_RATE_BYTES:
        raise ExpenseError('The official rate feed is empty or oversized.')
    if b'\x00' in data or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise ExpenseError('Unsupported XML in the official rate feed.')
    try:
        root = ET.fromstring(data)  # Keep bytes so the feed declaration controls decoding.
        if root.tag != 'ValCurs':
            raise ValueError()
        date = dt.datetime.strptime(root.attrib['Date'], '%d.%m.%Y').date()
        rates = {'date': date.isoformat()}
        for currency in ('USD', 'EUR'):
            matches = [row for row in root.findall('Valute') if row.findtext('CharCode') == currency]
            if not matches and currency == 'EUR':
                continue
            if len(matches) != 1:
                raise ValueError()
            values = []
            for key in ('Nominal', 'Value'):
                text = (matches[0].findtext(key) or '').replace(' ', '').replace('\xa0', '').replace(',', '.')
                if not re.fullmatch(r'\d{1,12}(?:\.\d{1,12})?', text):
                    raise ValueError()
                number = Decimal(text)
                if not number.is_finite() or number <= 0:
                    raise ValueError()
                values.append(number)
            with localcontext() as context:
                context.prec = 40
                rate = values[1] / values[0]
            rates[currency.lower() + '_rub'] = decimal_text(rate)
        return rates
    except (ET.ParseError, ValueError, KeyError, InvalidOperation, LookupError):
        raise ExpenseError('Invalid currency rate or date in the official rate feed.')


class Expenses:
    def __init__(self, store, fetcher=None, clock=None):
        self.store = store
        self.fetcher = fetcher or fetch_rate
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.refresh_lock = threading.Lock()
        self._snapshot_cache = None
        self._snapshot_generation = None

    def _read(self, key, default):
        with self.store.lock:
            row = self.store.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            try:
                value = json.loads(row['value']) if row else default
                return value if isinstance(value, type(default)) else default
            except (ValueError, TypeError):
                return default

    def _write(self, key, value):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    def refresh(self, force=False):
        # Simultaneous opens/buttons share the in-flight request, including errors.
        if not self.refresh_lock.acquire(blocking=False):
            with self.refresh_lock:
                return
        try:
            now = self.clock()
            saved = self._read('expenses_fx', {})
            if not force and saved.get('version') == 2 and str(saved.get('checked_at', ''))[:10] == now.date().isoformat():
                return
            try:
                rate = self.fetcher()
                date = dt.date.fromisoformat(rate['date'])
                numbers = {key: Decimal(rate[key]) for key in ('usd_rub', 'eur_rub') if key in rate}
                if ('usd_rub' not in numbers or any(not n.is_finite() or n <= 0 for n in numbers.values())
                        or date > now.date() + dt.timedelta(days=1)):
                    raise ExpenseError('Invalid currency rate or date in the official rate feed.')
                # Replace the pair together; never cross rates from different dates.
                saved.pop('eur_rub', None)
                saved.update({key: decimal_text(value) for key, value in numbers.items()})
                saved.update(date=date.isoformat(), error='')
            except Exception as error:
                saved['error'] = str(error) if isinstance(error, ExpenseError) else 'Could not refresh the official rate. Retry when the connection is available.'
            saved['checked_at'] = self.clock().isoformat()
            saved['version'] = 2
            self._write('expenses_fx', saved)
        finally:
            self.refresh_lock.release()

    def rate_snapshot(self):
        saved = self._read('expenses_fx', {})
        rate, date = saved.get('usd_rub'), saved.get('date')
        now = self.clock()
        try:
            if not isinstance(rate, str) or len(rate) > 80 or not Decimal(rate).is_finite() or Decimal(rate) <= 0:
                raise ValueError()
            date_value = dt.date.fromisoformat(date)
        except (ValueError, TypeError, InvalidOperation):
            rate, date, date_value = None, None, None
        eur_rate = saved.get('eur_rub')
        try:
            if (not date_value or not isinstance(eur_rate, str) or len(eur_rate) > 80
                    or not Decimal(eur_rate).is_finite() or Decimal(eur_rate) <= 0):
                raise ValueError()
        except (ValueError, InvalidOperation):
            eur_rate = None
        checked = saved.get('checked_at')
        try:
            checked_value = dt.datetime.fromisoformat(checked)
            if checked_value.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            checked, checked_value = None, None
        error = saved.get('error', '')
        error = error if isinstance(error, str) else ''
        return {'source': 'CBR', 'url': CBR_URL, 'usd_rub': rate, 'eur_rub': eur_rate, 'date': date,
                'checked_at': checked, 'error': error, 'estimated': True,
                'stale': not rate or bool(error) or not checked_value or checked_value.date() != now.date()
                or not date_value or not -1 <= (now.date() - date_value).days <= 7}

    def set_currency(self, payload):
        if set(payload) != {'currency'} or not isinstance(payload['currency'], str) or payload['currency'] not in CURRENCIES:
            raise ExpenseError('Choose USD, EUR or RUB.')
        self._write('expenses_currency', payload['currency'])

    def _validate_item(self, payload, creating=False):
        allowed = {'name', 'amount', 'currency', 'period', 'notes', 'next_charge_date'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ExpenseError('Unknown expense fields supplied.')
        if creating and not {'name', 'amount', 'currency', 'period'} <= payload.keys():
            raise ExpenseError('Enter a name, amount, currency and billing period.')
        result = {}
        for key, value in payload.items():
            if key == 'amount':
                result[key] = amount(value)
            elif key == 'next_charge_date':
                result[key] = charge_date(value)
            elif key in {'currency', 'period'}:
                if not isinstance(value, str) or value not in (CURRENCIES if key == 'currency' else PERIODS):
                    raise ExpenseError('Invalid expense currency or billing period.')
                result[key] = value
            else:
                limit = 160 if key == 'name' else 4000
                if not isinstance(value, str) or len(value) > limit or '\x00' in value:
                    raise ExpenseError('Expense name or notes are too long or invalid.')
                result[key] = value.strip()
                if key == 'name' and not result[key]:
                    raise ExpenseError('Enter the expense name.')
        return result

    def add(self, payload):
        row = self._validate_item(payload, creating=True)
        row.update(id=uuid.uuid4().hex, created_at=self.clock().isoformat(), updated_at=self.clock().isoformat())
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO expenses (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
        return row['id']

    def update(self, identifier, payload):
        row = self._validate_item(payload)
        row['updated_at'] = self.clock().isoformat()
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute('UPDATE expenses SET ' + ','.join(k + '=?' for k in row) + ' WHERE id=?', [*row.values(), identifier])
            if not cursor.rowcount:
                raise KeyError(identifier)

    def delete(self, identifier):
        with self.store.lock, self.store.db:
            if not self.store.db.execute('DELETE FROM expenses WHERE id=?', (identifier,)).rowcount:
                raise KeyError(identifier)

    def snapshot(self, refresh=False):
        if refresh:
            self.refresh()
        with self.store.lock:
            generation = (self.store.db.total_changes, self.store.db.execute('PRAGMA data_version').fetchone()[0],
                          self.clock().date().isoformat())
            if self._snapshot_cache is not None and generation == self._snapshot_generation:
                return copy.deepcopy(self._snapshot_cache)
            result = self._build_snapshot()
            self._snapshot_cache = copy.deepcopy(result) if len(result['servers']) + len(result['items']) <= 256 else None
            self._snapshot_generation = generation
            return result

    def _build_snapshot(self):
        currency = self._read('expenses_currency', 'RUB')
        if currency not in CURRENCIES:
            currency = 'RUB'
        fx = self.rate_snapshot()
        rate = {'RUB': Decimal(1), **{code: Decimal(fx[code.lower() + '_rub'])
                                    for code in ('USD', 'EUR') if fx[code.lower() + '_rub']}}
        with self.store.lock:
            servers = [dict(row) for row in self.store.db.execute('''SELECT id,name,alias,price,cost_amount,
                cost_currency,cost_period,cost_review FROM servers WHERE archived=0 ORDER BY created_at,alias''')]
            items = [dict(row) for row in self.store.db.execute('SELECT * FROM expenses ORDER BY created_at,id')]
        with localcontext() as context:
            context.prec = 40
            for row in servers:
                row['cost_review'] = bool(row['cost_review'])
                row.update(self._row_totals(row['cost_amount'], row['cost_currency'], row['cost_period'], currency, rate))
            for row in items:
                row.update(self._row_totals(row['amount'], row['currency'], row['period'], currency, rate))
            server_costs = [(r['cost_amount'], r['cost_currency'], r['cost_period'], r['cost_review']) for r in servers]
            item_costs = [(r['amount'], r['currency'], r['period'], False) for r in items]
            totals = {key: self._totals(rows, currency, rate) for key, rows in
                      [('servers', server_costs), ('items', item_costs), ('all', server_costs + item_costs)]}
        return {'currency': currency, 'fx': fx, 'servers': servers, 'items': items, 'totals': totals}

    @staticmethod
    def _money(value):
        return format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f') if value is not None else None

    @staticmethod
    def _convert(value, source, currency, rate):
        if source == currency or value == 0:
            return value
        if source not in rate or currency not in rate:
            return None
        return value * rate[source] / rate[currency]

    def _row_totals(self, raw, source, period, currency, rate):
        value = Decimal(raw) if raw is not None and source in CURRENCIES else None
        converted = self._convert(value, source, currency, rate) if value is not None else None
        monthly = converted / {'monthly': 1, 'quarterly': 3, 'yearly': 12}[period] if converted is not None and period != 'once' else None
        return {'converted_amount': self._money(converted), 'monthly': self._money(monthly),
                'yearly': self._money(monthly * 12) if monthly is not None else None}

    def _totals(self, rows, currency, rate):
        native = {code: {key: Decimal(0) for key in ('monthly', 'yearly', 'once')} for code in ('USD', 'EUR', 'RUB')}
        unknown = review = 0
        missing_recurring = missing_once = False
        for raw, source, period, needs_review in rows:
            review += int(needs_review)
            if raw is None or source not in CURRENCIES:
                unknown += 1
                if period == 'once':
                    missing_once = True
                else:
                    missing_recurring = True
                continue
            value = Decimal(raw)
            if period == 'once':
                native[source]['once'] += value
            else:
                monthly = value / {'monthly': 1, 'quarterly': 3, 'yearly': 12}[period]
                native[source]['monthly'] += monthly
                native[source]['yearly'] += monthly * 12
        result = {'unknown_count': unknown, 'review_count': review, 'known': {}}
        for key in ('monthly', 'yearly', 'once'):
            value = Decimal(0)
            missing = missing_once if key == 'once' else missing_recurring
            missing_fx = False
            for source, values in native.items():
                converted = self._convert(values[key], source, currency, rate) if values[key] else Decimal(0)
                if converted is None:
                    missing = True
                    missing_fx = True
                else:
                    value += converted
            result[key] = None if missing else self._money(value)
            result['known'][key] = None if missing_fx else self._money(value)
        result['native'] = {code: {key: self._money(value) for key, value in values.items()} for code, values in native.items()}
        result['complete'] = not (unknown or review) and all(result[key] is not None for key in ('monthly', 'yearly', 'once'))
        return result
