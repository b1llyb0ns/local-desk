"""Local rental reminders. Reads dates from SQLite, never contacts a VPS."""
import datetime
import html
from pathlib import Path
import shutil
import subprocess
import threading

HERE = Path(__file__).resolve().parent


def deadline(days):
    if days < 0:
        return 'overdue', f'Saved date passed {abs(days)} {"day" if days == -1 else "days"} ago'
    if days == 0:
        return 'today', 'Expires today'
    if days <= 3:
        return 'urgent', f'Expires in {days} {"day" if days == 1 else "days"}'
    if days <= 7:
        return 'soon', f'Expires in {days} days'
    return 'later', f'Expires in {days} days'


class RentalAlerts:
    def __init__(self, store, sender=None, clock=None):
        self.store = store
        self.program = shutil.which('notify-send')
        self.sender = sender or self.desktop_notification
        self.available = bool(sender or self.program)
        self.clock = clock or (lambda: datetime.datetime.now().astimezone())
        self.delivery_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self._snapshot_key = None
        self._snapshot_cache = None
        self._checked_key = None
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS rental_notifications (
                server_id TEXT NOT NULL, lease_end TEXT NOT NULL, notified_on TEXT NOT NULL,
                sent_at TEXT NOT NULL, PRIMARY KEY(server_id, lease_end, notified_on))''')
            store.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('rental_notifications_enabled','true')")

    def setting(self, name, default=''):
        with self.store.lock:
            row = self.store.db.execute('SELECT value FROM metadata WHERE key=?', (name,)).fetchone()
            return row['value'] if row else default

    def save_setting(self, name, value):
        with self.store.lock:
            row = self.store.db.execute('SELECT value FROM metadata WHERE key=?', (name,)).fetchone()
            if row and row['value'] == value:
                return
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', (name, value))

    def set_enabled(self, enabled):
        self.save_setting('rental_notifications_enabled', 'true' if enabled else 'false')
        self.wake_event.set()

    def _generation(self, now):
        """Called under store.lock; external commits and local dates invalidate caches."""
        return (now.date(), now.tzname(), now.utcoffset(), bool(self.available),
                self.store.db.total_changes, self.store.db.execute('PRAGMA data_version').fetchone()[0],
                self.store.db.in_transaction)

    def _notification_settings(self):
        values = {row['key']: row['value'] for row in self.store.db.execute('''SELECT key,value FROM metadata WHERE key IN
              ('rental_notifications_enabled','rental_notification_error','rental_notification_last_sent')''')}
        return {'enabled': values.get('rental_notifications_enabled') == 'true', 'available': self.available,
                'error': values.get('rental_notification_error', ''),
                'last_sent_at': values.get('rental_notification_last_sent', '')}

    @staticmethod
    def _copy_snapshot(data):
        # Rows/settings contain only scalar SQL values; copy each mutable shell.
        return dict(data, alerts=[dict(row) for row in data['alerts']],
                    undated=[dict(row) for row in data['undated']],
                    thresholds=list(data['thresholds']), notifications=dict(data['notifications']))

    def _snapshot(self, now, key, notifications=None):
        """Build/cache under store.lock; returned dictionaries never share cache state."""
        if key == self._snapshot_key and self._snapshot_cache is not None:
            return self._copy_snapshot(self._snapshot_cache)
        today = now.date()
        rows = [dict(row) for row in self.store.db.execute(
            'SELECT id,name,alias,lease_end,lease_hint,provider_url FROM servers WHERE archived=0 ORDER BY alias')]
        alerts, undated = [], []
        for row in rows:
            try:
                end = datetime.date.fromisoformat(row['lease_end']) if row['lease_end'] else None
            except (TypeError, ValueError):
                end = None
            if end is None:
                undated.append(row)
                continue
            days = (end - today).days
            if days <= 7:
                level, message = deadline(days)
                alerts.append(dict(row, days=days, level=level, message=message))
        alerts.sort(key=lambda row: (row['days'], row['alias']))
        result = {'today': today.isoformat(), 'timezone': now.tzname() or 'Local time',
                  'alerts': alerts, 'undated': undated, 'thresholds': [7, 3, 0],
                  'notifications': notifications if notifications is not None else self._notification_settings()}
        self._snapshot_key = key
        self._snapshot_cache = self._copy_snapshot(result) if len(rows) <= 256 else None
        return result

    def snapshot(self):
        with self.store.lock:
            now = self.clock()
            return self._snapshot(now, self._generation(now))

    def desktop_notification(self, title, body, urgency):
        if not self.program:
            raise RuntimeError('Desktop notifications are unavailable: notify-send is not installed.')
        result = subprocess.run(
            [self.program, '--app-name=Local Desk', '--icon=' + str(HERE / 'static/icon.svg'),
             '--hint=string:desktop-entry:local-desk', '--urgency=' + urgency,
             '--expire-time=15000', '--', title, body],
            capture_output=True, text=True, timeout=8)
        if result.returncode:
            raise RuntimeError('The desktop notification service could not be reached. Check notifications in your desktop settings.')

    def deliver(self, title, body, urgency='normal'):
        try:
            self.sender(title, body, urgency)
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'The desktop notification could not be delivered.'
            self.save_setting('rental_notification_error', message)
            raise RuntimeError(message) from None
        self.save_setting('rental_notification_error', '')

    def preview(self):
        with self.delivery_lock:
            self.deliver('Local Desk · Rental reminders',
                         'Notification preview. Rental dates were not changed.')

    def check(self):
        with self.delivery_lock:
            if not self.available:
                return 0
            with self.store.lock:
                now = self.clock()
                key = self._generation(now)
                if key == self._checked_key:
                    return 0
                cached = self._snapshot_cache if key == self._snapshot_key else None
                notifications = cached['notifications'] if cached is not None else self._notification_settings()
                if not notifications['enabled']:
                    self._checked_key = key
                    return 0
                data = self._snapshot(now, key, notifications)
                sent = {(row['server_id'], row['lease_end']) for row in self.store.db.execute(
                    'SELECT server_id,lease_end FROM rental_notifications WHERE notified_on=?', (data['today'],))}
                pending = [row for row in data['alerts'] if (row['id'], row['lease_end']) not in sent]
                if not pending:
                    self._checked_key = key
                    return 0
            lines = [html.escape(row['name'][:80]) + ': ' + row['message'] + ' (' + row['lease_end'] + ')' for row in pending[:5]]
            if len(pending) > 5:
                lines.append(f'And {len(pending) - 5} more. Open Local Desk for the full list.')
            lines.append('Check renewal with your hosting provider, then update the saved date in Local Desk.')
            self.deliver('VPS rental reminder' if len(pending) == 1 else f'Rental reminders · {len(pending)} VPS',
                         '\n'.join(lines), 'critical' if any(row['days'] <= 0 for row in pending) else 'normal')
            now = self.clock().isoformat()
            with self.store.lock, self.store.db:
                self.store.db.executemany('INSERT OR IGNORE INTO rental_notifications VALUES(?,?,?,?)',
                                         [(row['id'], row['lease_end'], data['today'], now) for row in pending])
                cutoff = (datetime.date.fromisoformat(data['today']) - datetime.timedelta(days=90)).isoformat()
                self.store.db.execute('DELETE FROM rental_notifications WHERE notified_on<?', (cutoff,))
                self.store.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('rental_notification_last_sent',?)", (now,))
            self.store.event(None, 'info', f'Rental reminder delivered for {len(pending)} server(s).')
            # Keep the generation that was actually checked. Notification writes
            # get one reconciliation pass; edits during delivery must not be missed.
            self._checked_key = key
            return len(pending)

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.stop_event.is_set():
                try:
                    self.check()
                except Exception:
                    # A notification failure must never stop SSH tasks or the UI.
                    pass
                self.wake_event.wait(60)
                self.wake_event.clear()
        self.thread = threading.Thread(target=run, name='rental-reminders', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=9)
