"""Repeated in-process rental reads; isolated data and a no-op notification sender."""
import argparse
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store
from rental_alerts import RentalAlerts


def measure(function, store, repeats=600):
    statements = []
    function()  # Warm caches and the once-daily delivery record, outside timing.
    store.db.set_trace_callback(statements.append)
    start = time.process_time()
    for _ in range(repeats):
        function()
    elapsed = time.process_time() - start
    store.db.set_trace_callback(None)
    return {'calls': repeats, 'cpu_ms': round(elapsed * 1000, 3),
            'selects': sum(s.startswith('SELECT') for s in statements),
            'pragmas': sum(s.startswith('PRAGMA') for s in statements),
            'writes': sum(s.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for s in statements)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'tmp'
    root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rental-runtime-', dir=root) as directory:
        home = Path(directory)
        store = Store(home / 'data', home, import_existing=False, export=False)
        with closing(store.db):
            for number in range(8):
                store.add({'name': f'Node {number}', 'alias': f'node-{number}',
                           'host': f'192.0.2.{number + 1}', 'lease_end': '2026-09-06'})
            reminders = RentalAlerts(store, sender=lambda *args: None,
                                     clock=lambda: dt.datetime(2026, 9, 5, 12, tzinfo=dt.timezone.utc))
            report = {'snapshot': measure(reminders.snapshot, store),
                      'delivered_check': measure(reminders.check, store)}
            reminders.set_enabled(False)
            report['disabled_check'] = measure(reminders.check, store)
    report['scope'] = '600 unchanged calls per case; no real notifications, service, network or hosts.'
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
