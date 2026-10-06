#!/usr/bin/env python3
"""Local VPS Desk: Python standard library, SQLite, OpenSSH, no server agents."""
import argparse
import concurrent.futures
import contextlib
import copy
import datetime
import glob
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sqlite3
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, parse_qs
import uuid

from ssh_engine import SSHEngine, SSHError
from rental_alerts import RentalAlerts
from desktop_updates import DesktopUpdates
from local_listeners import snapshot as local_listener_snapshot
from local_port_inventory import LocalPortInventory
from static_assets import StaticAssets, FILES as STATIC_FILES, matches_etag
from country_flags import CountryFlags
from tasks import Tasks, TaskError
from diary import Diary, DiaryError, DiaryConflict
from expenses import Expenses, ExpenseError, COST_FIELDS, cost_fields, validate_cost_record, legacy_cost, initialize as initialize_expenses

HERE = Path(__file__).resolve().parent
ALIAS = re.compile(r'[a-z][a-z0-9_-]{1,39}\Z')
USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}\Z')
IDENTIFIER = re.compile(r'[a-f0-9]{32}\Z')


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class InputError(Exception):
    pass


class ConflictError(InputError):
    pass


def atomic_file(path, text, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise InputError('Expected a regular file: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        encoded = text.encode('utf-8')
        info = path.stat()
        if info.st_size == len(encoded) and path.read_bytes() == encoded:
            if info.st_mode & 0o777 != mode:
                path.chmod(mode)
            return
    fd, temporary = tempfile.mkstemp(prefix='.local-desk-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clean_text(value, maximum=200):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise InputError('Invalid field value.')
    return value.strip()


def clean_host(value):
    value = clean_text(value, 253)
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            value = value.encode('idna').decode().lower().rstrip('.')
        except UnicodeError:
            raise InputError('Enter the server IP address or domain.')
        labels = value.split('.')
        if not value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
            raise InputError('Enter an IP address or domain without an SSH command or port.')
        return value


def clean_url(value):
    value = clean_text(value, 2000)
    if not value:
        return ''
    if any(ord(c) < 32 for c in value):
        raise InputError('Invalid hosting URL.')
    parsed = urlsplit(value)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
        raise InputError('Hosting URL must start with https:// or http://.')
    return value


def validate_fields(payload, creating=False):
    allowed = {'name', 'alias', 'host', 'port', 'ssh_user', 'provider', 'provider_url',
               'lease_end', 'lease_hint', 'price', 'notes', 'short_note', 'archived'} | COST_FIELDS
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise InputError('Unknown fields supplied.')
    result = {}
    for key, value in payload.items():
        if key in COST_FIELDS:
            continue
        elif key == 'host':
            result[key] = clean_host(value)
        elif key == 'port':
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise InputError('Port must be a number from 1 to 65535.')
            result[key] = value
        elif key == 'alias':
            result[key] = clean_text(value).lower()
            if not ALIAS.fullmatch(result[key]):
                raise InputError('SSH alias: 2–40 lowercase letters, digits, underscores or hyphens; start with a letter.')
        elif key == 'ssh_user':
            result[key] = clean_text(value)
            if not USER.fullmatch(result[key]):
                raise InputError('Invalid SSH username.')
        elif key == 'provider_url':
            result[key] = clean_url(value)
        elif key == 'lease_end':
            if value in (None, ''):
                result[key] = None
            else:
                try:
                    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        raise ValueError()
                    date = datetime.date.fromisoformat(value)
                    if not 2000 <= date.year <= 2200:
                        raise ValueError()
                except ValueError:
                    raise InputError('Choose the full rental expiry date.')
                result[key] = value
                result['lease_hint'] = ''
        elif key == 'lease_hint':
            value = clean_text(value, 5)
            if value:
                try:
                    datetime.date.fromisoformat('2000-' + value)
                except ValueError:
                    raise InputError('Invalid month and day.')
            result[key] = value
        elif key == 'archived':
            if not isinstance(value, bool):
                raise InputError('Invalid Trash value.')
            result[key] = int(value)
        elif key == 'short_note':
            value = clean_text(value, 160)
            if any(ord(char) < 32 or char in '\x7f\u2028\u2029' for char in value):
                raise InputError('A quick note must be a single line, up to 160 characters.')
            result[key] = value
        elif key == 'notes':
            if not isinstance(value, str) or len(value) > 16000 or '\x00' in value:
                raise InputError('Notes must not exceed 16,000 characters.')
            result[key] = value
        else:
            result[key] = clean_text(value, 200 if key != 'price' else 100)
    if result.get('lease_end'):
        result['lease_hint'] = ''
    if creating and not all(result.get(key) for key in ('name', 'host', 'alias')):
        raise InputError('Enter a name, SSH alias and server address.')
    if 'name' in result and not result['name']:
        raise InputError('Enter the server name.')
    try:
        result.update(cost_fields(payload))
    except ExpenseError as error:
        raise InputError(str(error))
    return result


class Store:
    def __init__(self, directory, user_home, import_existing=True, export=True):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.home = Path(user_home)
        self.lock = threading.RLock()
        self.export_enabled = export
        self.path = self.directory / 'state.sqlite3'
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._summary_cache = None
        self._summary_generation = None
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS servers (
            id TEXT PRIMARY KEY, alias TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            host TEXT NOT NULL, port INTEGER NOT NULL DEFAULT 22, ssh_user TEXT NOT NULL DEFAULT 'root',
            metrics_ipqos TEXT NOT NULL DEFAULT '' CHECK(metrics_ipqos IN ('','ef')),
            provider TEXT NOT NULL DEFAULT '', provider_url TEXT NOT NULL DEFAULT '',
            lease_end TEXT, lease_hint TEXT NOT NULL DEFAULT '', price TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'pending', status TEXT NOT NULL DEFAULT 'Not checked',
            last_seen TEXT, last_error TEXT NOT NULL DEFAULT '', error_kind TEXT NOT NULL DEFAULT '',
            snapshot TEXT, first_boot TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(host,port)
          );
          CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY, server_id TEXT NOT NULL, at TEXT NOT NULL,
            cpu REAL, memory REAL, disk REAL
          );
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, server_id TEXT, kind TEXT NOT NULL, message TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS history_server_id ON history(server_id,id DESC);
        ''')
        server_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(servers)')}
        if 'short_note' not in server_columns:
            with self.db:
                self.db.execute("ALTER TABLE servers ADD COLUMN short_note TEXT NOT NULL DEFAULT ''")
        if 'metrics_ipqos' not in server_columns:
            with self.db:
                self.db.execute("ALTER TABLE servers ADD COLUMN metrics_ipqos TEXT NOT NULL DEFAULT '' CHECK(metrics_ipqos IN ('','ef'))")
        for name, definition in [('ssh_latency_ms', 'REAL'), ('ssh_checked_at', 'TEXT')]:
            if name not in server_columns:
                with self.db:
                    self.db.execute('ALTER TABLE servers ADD COLUMN ' + name + ' ' + definition)
        initialize_expenses(self)
        self.path.chmod(0o600)
        if import_existing and not self.db.execute("SELECT 1 FROM metadata WHERE key='imported'").fetchone():
            self.import_existing()

    def import_existing(self):
        path = self.home / '.local/share/local-desk/import/inventory.json'
        if not path.exists():
            return
        inventory = json.loads(path.read_text())
        payment_path = self.home / '.local/share/local-desk/import/payments.md'
        payments = {}
        if payment_path.exists():
            for line in payment_path.read_text().splitlines():
                fields = [v.strip().strip('`').strip('<>') for v in line.split('|')[1:-1]]
                if len(fields) == 8 and ALIAS.fullmatch(fields[3]):
                    payments[fields[3]] = {'lease_hint': fields[4], 'price': fields[5], 'provider_url': fields[7]}
        for old in inventory['servers']:
            payment = payments.get(old['alias'], {})
            fields = {'name': old['alias'], 'alias': old['alias'], 'host': old['ip'], 'port': 22,
                      'ssh_user': old.get('user', 'root'), 'provider': old.get('provider', ''),
                      'provider_url': payment.get('provider_url', ''), 'lease_hint': payment.get('lease_hint', ''),
                      'price': payment.get('price', ''), 'notes': '', 'short_note': old.get('short_note', '')}
            server = self.add(fields, state='pending', export=False)
            with self.db:
                self.db.execute('UPDATE servers SET first_boot=?,status=? WHERE id=?',
                                (old.get('first_recorded_boot_utc'), 'Refresh status over SSH', server['id']))
                if old['state'] != 'ready':
                    self.db.execute('UPDATE servers SET state=?,last_error=?,error_kind=? WHERE id=?',
                                    (old['state'], old.get('status', ''), old['state'], server['id']))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('imported',?)", (utcnow(),))
        self.event(None, 'info', 'Imported servers and dates from local files. Dates without years need confirmation.')

    def add(self, fields, state='provisioning', export=True):
        fields = validate_fields(fields, creating=True)
        if not COST_FIELDS.intersection(fields):
            fields.update(legacy_cost(fields.get('price', '')))
        try:
            validate_cost_record(fields)
        except ExpenseError as error:
            raise InputError(str(error))
        if export and self.export_enabled:
            self.check_alias_available(fields['alias'])
        row = dict(fields, id=uuid.uuid4().hex, state=state, created_at=utcnow(), updated_at=utcnow())
        try:
            with self.lock, self.db:
                self.db.execute('INSERT INTO servers (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
        except sqlite3.IntegrityError:
            raise InputError('Address and port or SSH alias already exists, possibly in Trash.')
        if export:
            self.export_connections()
        return self.get(row['id'])

    def get(self, identifier, full=True):
        with self.lock:
            row = self.db.execute('SELECT * FROM servers WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self.format_row(row, full)

    def format_row(self, row, full=False):
        result = dict(row)
        snapshot = json.loads(result.pop('snapshot')) if row['snapshot'] else None
        if not full and snapshot:
            keys = {'collected_at', 'hostname', 'os', 'cpu_count', 'cpu_pct', 'memory', 'disk', 'uptime_seconds', 'boot_at'}
            snapshot = {k: v for k, v in snapshot.items() if k in keys}
        result['snapshot'] = snapshot
        result['archived'] = bool(result['archived'])
        result['cost_review'] = bool(result['cost_review'])
        result['history'] = [dict(item) for item in self.db.execute(
            'SELECT at,cpu,memory,disk FROM history WHERE server_id=? ORDER BY id DESC LIMIT 36', (result['id'],))][::-1]
        return result

    def list(self, full=False):
        with self.lock:
            generation = (self.db.total_changes, self.db.execute('PRAGMA data_version').fetchone()[0])
            if not full and self._summary_cache is not None and generation == self._summary_generation:
                return copy.deepcopy(self._summary_cache)
            rows = [self.format_row(row, full) for row in self.db.execute('SELECT * FROM servers ORDER BY archived,created_at,alias')]
            if not full:
                # Only compact summaries are retained; full process/package lists
                # are never cached here. Direct SQLite edits also invalidate it.
                self._summary_cache = copy.deepcopy(rows) if len(rows) <= 256 else None
                self._summary_generation = generation
            return rows

    def update(self, identifier, fields, internal=False):
        fields = fields if internal else validate_fields(fields)
        with self.lock:
            old = self.db.execute('SELECT * FROM servers WHERE id=?', (identifier,)).fetchone()
            if old is None:
                raise KeyError(identifier)
            if not internal:
                try:
                    validate_cost_record(dict(dict(old), **fields))
                except ExpenseError as error:
                    raise InputError(str(error))
            if not internal and self.export_enabled and (fields.get('alias', old['alias']) != old['alias'] or
                                                         (old['archived'] and fields.get('archived') == 0)):
                self.check_alias_available(fields.get('alias', old['alias']))
            if not internal and any(key in fields and fields[key] != old[key] for key in ('host', 'port', 'ssh_user')):
                fields.update(snapshot=None, last_seen=None, last_error='', error_kind='', metrics_ipqos='',
                              ssh_latency_ms=None, ssh_checked_at=None,
                              state='pending', status='Connection changed')
            fields['updated_at'] = utcnow()
            try:
                with self.db:
                    self.db.execute('UPDATE servers SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', [*fields.values(), identifier])
            except sqlite3.IntegrityError:
                raise InputError('Address and port or SSH alias already in use.')
        if not internal:
            self.export_connections()
        return self.get(identifier)

    def delete(self, identifier, confirm_alias):
        """Forget a trashed local record; never remove keys or contact its VPS."""
        with self.lock:
            row = self.db.execute('SELECT alias,archived FROM servers WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if not isinstance(confirm_alias, str) or confirm_alias != row['alias']:
                raise InputError('Type the exact SSH alias to permanently delete this local record.')
            if not row['archived']:
                raise ConflictError('Move this server to Trash before permanently deleting its local record.')
            # Trashed records are already excluded from exports. Check exports
            # before the irreversible transaction so a write failure keeps data.
            self.export_connections()
            with self.db:
                self.db.execute('DELETE FROM history WHERE server_id=?', (identifier,))
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rental_notifications'").fetchone():
                    self.db.execute('DELETE FROM rental_notifications WHERE server_id=?', (identifier,))
                self.db.execute('UPDATE events SET server_id=NULL WHERE server_id=?', (identifier,))
                self.db.execute('DELETE FROM servers WHERE id=?', (identifier,))
                self.db.execute('INSERT INTO events(at,server_id,kind,message) VALUES (?,NULL,?,?)',
                                (utcnow(), 'info', f'Permanently deleted local server record: {row["alias"]}. VPS unchanged.'))
                self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 400)')
            self._summary_cache = None
            self._summary_generation = None

    def snapshot(self, identifier, snapshot):
        with self.lock:
            self.update(identifier, {'snapshot': json.dumps(snapshot), 'last_seen': snapshot['collected_at'],
                                    'last_error': '', 'error_kind': '', 'state': 'ready', 'status': 'Available'}, internal=True)
            with self.db:
                self.db.execute('INSERT INTO history(server_id,at,cpu,memory,disk) VALUES (?,?,?,?,?)',
                                (identifier, snapshot['collected_at'], snapshot['cpu_pct'], snapshot['memory']['pct'], snapshot['disk']['pct']))
                self.db.execute('DELETE FROM history WHERE server_id=? AND id NOT IN (SELECT id FROM history WHERE server_id=? ORDER BY id DESC LIMIT 72)', (identifier, identifier))

    def failure(self, identifier, error):
        self.update(identifier, {'last_error': str(error), 'error_kind': getattr(error, 'kind', 'ssh_error'),
                                 'state': getattr(error, 'kind', 'ssh_error'), 'status': str(error)}, internal=True)
        self.export_connections()

    def event(self, identifier, kind, message):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(at,server_id,kind,message) VALUES (?,?,?,?)', (utcnow(), identifier, kind, message[:1200]))
            self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 400)')

    def events(self):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT events.*,servers.name AS server_name FROM events LEFT JOIN servers ON servers.id=events.server_id ORDER BY events.id DESC LIMIT 100')]

    def check_alias_available(self, alias):
        """Do not silently shadow an unrelated, explicit local SSH alias."""
        visited = set()
        def inspect(path):
            if path in visited or len(visited) >= 128 or path == self.home / '.ssh/local-desk.conf' or not path.is_file():
                return
            visited.add(path)
            for line in path.read_text().splitlines():
                words = shlex.split(line, comments=True)
                if not words:
                    continue
                if words[0].lower() == 'host' and alias in words[1:]:
                    raise InputError('This SSH alias is used outside the panel. Choose another.')
                if words[0].lower() == 'include':
                    for pattern in words[1:]:
                        pattern = str(self.home) + pattern[1:] if pattern.startswith('~/') else pattern
                        if not pattern.startswith('/'):
                            pattern = str(self.home / '.ssh' / pattern)
                        for child in glob.glob(pattern):
                            inspect(Path(child))
        inspect(self.home / '.ssh/config')

    def export_connections(self):
        if not self.export_enabled:
            return
        with self.lock:
            rows = [dict(row) for row in self.db.execute('''
                SELECT alias,host,port,ssh_user,metrics_ipqos,provider,state,status,lease_end,lease_hint,
                       notes,short_note,provider_url,first_boot,
                       json_extract(snapshot,'$.boot_at') AS last_boot_utc,
                       snapshot IS NOT NULL AS has_snapshot
                  FROM servers WHERE archived=0 ORDER BY created_at,alias''')]
            ssh_dir = self.home / '.ssh'
            ssh_dir.mkdir(mode=0o700, exist_ok=True)
            managed = ssh_dir / 'local-desk.conf'
            chunks = ['# Managed by VPS Desk; edit server addresses in the local panel.\n']
            for row in rows:
                names = row['alias'] + (' ' + row['host'] if sum(s['host'] == row['host'] for s in rows) == 1 else '')
                # Records are exported before onboarding succeeds, and a VPS may
                # later be reinstalled. Keep the client's password fallback;
                # successful setup enforces key-only access on the server.
                chunks.append('\nHost ' + names + '\n  HostName ' + row['host'] +
                              '\n  User ' + row['ssh_user'] + '\n  Port ' + str(row['port']) +
                              '\n  IdentityFile ~/.ssh/id_ed25519\n  IdentitiesOnly yes\n  IdentityAgent none\n'
                              '  StrictHostKeyChecking accept-new\n  ForwardAgent no\n  ServerAliveInterval 30\n  ConnectTimeout 8\n' +
                              ('  IPQoS ef\n' if row['metrics_ipqos'] == 'ef' else ''))
            atomic_file(managed, ''.join(chunks))
            config_path = ssh_dir / 'config'
            config = config_path.read_text() if config_path.exists() else ''
            include = 'Include ~/.ssh/local-desk.conf\n'
            if not config.startswith(include):
                backup = self.directory / 'ssh-config.before-panel'
                if not backup.exists():
                    atomic_file(backup, config)
                atomic_file(config_path, include + config)
            old_path = self.home / '.local/share/local-desk/import/inventory.json'
            if old_path.exists():
                original = json.loads(old_path.read_text())
                by_alias = {s['alias']: s for s in original.get('servers', [])}
                exported = []
                for row in rows:
                    old = by_alias.get(row['alias'], {})
                    old.update(alias=row['alias'], ip=row['host'], port=row['port'], user=row['ssh_user'],
                               provider=row['provider'], state=row['state'], status=row['status'],
                               lease_end=row['lease_end'], lease_hint=row['lease_hint'], notes=row['notes'],
                               short_note=row['short_note'],
                               provider_url=row['provider_url'], first_recorded_boot_utc=row['first_boot'])
                    if row['has_snapshot']:
                        old['last_boot_utc'] = row['last_boot_utc']
                    else:
                        old.setdefault('last_boot_utc', None)
                    exported.append(old)
                original['servers'] = exported
                original['checked_on'] = datetime.date.today().isoformat()
                original['source_of_truth'] = str(self.path)
                backup = self.directory / 'inventory.before-panel.json'
                if not backup.exists():
                    atomic_file(backup, old_path.read_text())
                atomic_file(old_path, json.dumps(original, ensure_ascii=False, indent=2) + '\n')


class Application:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine
        self.csrf = secrets.token_urlsafe(32)
        self.jobs = {}
        self.busy = {}
        self.host_key_busy = set()
        self.job_lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix='vps')
        self.rentals = RentalAlerts(store)
        def notify(title, body):
            if self.rentals.snapshot()['notifications']['enabled']:
                self.rentals.deliver(title, body)
        self.desktop = DesktopUpdates(store, notify=notify)
        self.local_ports = LocalPortInventory(collector=lambda: local_listener_snapshot())
        self.expenses = Expenses(store)
        self.tasks = Tasks(store)
        self.diary = Diary(store)
        self.countries = CountryFlags(store)

    def submit(self, identifier, kind, credentials=None):
        with self.job_lock:
            server = self.store.get(identifier, full=False)
            if server['archived']:
                raise ConflictError('Restore this server from Trash before starting an SSH task.')
            if identifier in self.host_key_busy:
                raise ConflictError('Wait for the host-key operation to finish.')
            if identifier in self.busy:
                if kind in {'refresh', 'check'}:
                    return self.jobs[self.busy[identifier]].copy()
                raise InputError('A task is already running for this server.')
            if len(self.busy) >= 30 and kind != 'check':
                raise InputError('Wait for current tasks to finish.')
            job = {'id': uuid.uuid4().hex, 'server_id': identifier, 'kind': kind, 'state': 'queued',
                   'step': 0, 'message': 'Queued', 'started_at': utcnow()}
            self.jobs[job['id']] = job
            self.busy[identifier] = job['id']
            try:
                self.pool.submit(self.work, job['id'], credentials or {})
            except Exception:
                self.jobs.pop(job['id'], None)
                self.busy.pop(identifier, None)
                if credentials is not None:
                    credentials.clear()
                raise
            return job.copy()

    def check_all(self):
        # A fixed-size worker pool bounds connections; repeated clicks reuse
        # existing checks and never overlap another task on the same server.
        with self.job_lock:
            jobs, skipped = [], []
            for record in self.store.list():
                identifier = record['id']
                if record['archived']:
                    continue
                if identifier in self.host_key_busy or (identifier in self.busy and
                        self.jobs[self.busy[identifier]]['kind'] != 'check'):
                    skipped.append(identifier)
                    continue
                jobs.append(self.submit(identifier, 'check'))
            return {'jobs': jobs, 'skipped': skipped}

    def update_server(self, identifier, fields):
        validate_fields(fields)
        # Notification delivery can finish with a database write. Lifecycle
        # changes share its lock, always before job_lock and then store.lock.
        delivery = self.rentals.delivery_lock if 'archived' in fields else contextlib.nullcontext()
        with delivery, self.job_lock:
            previous = self.store.get(identifier, full=False)
            busy = identifier in self.busy or identifier in self.host_key_busy
            if busy and 'archived' in fields:
                raise ConflictError('Wait for the SSH task to finish before moving or restoring this server.')
            if busy and any(k not in {'notes', 'short_note', 'lease_end', 'provider_url', 'provider', 'price', 'name'} | COST_FIELDS for k in fields):
                raise InputError('Wait for the SSH task to finish before changing connection settings.')
            updated = self.store.update(identifier, fields)
            if previous['archived'] != updated['archived']:
                action = 'Moved local server record to Trash' if updated['archived'] else 'Restored local server record from Trash'
                self.store.event(identifier, 'info', f'{action}: {updated["alias"]}. VPS unchanged.')
            elif set(fields) != {'notes'}:
                self.store.event(identifier, 'info', 'Server details saved.')
            if set(fields) & {'lease_end', 'archived'}:
                self.rentals.wake_event.set()
            return updated

    def delete_server(self, identifier, data):
        if not isinstance(data, dict) or set(data) != {'confirm_alias'} or not isinstance(data['confirm_alias'], str) or not data['confirm_alias']:
            raise InputError('Type the exact SSH alias to permanently delete this local record.')
        with self.rentals.delivery_lock, self.job_lock:
            if identifier in self.busy or identifier in self.host_key_busy:
                raise ConflictError('Wait for the SSH task to finish before permanently deleting this local record.')
            self.store.delete(identifier, data['confirm_alias'])
            for job_id in [key for key, job in self.jobs.items() if job['server_id'] == identifier]:
                self.jobs.pop(job_id, None)
            self.rentals.wake_event.set()

    def accept_host_key(self, identifier):
        with self.job_lock:
            server = self.store.get(identifier)
            if server['archived']:
                raise ConflictError('Restore this server from Trash before changing its host key.')
            if identifier in self.busy or identifier in self.host_key_busy:
                raise ConflictError('Wait for the SSH task to finish.')
            self.host_key_busy.add(identifier)
        try:
            fingerprint = self.engine.accept_host_key(server)
            self.store.event(identifier, 'info', 'Host key updated at user request: ' + fingerprint)
            return fingerprint
        finally:
            with self.job_lock:
                self.host_key_busy.discard(identifier)

    def work(self, job_id, credentials):
        with self.job_lock:
            job = self.jobs[job_id]
            job.update(state='running', message='Connecting over SSH')
        identifier = job['server_id']

        def progress(step, message):
            with self.job_lock:
                job.update(step=step, message=message)

        try:
            server = self.store.get(identifier)
            if job['kind'] == 'check':
                progress(1, 'Checking SSH availability and connection time')
                latency = self.engine.check(server)
                self.store.update(identifier, {'state': 'ready', 'status': 'SSH available',
                                  'last_error': '', 'error_kind': '', 'ssh_latency_ms': latency,
                                  'ssh_checked_at': utcnow(), 'metrics_ipqos': server.get('metrics_ipqos', '')}, internal=True)
                self.store.export_connections()
                with self.job_lock:
                    job.update(state='done', message=f'SSH available · {latency:g} ms', finished_at=utcnow())
                return
            if job['kind'] == 'onboard':
                result = self.engine.onboard(server, credentials, progress)
                self.store.update(identifier, {'ssh_user': 'root', 'state': 'ready', 'status': 'root + SSH key',
                                                'metrics_ipqos': server.get('metrics_ipqos', ''),
                                                'last_error': '', 'error_kind': ''}, internal=True)
                self.store.event(identifier, 'success', 'Configured root + SSH key; SSH password login disabled. VPS backup: ' + result['backup'])
                server['ssh_user'] = 'root'
            else:
                progress(1, 'Collecting resources, services and containers')
            try:
                learned_ipqos = ''
                try:
                    snapshot = self.engine.collect(server)
                except SSHError as error:
                    if error.retry_hint != 'ipqos_ef' or server.get('metrics_ipqos'):
                        raise
                    progress(1, 'Retrying with SSH compatibility mode')
                    retry_server = dict(server, metrics_ipqos='ef')
                    snapshot = self.engine.collect(retry_server)
                    learned_ipqos = 'ef'
                self.store.snapshot(identifier, snapshot)
                if learned_ipqos:
                    self.store.update(identifier, {'metrics_ipqos': learned_ipqos}, internal=True)
            except SSHError as error:
                if job['kind'] != 'onboard':
                    raise
                self.store.failure(identifier, error)
                self.store.event(identifier, 'warning', 'SSH configured. Snapshot unavailable: ' + str(error))
            self.store.export_connections()
            with self.job_lock:
                job.update(state='done', message='SSH configured' if job['kind'] == 'onboard' else 'Status refreshed', finished_at=utcnow())
            if job['kind'] == 'refresh':
                self.store.event(identifier, 'success', 'Refreshed resources and running services.')
        except Exception as error:
            safe = error if isinstance(error, (SSHError, InputError)) else SSHError('Internal task error; details are in the local service log.')
            if not isinstance(error, (SSHError, InputError)):
                traceback.print_exc()
            if job['kind'] == 'check':
                self.store.update(identifier, {'ssh_latency_ms': None, 'ssh_checked_at': utcnow()}, internal=True)
            self.store.failure(identifier, safe)
            self.store.event(identifier, 'error', str(safe))
            with self.job_lock:
                job.update(state='error', message=str(safe), error_kind=getattr(safe, 'kind', 'error'), finished_at=utcnow())
        finally:
            credentials.clear()
            with self.job_lock:
                self.busy.pop(identifier, None)
                finished = [key for key, value in self.jobs.items() if value['state'] in {'done', 'error'}]
                for key in finished[:-100]:
                    self.jobs.pop(key, None)

    def all_jobs(self):
        with self.job_lock:
            return [job.copy() for job in self.jobs.values()]

    def credential_fields(self, data):
        if not isinstance(data, dict) or set(data) - {'mode', 'password', 'key_id', 'user', 'sudo_password', 'only_managed'}:
            raise InputError('Invalid initial login settings.')
        mode = data.get('mode', 'password')
        if mode not in {'password', 'key', 'managed'}:
            raise InputError('Choose a login method.')
        user = data.get('user', 'root')
        if not isinstance(user, str) or not USER.fullmatch(user):
            raise InputError('Invalid SSH user.')
        for field in ('password', 'sudo_password'):
            value = data.get(field, '')
            if not isinstance(value, str) or len(value) > 4096 or '\n' in value or '\r' in value or '\x00' in value:
                raise InputError('Invalid password.')
        if mode == 'password' and not data.get('password'):
            raise InputError('Enter the current password for the initial login.')
        if mode == 'key' and data.get('key_id') not in {k['id'] for k in self.engine.key_options()}:
            raise InputError('Choose an existing SSH key.')
        if not isinstance(data.get('only_managed', True), bool):
            raise InputError('Invalid key setting.')
        return dict(data, mode=mode, user=user)


class Handler(BaseHTTPRequestHandler):
    server_version = 'VPSDesk'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # No bodies, query strings, passwords, notes or headers in access logs.
        return

    def trusted(self, mutation=False):
        port = self.server.server_address[1]
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host = self.headers.get('Host', '')
        if host not in allowed or self.client_address[0] != '127.0.0.1':
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin not in {'http://' + h for h in allowed}:
            return False
        if self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
            return False
        if mutation:
            return hmac.compare_digest(self.headers.get('X-VPS-CSRF', ''), self.app.csrf)
        return True

    def reply(self, status, body, content_type='application/json; charset=utf-8', *, cache_control='no-store', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if status != 304:
            self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache_control)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        if self.headers.get_content_type() != 'application/json':
            raise InputError('Expected JSON.')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 64000:
                raise ValueError()
            value = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError):
            raise InputError('Invalid or oversized request.')

    def do_GET(self):
        if not self.trusted():
            return self.reply(403, {'error': 'The panel is available locally only.'})
        path = urlsplit(self.path).path
        try:
            if path == '/api/bootstrap':
                return self.reply(200, {'csrf': self.app.csrf, 'servers': self.app.store.list(),
                                        'jobs': self.app.all_jobs(), 'key': {'name': 'managed', 'fingerprint': self.app.engine.fingerprint},
                                        'key_options': self.app.engine.key_options(), 'rentals': self.app.rentals.snapshot(),
                                        'expenses': self.app.expenses.snapshot()})
            if path == '/api/servers':
                return self.reply(200, {'servers': self.app.store.list(), 'jobs': self.app.all_jobs(),
                                        'rentals': self.app.rentals.snapshot(), 'expenses': self.app.expenses.snapshot()})
            if path == '/api/server-countries':
                return self.reply(200, {'countries': self.app.countries.snapshot()})
            if path == '/api/expenses':
                return self.reply(200, self.app.expenses.snapshot(refresh=True))
            if path == '/api/rental-alerts':
                return self.reply(200, {'rentals': self.app.rentals.snapshot()})
            if path == '/api/desktop-updates':
                views = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('view')
                if views is None:
                    return self.reply(200, self.app.desktop.snapshot())
                if len(views) != 1 or views[0] not in {'pc', 'burp'}:
                    return self.reply(400, {'error': 'Choose the PC or Burp view.'})
                return self.reply(200, self.app.desktop.snapshot(view=views[0]))
            if path == '/api/desktop-updates/status':
                return self.reply(200, self.app.desktop.status())
            if path == '/api/local-listeners':
                return self.reply(200, self.app.local_ports.refresh())
            if path == '/api/local-listeners/cached':
                return self.reply(200, self.app.local_ports.cached_inventory())
            if path == '/api/jobs':
                return self.reply(200, {'jobs': self.app.all_jobs()})
            if path == '/api/events':
                return self.reply(200, {'events': self.app.store.events()})
            if path == '/api/tasks':
                return self.reply(200, self.app.tasks.snapshot())
            if path == '/api/diary':
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) != {'week'} or len(query['week']) != 1:
                    raise DiaryError('Select a diary week.')
                return self.reply(200, self.app.diary.week(query['week'][0]))
            if path.startswith('/api/servers/'):
                identifier = path.rsplit('/', 1)[-1]
                if IDENTIFIER.fullmatch(identifier):
                    return self.reply(200, {'server': self.app.store.get(identifier)})
                raise KeyError()
            if path in STATIC_FILES:
                body, content_type, headers = self.server.assets.get(path, self.headers.get('Accept-Encoding', ''))
                cached = matches_etag(self.headers.get('If-None-Match', ''), headers['ETag'])
                return self.reply(304 if cached else 200, b'' if cached else body, content_type,
                                  cache_control='private, max-age=0, must-revalidate', headers=headers)
            self.reply(404, {'error': 'Not found.'})
        except DiaryError as error:
            self.reply(400, {'error': str(error)})
        except KeyError:
            self.reply(404, {'error': 'Server not found.'})

    def do_POST(self):
        self.mutate('POST')

    def do_PATCH(self):
        self.mutate('PATCH')

    def do_DELETE(self):
        self.mutate('DELETE')

    def mutate(self, method):
        if not self.trusted(mutation=True):
            return self.reply(403, {'error': 'Open the panel locally and retry.'})
        try:
            data = self.body()
            path = urlsplit(self.path).path
            diary_day = re.fullmatch(r'/api/diary/(\d{4}-\d{2}-\d{2})', path)
            if diary_day and method == 'PATCH':
                try:
                    return self.reply(200, {'entry': self.app.diary.save(diary_day[1], data)})
                except DiaryConflict as error:
                    return self.reply(409, {'error': str(error), 'entry': error.entry})
            if path == '/api/tasks' and method == 'POST':
                self.app.tasks.add(data)
                return self.reply(201, self.app.tasks.snapshot())
            task = re.fullmatch(r'/api/tasks/([a-f0-9]{32})', path)
            if task and method in {'PATCH', 'DELETE'}:
                try:
                    if method == 'PATCH':
                        self.app.tasks.update(task[1], data)
                    else:
                        if data:
                            raise TaskError('Task deletion does not accept extra fields.')
                        self.app.tasks.delete(task[1])
                except KeyError:
                    return self.reply(404, {'error': 'Task not found.'})
                return self.reply(200, self.app.tasks.snapshot())
            if path == '/api/expenses/settings' and method == 'PATCH':
                self.app.expenses.set_currency(data)
                return self.reply(200, self.app.expenses.snapshot())
            if path == '/api/expenses/rates/refresh' and method == 'POST':
                if data:
                    raise InputError('The rate refresh does not accept custom parameters.')
                self.app.expenses.refresh(force=True)
                return self.reply(200, self.app.expenses.snapshot())
            if path == '/api/expenses' and method == 'POST':
                self.app.expenses.add(data)
                return self.reply(201, self.app.expenses.snapshot())
            expense = re.fullmatch(r'/api/expenses/([a-f0-9]{32})', path)
            if expense and method in {'PATCH', 'DELETE'}:
                if method == 'PATCH':
                    self.app.expenses.update(expense[1], data)
                else:
                    if data:
                        raise InputError('Expense deletion does not accept custom parameters.')
                    self.app.expenses.delete(expense[1])
                return self.reply(200, self.app.expenses.snapshot())
            if path in {'/api/local-port-watch/settings', '/api/local-port-watch/acknowledge'} and method == 'POST':
                return self.reply(410, {'error': 'Port alerts have been removed. Listening ports refresh only on request.'})
            tool_action = re.fullmatch(r'/api/tool-updates/(go|pdtm|go-tools|pipx)/(check|install)', path)
            if tool_action and method == 'POST':
                manager, operation = tool_action.groups()
                try:
                    if operation == 'check':
                        if data:
                            raise InputError('This version check does not accept custom parameters.')
                        self.app.desktop.check_tools(manager)
                    else:
                        if set(data) != {'item', 'version', 'allow_unknown_version'}:
                            raise InputError('Choose a tool and confirm the exact version.')
                        self.app.desktop.launch_tool(manager, data['item'], data['version'], data['allow_unknown_version'])
                except RuntimeError as error:
                    raise InputError(str(error))
                return self.reply(202, self.app.desktop.snapshot())
            manager_action = re.fullmatch(r'/api/desktop-updates/manager/(apt|snap|flatpak)/(check|upgrade|refresh-lists)', path)
            if manager_action and method == 'POST':
                manager, operation = manager_action.groups()
                if data or (operation == 'refresh-lists' and manager != 'apt'):
                    raise InputError('Unsupported package-manager action.')
                try:
                    if operation == 'check':
                        self.app.desktop.check_manager(manager)
                    else:
                        self.app.desktop.launch_manager(manager, operation)
                except RuntimeError as error:
                    raise InputError(str(error))
                return self.reply(202, self.app.desktop.snapshot())
            if path == '/api/rental-settings' and method == 'POST':
                if set(data) != {'enabled'} or not isinstance(data['enabled'], bool):
                    raise InputError('Choose whether desktop reminders are enabled.')
                self.app.rentals.set_enabled(data['enabled'])
                return self.reply(200, {'rentals': self.app.rentals.snapshot()})
            if path in {'/api/pc/check', '/api/pc/refresh-lists', '/api/pc/upgrade', '/api/burp/check', '/api/burp/install', '/api/burp/settings'} and method == 'POST':
                try:
                    if path == '/api/burp/settings':
                        if set(data) != {'weekly'} or not isinstance(data['weekly'], bool):
                            raise InputError('Invalid weekly check setting.')
                        if data['weekly']:
                            raise InputError('Burp updates are checked manually. Use Check version.')
                        self.app.desktop.set_setting('burp_weekly', False)
                    elif path == '/api/burp/install':
                        if set(data) != {'version', 'preserve_launch'}:
                            raise InputError('Review the version and confirm the JAR update.')
                        self.app.desktop.install_burp(data['version'], data['preserve_launch'])
                    else:
                        if data:
                            raise InputError('This action does not accept custom parameters.')
                        if path == '/api/pc/check':
                            self.app.desktop.check_pc()
                        elif path == '/api/burp/check':
                            self.app.desktop.check_burp()
                        else:
                            self.app.desktop.launch_apt(path.rsplit('/', 1)[-1])
                except RuntimeError as error:
                    raise InputError(str(error))
                return self.reply(202, self.app.desktop.snapshot())
            if path == '/api/rental-notification-preview' and method == 'POST':
                try:
                    self.app.rentals.preview()
                except RuntimeError as error:
                    raise InputError(str(error))
                return self.reply(200, {'ok': True, 'rentals': self.app.rentals.snapshot()})
            if path == '/api/servers' and method == 'POST':
                credentials = self.app.credential_fields(data.get('credentials', {}))
                fields = data.get('server', {})
                if not isinstance(fields, dict):
                    raise InputError('Invalid server record.')
                if fields.get('archived') is True:
                    raise InputError('New servers cannot start in Trash.')
                fields['ssh_user'] = 'root'
                with self.app.job_lock:
                    server = self.app.store.add(fields)
                    self.app.store.event(server['id'], 'info', 'Server added. SSH setup started.')
                    job = self.app.submit(server['id'], 'onboard', credentials)
                return self.reply(202, {'server': server, 'job': job})
            if path == '/api/check-all' and method == 'POST':
                if data:
                    raise InputError('Availability checks do not accept custom parameters.')
                return self.reply(202, self.app.check_all())
            if path == '/api/refresh-all' and method == 'POST':
                with self.app.job_lock:
                    jobs = [self.app.submit(s['id'], 'refresh') for s in self.app.store.list() if not s['archived']]
                return self.reply(202, {'jobs': jobs})
            match = re.fullmatch(r'/api/servers/([a-f0-9]{32})(?:/(refresh|onboard|host-key))?', path)
            if match:
                identifier, action = match.groups()
                if method == 'PATCH' and action is None:
                    updated = self.app.update_server(identifier, data)
                    return self.reply(200, {'server': updated})
                if method == 'DELETE' and action is None:
                    self.app.delete_server(identifier, data)
                    return self.reply(200, {'deleted': identifier})
                if action == 'refresh' and method == 'POST':
                    return self.reply(202, {'job': self.app.submit(identifier, 'refresh')})
                if action == 'onboard' and method == 'POST':
                    credentials = self.app.credential_fields(data.get('credentials', {}))
                    return self.reply(202, {'job': self.app.submit(identifier, 'onboard', credentials)})
                if action == 'host-key' and method == 'POST':
                    fingerprint = self.app.accept_host_key(identifier)
                    return self.reply(200, {'fingerprint': fingerprint})
            self.reply(404, {'error': 'Not found.'})
        except ConflictError as error:
            self.reply(409, {'error': str(error)})
        except (InputError, ExpenseError, TaskError, DiaryError) as error:
            self.reply(400, {'error': str(error)})
        except SSHError as error:
            self.reply(400, {'error': str(error), 'kind': error.kind})
        except KeyError:
            self.reply(404, {'error': 'Server not found.'})
        except Exception:
            traceback.print_exc()
            self.reply(500, {'error': 'Action failed. State is saved in the local panel.'})


def make_server(app, port):
    http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    http.daemon_threads = True
    http.app = app
    http.assets = StaticAssets(HERE / 'static')
    return http


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--data-dir', default=str(Path.home() / '.local/share/local-desk'))
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.data_dir, Path.home(), import_existing=False)
    engine = SSHEngine(Path.home(), args.data_dir)
    app = Application(store, engine)
    store.export_connections()
    http = make_server(app, args.port)
    app.rentals.start()
    app.desktop.start()
    print(f'Local Desk: http://127.0.0.1:{http.server_address[1]}', flush=True)
    try:
        # ThreadingHTTPServer still dispatches concurrent requests. This entry
        # point stops on a process signal, not BaseServer.shutdown(), so it can
        # block on socket readiness instead of waking to poll a shutdown flag.
        # Rental/update schedules have their own existing wake events.
        while True:
            http.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        app.rentals.stop()
        app.desktop.stop()
        http.server_close()
        app.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
