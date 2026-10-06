"""Small persistent IP-to-country cache; no SSH or background polling."""
import ipaddress
import json
import re
import threading
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

COUNTRY_API = 'https://api.country.is/'
COUNTRY = re.compile(r'[A-Z]{2}\Z')


def public_ip(host):
    try:
        address = ipaddress.ip_address(host)
        return str(address) if address.is_global else None
    except ValueError:
        return None


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def lookup_countries(addresses):
    request = Request(COUNTRY_API, data=json.dumps(addresses).encode(), headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'})
    with build_opener(NoRedirects()).open(request, timeout=6) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Country response too large')
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError('Invalid country response')
    return {row['ip']: row['country'] for row in rows if isinstance(row, dict)
            and isinstance(row.get('ip'), str) and row['ip'] in addresses
            and isinstance(row.get('country'), str) and COUNTRY.fullmatch(row['country'])}


class CountryFlags:
    def __init__(self, store, lookup=None, clock=None):
        self.store = store
        self.lookup = lookup or lookup_countries
        self.clock = clock or time.time
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            with self.store.lock:
                hosts = [row['host'] for row in self.store.db.execute('SELECT DISTINCT host FROM servers')]
                saved = self.store.db.execute("SELECT value FROM metadata WHERE key='ip_countries'").fetchone()
            try:
                cache = json.loads(saved['value']) if saved else {}
            except (ValueError, TypeError):
                cache = {}
            if not isinstance(cache, dict):
                cache = {}
            addresses = {host: public_ip(host) for host in hosts}
            now = self.clock()
            valid = {}
            for address in set(addresses.values()) - {None}:
                item = cache.get(address)
                if (isinstance(item, dict) and isinstance(item.get('next_check'), (int, float))
                        and (item.get('country') is None or isinstance(item['country'], str)
                             and COUNTRY.fullmatch(item['country']))):
                    valid[address] = item
            missing = sorted(address for address in set(addresses.values()) - {None}
                             if valid.get(address, {}).get('next_check', 0) <= now)
            for start in range(0, len(missing), 100):
                batch = missing[start:start + 100]
                try:
                    countries = self.lookup(batch)
                except Exception:
                    # Keep an old flag during an outage and avoid repeated calls.
                    for address in batch:
                        valid[address] = {'country': valid.get(address, {}).get('country'), 'next_check': now + 3600}
                else:
                    for address in batch:
                        country = countries.get(address)
                        valid[address] = {'country': country, 'next_check': now + (30 * 86400 if country else 86400)}
            if missing or valid != cache:
                with self.store.lock, self.store.db:
                    self.store.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                          ('ip_countries', json.dumps(valid)))
            return {host: valid[address]['country'] for host, address in addresses.items()
                    if address in valid and valid[address].get('country')}
