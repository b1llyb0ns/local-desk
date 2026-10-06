import base64
import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import ssh_engine
import remote_setup as remote

PUBLIC = 'ssh-ed25519 ' + base64.b64encode(b'public-test-fixture').decode() + ' test'
SNAPSHOT = {'collected_at': '2026-09-05T12:00:00+00:00', 'cpu_pct': 8.2, 'cpu_count': 2,
            'memory': {'pct': 24.2, 'used': 520000000, 'total': 2147483648},
            'disk': {'pct': 30, 'used': 6000000000, 'total': 20000000000},
            'processes': [{'pid': 1, 'name': 'systemd', 'user': 'root', 'cpu_pct': 0.1, 'memory_bytes': 15000000, 'age_seconds': 1000}],
            'services': [{'name': 'ssh.service', 'description': 'OpenSSH', 'pid': 22, 'cpu_pct': 0.1, 'memory_bytes': 20000000, 'memory_scope': 'service'}],
            'containers': [{'name': 'example-web', 'image': 'example:1', 'state': 'running', 'status': 'Up 3 days', 'ports': '127.0.0.1:8000', 'cpu': '0.1%', 'memory': '12MiB / 2GiB'}],
            'packages': [{'name': 'openssh-server', 'version': 'test-1'}], 'manual_tools': [],
            'boot_at': '2026-09-02T12:00:00+00:00', 'uptime_seconds': 259200,
            'hostname': 'fixture', 'os': 'Ubuntu (test)', 'kernel': 'test', 'ports': ['tcp LISTEN 0 128 0.0.0.0:22'], 'failed_units': []}
LOCAL_LISTENERS = {
    'checked_at': '2026-09-05T12:00:00+00:00', 'state': 'ok', 'count': 4,
    'process_visibility': 'limited',
    'process_visibility_notice': 'Process names and PIDs are limited to what the current user can see. Other owners may be hidden; no elevation is used.',
    'exposure_notice': 'Bind scope describes the local address only. It does not establish internet reachability.',
    'skipped_lines': 0, 'truncated': False, 'error': None,
    'listeners': [
        {'protocol': 'tcp', 'state': 'LISTEN', 'address': '127.0.0.1', 'port': 8787,
         'family': 'ipv4', 'exposure': 'loopback', 'processes': [{'name': 'python3', 'pid': 1234}], 'process_visibility': 'visible'},
        {'protocol': 'tcp', 'state': 'LISTEN', 'address': '0.0.0.0', 'port': 22,
         'family': 'ipv4', 'exposure': 'all_interfaces', 'processes': [], 'process_visibility': 'unavailable'},
        {'protocol': 'tcp', 'state': 'LISTEN', 'address': '::1', 'port': 5432,
         'family': 'ipv6', 'exposure': 'loopback', 'processes': [{'name': 'postgres', 'pid': 2345}], 'process_visibility': 'visible'},
        {'protocol': 'udp', 'state': 'UNCONN', 'address': '*', 'port': 5353,
         'family': 'unspecified', 'exposure': 'all_interfaces', 'processes': [{'name': 'avahi', 'pid': 3456}], 'process_visibility': 'visible'},
    ],
}


class FakeEngine:
    fingerprint = 'SHA256:TEST-PUBLIC-KEY'
    def key_options(self): return []
    def collect(self, record): return json.loads(json.dumps(SNAPSHOT))
    def onboard(self, record, credentials, progress):
        for step in range(1, 7): progress(step, 'Test ' + str(step))
        return {'backup': '/root/test-backup', 'ssh_user': 'root'}


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'tmp'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='panel-', dir=directory)
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.record = self.store.add({'name': 'Test', 'alias': 'test-de', 'host': '192.0.2.1'})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_bad_inputs(self):
        for field, values in {
            'host': ['-oProxyCommand=evil', 'a\nHost evil', 'a; id', 'a/b', '', 'a..b'],
            'alias': ['a\nb', 'host command', '-host'],
            'port': [0, 65536, True, '22'],
            'provider_url': ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:pass@host.test'],
            'lease_end': ['2026-02-30', '09-18', '01.01.2026'],
            'notes': ['a\x00b', 'x' * 16001]
        }.items():
            for value in values:
                with self.subTest(field=field, value=str(value)[:30]), self.assertRaises(server.InputError):
                    server.validate_fields({field: value})
        self.assertEqual(server.clean_host('[2001:db8::1]'), '2001:db8::1')
        with self.assertRaises(server.InputError):
            server.validate_fields({'metrics_ipqos': 'ef'})

    def test_notes_dates_and_archive_roundtrip(self):
        text = '<img src=x onerror=alert(1)>\nМои заметки\n'
        self.store.update(self.record['id'], {'notes': text, 'lease_hint': '09-18'})
        self.assertIsNone(self.store.get(self.record['id'])['lease_end'])
        self.store.update(self.record['id'], {'lease_end': '2026-09-18', 'lease_hint': '11-17'})
        self.assertEqual(self.store.get(self.record['id'])['lease_hint'], '')
        self.store.update(self.record['id'], {'archived': True})
        self.assertEqual(self.store.list()[0]['notes'], text)
        self.assertTrue(self.store.list()[0]['archived'])
        self.store.update(self.record['id'], {'archived': False})
        self.assertFalse(self.store.list()[0]['archived'])

    def test_unique_and_connection_change(self):
        with self.assertRaises(server.InputError):
            self.store.add({'name': 'Duplicate', 'alias': 'test-de', 'host': '192.0.2.2'})
        self.store.snapshot(self.record['id'], SNAPSHOT)
        self.store.update(self.record['id'], {'metrics_ipqos': 'ef'}, internal=True)
        self.store.update(self.record['id'], {'host': '192.0.2.2'})
        record = self.store.get(self.record['id'])
        self.assertIsNone(record['snapshot'])
        self.assertIsNone(record['last_seen'])
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(record['metrics_ipqos'], '')

    def test_history_is_bounded_and_previous_snapshot_survives_failure(self):
        for _ in range(78): self.store.snapshot(self.record['id'], SNAPSHOT)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM history').fetchone()[0], 72)
        self.assertEqual(len(self.store.get(self.record['id'])['history']), 36)
        self.store.failure(self.record['id'], ssh_engine.SSHError('No connection', 'unreachable'))
        self.assertIsNotNone(self.store.get(self.record['id'])['snapshot'])

    def test_exports_and_keeps_unrelated_ssh(self):
        ssh = self.home / '.ssh'; ssh.mkdir()
        (ssh / 'config').write_text('Host other\n  HostName other.example\n')
        self.store.export_enabled = True
        self.store.export_connections()
        self.store.export_connections()
        text = (ssh / 'config').read_text()
        self.assertEqual(text.count('Include ~/.ssh/local-desk.conf'), 1)
        self.assertIn('Host other', text)
        self.assertIn('IdentityFile ~/.ssh/id_ed25519', (ssh / 'local-desk.conf').read_text())
        self.assertEqual((ssh / 'local-desk.conf').stat().st_mode & 0o777, 0o600)

    def test_does_not_shadow_unrelated_alias(self):
        ssh = self.home / '.ssh'; ssh.mkdir()
        (ssh / 'config').write_text('Host unrelated\n HostName other.example\n')
        self.store.export_enabled = True
        with self.assertRaises(server.InputError):
            self.store.add({'name': 'No', 'alias': 'unrelated', 'host': '192.0.2.44'})

    def test_import_is_idempotent_and_preserves_unknown_year(self):
        path = self.home / '.local/share/local-desk/import'; path.mkdir(parents=True)
        (path / 'inventory.json').write_text(json.dumps({'servers':[{'alias':'imported','ip':'192.0.2.50','state':'ready'}]}))
        vault = self.home / '.local/share/local-desk/import'; vault.mkdir(parents=True, exist_ok=True)
        (vault / 'payments.md').write_text('| VPS | test | 192.0.2.50 | imported | 09-18 | 5$ | 500 | <https://example.com/> |\n')
        self.store.import_existing()
        item = next(s for s in self.store.list() if s['alias']=='imported')
        self.assertEqual(item['lease_hint'],'09-18');self.assertIsNone(item['lease_end'])
        reopened = server.Store(self.home/'data',self.home,export=False)
        self.assertEqual(len(reopened.list()),2);reopened.db.close()


class HttpTests(StoreTests):
    def setUp(self):
        super().setUp()
        self.app = server.Application(self.store, FakeEngine())
        self.http = server.make_server(self.app, 0)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        self.app.pool.shutdown(wait=True)
        super().tearDown()

    def request(self, path, method='GET', data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
        response = connection.getresponse(); body = response.read(); status = response.status
        connection.close()
        return status, body

    def test_rejects_foreign_origins_hosts_and_missing_csrf(self):
        self.assertEqual(self.request('/api/bootstrap', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('/api/refresh-all', 'POST', {}, {'Content-Type': 'application/json'})[0], 403)
        self.assertEqual(self.request('/../server.py')[0], 404)

    def test_local_listeners_route_reads_local_snapshot_without_ssh(self):
        with mock.patch.object(server, 'local_listener_snapshot', return_value=LOCAL_LISTENERS) as snapshot, \
                mock.patch.object(self.app.engine, 'collect') as collect, \
                mock.patch.object(self.app.engine, 'onboard') as onboard:
            status, body = self.request('/api/local-listeners')
            self.assertEqual(status, 200)
            result = json.loads(body)
            self.assertEqual(result['count'], LOCAL_LISTENERS['count'])
            self.assertEqual(result['state'], 'ok')
            for actual, expected in zip(result['listeners'], LOCAL_LISTENERS['listeners']):
                self.assertEqual({key: actual[key] for key in expected}, expected)
                self.assertNotIn('first_seen', actual)
                self.assertTrue(actual['exposure_reason'])
                self.assertEqual(actual['internet_reachability'], 'not_checked')
            self.assertNotIn('watch', result)
            self.assertNotIn('events', result)
            self.assertFalse(result['stale'])
            snapshot.assert_called_once_with()
            collect.assert_not_called()
            onboard.assert_not_called()

    def test_local_listeners_route_enforces_local_request_guard(self):
        with mock.patch.object(server, 'local_listener_snapshot') as snapshot, \
                mock.patch.object(self.app.engine, 'collect') as collect:
            for path in ['/api/local-listeners', '/api/local-listeners/cached']:
                for headers in [
                    {'Host': 'other.example'},
                    {'Origin': 'https://other.example'},
                    {'Sec-Fetch-Site': 'cross-site'},
                ]:
                    with self.subTest(path=path, headers=headers):
                        self.assertEqual(self.request(path, headers=headers)[0], 403)
            snapshot.assert_not_called()
            collect.assert_not_called()

    def test_cached_local_listeners_do_not_collect_again(self):
        with mock.patch.object(server, 'local_listener_snapshot', return_value=LOCAL_LISTENERS) as snapshot, \
                mock.patch.object(self.app.engine, 'collect') as collect:
            status, body = self.request('/api/local-listeners/cached')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['state'], 'pending')
            snapshot.assert_not_called()
            fresh = json.loads(self.request('/api/local-listeners')[1])
            snapshot.assert_called_once_with()
            snapshot.reset_mock()
            cached = json.loads(self.request('/api/local-listeners/cached')[1])
            self.assertTrue(cached['cached'])
            self.assertEqual(cached['listeners'], fresh['listeners'])
            snapshot.assert_not_called()
            collect.assert_not_called()

    def test_retired_port_alert_routes_cannot_reenable_or_acknowledge_anything(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        with mock.patch.object(server, 'local_listener_snapshot') as snapshot:
            for path in ['/api/local-port-watch/settings', '/api/local-port-watch/acknowledge']:
                self.assertEqual(self.request(path, 'POST', {}, {'Content-Type': 'application/json'})[0], 403)
                self.assertEqual(self.request(path, 'POST', {}, dict(headers, Origin='https://other.example'))[0], 403)
            for path in ['/api/local-port-watch/settings', '/api/local-port-watch/acknowledge']:
                for value in [{}, {'enabled': True}, {'enabled': False}, {'ids': []}]:
                    status, body = self.request(path, 'POST', value, headers)
                    self.assertEqual(status, 410)
                    self.assertIn('removed', json.loads(body)['error'])
            snapshot.assert_not_called()

    def test_changed_listeners_never_call_notification_sender_or_return_alerts(self):
        initial = dict(LOCAL_LISTENERS, listeners=[row for row in LOCAL_LISTENERS['listeners'] if row['exposure'] == 'loopback'], count=2)
        with mock.patch.object(server, 'local_listener_snapshot', side_effect=[initial, LOCAL_LISTENERS]) as snapshot, \
                mock.patch.object(self.app.rentals, 'deliver') as send:
            self.request('/api/local-listeners')
            changed = json.loads(self.request('/api/local-listeners')[1])
            self.assertEqual(changed['count'], 4)
            self.assertNotIn('watch', changed)
            self.assertNotIn('events', changed)
            send.assert_not_called()
            self.assertEqual(snapshot.call_count, 2)

    def test_legacy_queued_port_alert_metadata_is_never_loaded_changed_or_delivered(self):
        key = 'local_port_watch_state'
        saved = json.dumps({'version': 1, 'enabled': True, 'initialized': True, 'baseline': {}, 'seen': {},
                            'events': [{'id': 'old-tcp', 'protocol': 'tcp', 'notified_at': None, 'acknowledged': False}]})
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', (key, saved))
        statements = []
        self.store.db.set_trace_callback(statements.append)
        with mock.patch.object(server, 'local_listener_snapshot', return_value=LOCAL_LISTENERS) as collector, \
                mock.patch.object(server.RentalAlerts, 'deliver') as sender:
            reopened = server.Application(self.store, FakeEngine())
            try:
                self.assertEqual(reopened.local_ports.cached_inventory()['state'], 'pending')
                collector.assert_not_called()
                self.assertNotIn('watch', reopened.local_ports.refresh())
                self.assertFalse(hasattr(reopened, 'port_watch'))
                self.assertFalse(hasattr(reopened.local_ports, 'start'))
                sender.assert_not_called()
            finally:
                reopened.pool.shutdown(wait=True)
        self.store.db.set_trace_callback(None)
        self.assertFalse(any(key in statement for statement in statements))
        with self.store.lock:
            self.assertEqual(self.store.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()['value'], saved)

    def test_socket_collection_failure_marks_previous_result_stale(self):
        with mock.patch.object(server, 'local_listener_snapshot', side_effect=[LOCAL_LISTENERS, OSError('Unavailable')]):
            first = json.loads(self.request('/api/local-listeners')[1])
            stale = json.loads(self.request('/api/local-listeners')[1])
            self.assertEqual(stale['listeners'], first['listeners'])
            self.assertEqual(stale['checked_at'], first['checked_at'])
            self.assertTrue(stale['stale'])
            self.assertTrue(stale['cached'])
            self.assertEqual(stale['state'], 'error')

    def test_notes_api_and_no_secrets(self):
        status, body = self.request('/api/bootstrap')
        bootstrap = json.loads(body); self.assertEqual(status, 200)
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': bootstrap['csrf']}
        url = '/api/servers/' + self.record['id']
        note = 'Записать назначение\n<script>never run</script>'
        self.assertEqual(self.request(url, 'PATCH', {'notes': note}, headers)[0], 200)
        self.assertEqual(json.loads(self.request(url)[1])['server']['notes'], note)
        self.assertEqual(self.request(url, 'PATCH', {'snapshot': {}}, headers)[0], 400)
        self.assertEqual(self.request('/api/servers', 'POST', {'server': 'bad', 'credentials': {'mode': 'managed'}}, headers)[0], 400)
        self.assertNotIn(b'PRIVATE KEY', body)

    def test_check_all_requires_csrf_and_rejects_custom_parameters(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        with mock.patch.object(self.app, 'check_all', return_value={'jobs': [], 'skipped': []}) as check:
            self.assertEqual(self.request('/api/check-all', 'POST', {})[0], 403)
            self.assertEqual(self.request('/api/check-all', 'POST', {'host': '192.0.2.9'}, headers)[0], 400)
            check.assert_not_called()
            status, body = self.request('/api/check-all', 'POST', {}, headers)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {'jobs': [], 'skipped': []})
        check.assert_called_once_with()

    def test_refresh_learns_and_reuses_metrics_ipqos_after_keepalive_timeout(self):
        class CompatibilityEngine(FakeEngine):
            def __init__(self):
                self.calls = []

            def collect(self, record):
                mode = record.get('metrics_ipqos', '')
                self.calls.append(mode)
                if not mode:
                    raise ssh_engine.SSHError('Server response timed out.', 'timeout', 'ipqos_ef')
                return super().collect(record)

        compatibility = CompatibilityEngine()
        self.app.engine = compatibility

        def refresh(job_id):
            self.app.jobs[job_id] = {'id': job_id, 'server_id': self.record['id'], 'kind': 'refresh',
                                     'state': 'queued', 'step': 0, 'message': 'Queued',
                                     'started_at': server.utcnow()}
            self.app.busy[self.record['id']] = job_id
            self.app.work(job_id, {})

        refresh('compatibility-first')
        self.assertEqual(self.app.jobs['compatibility-first']['state'], 'done')
        self.assertEqual(compatibility.calls, ['', 'ef'])
        self.assertEqual(self.store.get(self.record['id'])['metrics_ipqos'], 'ef')

        refresh('compatibility-reused')
        self.assertEqual(self.app.jobs['compatibility-reused']['state'], 'done')
        self.assertEqual(compatibility.calls, ['', 'ef', 'ef'])

    def test_onboard_saves_working_transport_and_reuses_it_for_snapshot(self):
        class CompatibilityEngine(FakeEngine):
            def onboard(self, record, credentials, progress):
                record['metrics_ipqos'] = 'ef'
                return super().onboard(record, credentials, progress)

            def collect(self, record):
                if record.get('metrics_ipqos') != 'ef':
                    raise ssh_engine.SSHError('Connection timed out.', 'timeout')
                return super().collect(record)

        self.app.engine = CompatibilityEngine()
        job_id = 'initial-connection'
        self.app.jobs[job_id] = {'id': job_id, 'server_id': self.record['id'], 'kind': 'onboard',
                                 'state': 'queued', 'step': 0, 'message': 'Queued',
                                 'started_at': server.utcnow()}
        self.app.busy[self.record['id']] = job_id
        self.app.work(job_id, {'mode': 'managed'})
        self.assertEqual(self.app.jobs[job_id]['state'], 'done')
        record = self.store.get(self.record['id'])
        self.assertEqual(record['metrics_ipqos'], 'ef')
        self.assertEqual(record['state'], 'ready')
        self.assertIsNotNone(record['snapshot'])

    def test_rental_settings_and_preview_are_local_and_csrf_protected(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        with mock.patch.object(self.app.engine, 'collect') as collect, mock.patch.object(self.app.rentals, 'sender') as send:
            self.app.rentals.available = True
            code, body = self.request('/api/rental-alerts')
            self.assertEqual(code, 200)
            self.assertEqual(len(json.loads(body)['rentals']['undated']), 1)
            for path in ['/api/rental-settings', '/api/rental-notification-preview']:
                self.assertEqual(self.request(path, 'POST', {}, {'Content-Type': 'application/json'})[0], 403)
            self.assertEqual(self.request('/api/rental-settings', 'POST', {'enabled': 'false'}, headers)[0], 400)
            self.assertEqual(self.request('/api/rental-settings', 'POST', {}, headers)[0], 400)
            code, body = self.request('/api/rental-settings', 'POST', {'enabled': False}, headers)
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(body)['rentals']['notifications']['enabled'])
            self.assertEqual(self.request('/api/rental-notification-preview', 'POST', {}, headers)[0], 200)
            send.assert_called_once()
            collect.assert_not_called()

    def test_quick_note_api_can_save_during_ssh_task(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        self.app.busy[self.record['id']] = 'fixture-task'
        url = '/api/servers/' + self.record['id']
        with mock.patch.object(self.app.engine, 'collect') as collect:
            code, body = self.request(url, 'PATCH', {'short_note': 'Brief note'}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['server']['short_note'], 'Brief note')
            self.assertEqual(self.request(url, 'PATCH', {'short_note': 'x' * 161}, headers)[0], 400)
            collect.assert_not_called()

    def test_desktop_actions_require_csrf_and_reject_custom_commands(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        with mock.patch.object(self.app.desktop, 'launch_apt') as launch, mock.patch.object(self.app.desktop, 'install_burp') as install:
            self.assertEqual(self.request('/api/desktop-updates')[0], 200)
            self.assertEqual(self.request('/api/pc/upgrade', 'POST', {}, {'Content-Type': 'application/json'})[0], 403)
            self.assertEqual(self.request('/api/pc/upgrade', 'POST', {'command': 'arbitrary'}, headers)[0], 400)
            self.assertEqual(self.request('/api/burp/install', 'POST', {'url': 'https://example.com/file'}, headers)[0], 400)
            launch.assert_not_called()
            install.assert_not_called()
            self.assertEqual(self.request('/api/pc/upgrade', 'POST', {}, headers)[0], 202)
            launch.assert_called_once_with('upgrade')

    def test_package_manager_routes_dispatch_only_fixed_manager_actions(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        with mock.patch.object(self.app.desktop, 'check_manager') as check, \
                mock.patch.object(self.app.desktop, 'launch_manager') as launch, \
                mock.patch.object(self.app.desktop, 'snapshot', return_value={'pc': {}, 'jobs': {}}):
            for manager in ['apt', 'snap', 'flatpak']:
                self.assertEqual(self.request(f'/api/desktop-updates/manager/{manager}/check', 'POST', {}, headers)[0], 202)
                self.assertEqual(self.request(f'/api/desktop-updates/manager/{manager}/upgrade', 'POST', {}, headers)[0], 202)
            self.assertEqual(self.request('/api/desktop-updates/manager/apt/refresh-lists', 'POST', {}, headers)[0], 202)
            self.assertEqual(check.call_args_list, [mock.call('apt'), mock.call('snap'), mock.call('flatpak')])
            self.assertEqual(launch.call_args_list, [mock.call('apt', 'upgrade'), mock.call('snap', 'upgrade'), mock.call('flatpak', 'upgrade'), mock.call('apt', 'refresh-lists')])

    def test_package_manager_routes_reject_missing_csrf_custom_payloads_and_unknown_actions(self):
        headers = {'Content-Type': 'application/json', 'X-VPS-CSRF': self.app.csrf}
        with mock.patch.object(self.app.desktop, 'check_manager') as check, \
                mock.patch.object(self.app.desktop, 'launch_manager') as launch:
            path = '/api/desktop-updates/manager/apt/upgrade'
            self.assertEqual(self.request(path, 'POST', {}, {'Content-Type': 'application/json'})[0], 403)
            self.assertEqual(self.request(path, 'POST', {'command': 'custom'}, headers)[0], 400)
            self.assertEqual(self.request(path, 'POST', {'packages': ['openssl']}, headers)[0], 400)
            for manager in ['snap', 'flatpak']:
                self.assertEqual(self.request(f'/api/desktop-updates/manager/{manager}/refresh-lists', 'POST', {}, headers)[0], 400)
            for path in ['/api/desktop-updates/manager/other/upgrade', '/api/desktop-updates/manager/apt/install', '/api/desktop-updates/manager/apt/upgrade/extra']:
                self.assertEqual(self.request(path, 'POST', {}, headers)[0], 404)
            check.assert_not_called()
            launch.assert_not_called()


class SSHTests(unittest.TestCase):
    def engine(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.public_key = PUBLIC
        engine.probe = mock.Mock(return_value={'uid': 0, 'sudo': True})
        engine.controller = mock.Mock(side_effect=lambda s,action,*a,**kw: {'ok': True, 'backup': '/root/backup', 'phase': 'rolled_back' if action == 'rollback' else 'committed'})
        return engine

    def test_order_independent_verification_before_password_disable_and_commit(self):
        engine = self.engine(); calls = []
        engine.probe.side_effect = lambda *a,**kw: (calls.append('probe') or {'uid':0,'sudo':True})
        engine.controller.side_effect = lambda s,action,*a,**kw: (calls.append(action) or {'backup':'/root/backup'})
        credentials = {'mode': 'managed', 'user': 'root'}
        engine.onboard({}, credentials, lambda *a: None)
        self.assertEqual(calls, ['probe','prepare','probe','finalize','probe','commit'])
        self.assertEqual(credentials, {})

    def test_first_failed_key_check_never_disables_password(self):
        engine = self.engine()
        engine.probe.side_effect = [{'uid':0,'sudo':True}, ssh_engine.SSHError('key failed','credentials_needed')]
        with self.assertRaisesRegex(ssh_engine.SSHError, 'restored'):
            engine.onboard({}, {'mode':'managed'}, lambda *a: None)
        self.assertEqual([c.args[1] for c in engine.controller.call_args_list], ['prepare','rollback'])

    def test_final_failed_key_check_rolls_back(self):
        engine = self.engine()
        engine.probe.side_effect = [{'uid':0,'sudo':True},{'uid':0,'sudo':True},ssh_engine.SSHError('final failed')]
        with self.assertRaises(ssh_engine.SSHError): engine.onboard({}, {'mode':'managed'}, lambda *a: None)
        self.assertEqual([c.args[1] for c in engine.controller.call_args_list], ['prepare','finalize','rollback'])

    def test_lost_commit_response_does_not_claim_false_rollback(self):
        engine = self.engine()
        def control(s, action, *args, **kwargs):
            if action == 'commit': raise ssh_engine.SSHError('network lost')
            return {'backup': '/root/backup', 'phase': 'committed'}
        engine.controller.side_effect = control
        self.assertEqual(engine.onboard({}, {'mode':'managed'}, lambda *a: None)['ssh_user'], 'root')

    def test_prepare_interruption_mentions_timer(self):
        engine = self.engine();engine.controller.side_effect=ssh_engine.SSHError('network lost')
        with self.assertRaisesRegex(ssh_engine.SSHError, 'rollback timer'):
            engine.onboard({}, {'mode':'managed'}, lambda *a:None)

    def test_password_channel_has_no_secret_in_environment(self):
        with ssh_engine.password_channel('test-secret-value') as env:
            self.assertNotIn('test-secret-value', json.dumps(env))
            s=socket.socket(socket.AF_UNIX);s.connect(env['VPS_DESK_SECRET_SOCKET']);s.sendall(b'password\n')
            self.assertEqual(s.recv(100), b'test-secret-value\n');s.close()
        self.assertFalse(Path(env['VPS_DESK_SECRET_SOCKET']).exists())

    def test_askpass_executable_receives_secret_without_disk_file(self):
        with ssh_engine.password_channel('short-lived-test-value') as env:
            result=subprocess.run([env['SSH_ASKPASS'],"root@192.0.2.1's password:"],env=dict(os.environ,**env),capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,0)
            self.assertEqual(result.stdout.strip(),'short-lived-test-value')

    def test_subprocess_limits(self):
        self.assertEqual(ssh_engine.limited_run([sys.executable, '-c', 'print("ok")'])[1].strip(), 'ok')
        with self.assertRaises(ssh_engine.SSHError): ssh_engine.limited_run([sys.executable, '-c', 'print("x"*100000)'], maximum=1000)
        with self.assertRaises(ssh_engine.SSHError): ssh_engine.limited_run([sys.executable, '-c', 'import time;time.sleep(2)'], timeout=.05)

    def test_ipqos_is_explicit_and_limited_to_supported_mode(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.key = Path('/test/id_ed25519')
        engine.known_hosts = Path('/test/known_hosts')
        record = {'host': '192.0.2.10', 'port': 22, 'ssh_user': 'root'}
        with mock.patch.object(ssh_engine, 'limited_run', return_value=(0, '', '')) as execute:
            engine.run(record, 'true', ipqos='ef')
            self.assertIn('IPQoS=ef', execute.call_args.args[0])
            engine.run(record, 'true')
            self.assertNotIn('IPQoS=ef', execute.call_args.args[0])
        with self.assertRaises(ValueError):
            engine.run(record, 'true', ipqos='invalid')

    def test_collect_applies_only_saved_metrics_ipqos(self):
        engine = object.__new__(ssh_engine.SSHEngine)
        engine.run = mock.Mock(return_value=(0, json.dumps(SNAPSHOT), ''))
        engine.collect({'ssh_user': 'root', 'metrics_ipqos': 'ef'})
        self.assertEqual(engine.run.call_args.kwargs['ipqos'], 'ef')


class RemoteTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='local-desk-remote-test-');self.root=Path(self.tmp.name)
        self.config=self.root/'sshd_config';self.config.write_text('PasswordAuthentication yes\nPermitRootLogin yes\n')
        self.managed=self.root/'00-local-desk.conf';self.home=self.root/'root';(self.home/'.ssh').mkdir(parents=True)
        self.auth=self.home/'.ssh/authorized_keys';self.original='ssh-ed25519 '+base64.b64encode(b'OLD-KEY').decode()+' old\n';self.auth.write_text(self.original)
        self.base=self.root/'transactions';self.base.mkdir();self.token='1'*24
        self.settings={'authorizedkeysfile':'.ssh/authorized_keys .ssh/authorized_keys2','authenticationmethods':'any','permitrootlogin':'yes'}
        self.calls=[];self.stack=contextlib.ExitStack()
        for name,value in [('ROOT_HOME',self.home),('BASE',self.base),('CONFIG',self.config),('MANAGED',self.managed),('SSHD','fake-sshd')]:self.stack.enter_context(mock.patch.object(remote,name,value))
        self.stack.enter_context(mock.patch.object(remote,'run',side_effect=self.fake_run))
        self.stack.enter_context(mock.patch.object(remote,'effective',side_effect=lambda:dict(self.settings)))
        self.stack.enter_context(mock.patch.object(remote.os,'chown'))
        self.stack.enter_context(mock.patch.object(remote.os,'getuid',return_value=0))
        self.stack.enter_context(mock.patch.object(remote.shutil,'which',return_value='/test/bin'))

    def tearDown(self):self.stack.close();self.tmp.cleanup()

    def fake_run(self,args,check=True):
        self.calls.append(args)
        if args[:2]==['systemctl','is-active']:return 'active'
        return ''

    def prepare(self):
        directory=self.base/self.token;directory.mkdir()
        return remote.prepare(directory, {'public_key':PUBLIC,'controller_source':'# test','only_managed':True})

    def action(self,name):
        with mock.patch.object(sys,'argv',['test',name,self.token]):return remote.main()

    def final_settings(self):self.settings.update(pubkeyauthentication='yes',passwordauthentication='no',kbdinteractiveauthentication='no',authenticationmethods='publickey',permitrootlogin='without-password')

    def test_prepare_preserves_existing_access_and_arms_rollback_first(self):
        self.prepare()
        self.assertIn(self.original.strip(),self.auth.read_text());self.assertIn(PUBLIC,self.auth.read_text())
        self.assertNotIn('PasswordAuthentication no',self.managed.read_text())
        self.assertNotIn('AuthenticationMethods',self.managed.read_text())
        self.assertIn('PermitRootLogin yes',self.managed.read_text())
        self.assertTrue(any(call[0]=='systemd-run' for call in self.calls))
        self.assertTrue(self.config.read_text().startswith('Include '+str(self.managed)))

    def test_prepare_password_only_allows_independent_key_verification(self):
        self.settings['authenticationmethods'] = 'password'
        self.prepare()
        self.assertIn('AuthenticationMethods publickey password\n', self.managed.read_text())
        self.assertNotIn('AuthenticationMethods any', self.managed.read_text())
        self.assertNotIn('PasswordAuthentication no', self.managed.read_text())

    def test_finalize_only_managed_and_commit_protects_against_late_timer(self):
        self.prepare();self.final_settings();self.action('finalize')
        self.assertEqual(self.auth.read_text(),PUBLIC+'\n')
        self.assertIn('PasswordAuthentication no',self.managed.read_text())
        self.action('commit');self.action('rollback')
        self.assertEqual(self.auth.read_text(),PUBLIC+'\n')
        self.assertEqual(json.loads((self.base/self.token/'state.json').read_text())['phase'],'committed')

    def test_rollback_restores_configs_and_old_keys_exactly(self):
        original=self.config.read_text();self.prepare();self.final_settings();self.action('finalize');self.action('rollback')
        self.assertEqual(self.auth.read_text(),self.original);self.assertEqual(self.config.read_text(),original)
        self.assertFalse(self.managed.exists())

    def test_match_rules_rejected_before_mutation(self):
        self.config.write_text('Match User example\n PasswordAuthentication no\n')
        with self.assertRaisesRegex(RuntimeError,'Match'):self.prepare()
        self.assertEqual(self.auth.read_text(),self.original);self.assertFalse(self.managed.exists())

    def test_extra_key_sources_rejected(self):
        self.settings['authorizedkeyscommand']='/custom/keys'
        with self.assertRaisesRegex(RuntimeError,'Additional'):self.prepare()
        self.assertEqual(self.auth.read_text(),self.original)

    def test_policy_validation_failure_rolls_back(self):
        original=self.config.read_text();self.prepare()
        with self.assertRaisesRegex(RuntimeError,'policy'):self.action('finalize')
        self.assertEqual(self.auth.read_text(),self.original);self.assertEqual(self.config.read_text(),original)


if __name__=='__main__':unittest.main()
