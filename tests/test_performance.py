import copy
from contextlib import closing
import gzip
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from static_assets import StaticAssets, accepts_gzip, matches_etag
from test_panel import FakeEngine, SNAPSHOT

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'


class StorePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='performance-', dir=Path(__file__).resolve().parents[1] / 'tmp')
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.record = self.store.add({'name': 'Germany', 'alias': 'server-a', 'host': '192.0.2.1'})
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot['packages'] = [{'name': 'package-' + str(index), 'version': '1.0'} for index in range(1000)]
        self.store.snapshot(self.record['id'], snapshot)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_warm_summary_avoids_full_json_and_history_queries(self):
        first = self.store.list()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        with mock.patch.object(server.json, 'loads', wraps=json.loads) as loads:
            second = self.store.list()
            loads.assert_not_called()
        self.store.db.set_trace_callback(None)
        self.assertEqual(first, second)
        self.assertEqual(statements, ['PRAGMA data_version'])
        self.assertNotIn('packages', second[0]['snapshot'])
        self.assertEqual(len(self.store.get(self.record['id'])['snapshot']['packages']), 1000)

    def test_cached_objects_are_independent_and_writes_invalidate(self):
        first = self.store.list()
        first[0]['name'] = 'Changed locally'
        first[0]['snapshot']['memory']['pct'] = 99
        first[0]['history'].clear()
        current = self.store.list()[0]
        self.assertEqual(current['name'], 'Germany')
        self.assertEqual(current['snapshot']['memory']['pct'], SNAPSHOT['memory']['pct'])
        self.assertEqual(len(current['history']), 1)
        self.store.update(self.record['id'], {'notes': 'New note'})
        self.assertEqual(self.store.list()[0]['notes'], 'New note')
        with closing(sqlite3.connect(self.store.path)) as external, external:
            external.execute('UPDATE servers SET name=? WHERE id=?', ('Renamed', self.record['id']))
        self.assertEqual(self.store.list()[0]['name'], 'Renamed')

    def test_history_uses_server_index(self):
        plan = self.store.db.execute('EXPLAIN QUERY PLAN SELECT at,cpu,memory,disk FROM history WHERE server_id=? ORDER BY id DESC LIMIT 36', (self.record['id'],))
        self.assertTrue(any('history_server_id' in row[3] for row in plan))

    def test_notes_export_updates_legacy_without_rewriting_ssh(self):
        legacy = self.home / '.local/share/local-desk/import/inventory.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"servers": []}')
        self.store.export_enabled = True
        with mock.patch.object(self.store, 'list', side_effect=AssertionError('Full inventory used for export')):
            self.store.export_connections()
        managed = self.home / '.ssh/local-desk.conf'
        identity = lambda path: (path.stat().st_ino, path.stat().st_mtime_ns)
        before = identity(managed)
        with mock.patch.object(self.store, 'list', side_effect=AssertionError('Full inventory used for export')):
            self.store.update(self.record['id'], {'notes': 'Visible in both inventories'})
        self.assertEqual(identity(managed), before)
        self.assertEqual(json.loads(legacy.read_text())['servers'][0]['notes'], 'Visible in both inventories')
        self.assertEqual(json.loads(legacy.read_text())['servers'][0]['last_boot_utc'], SNAPSHOT['boot_at'])
        self.store.update(self.record['id'], {'host': '192.0.2.2'})
        self.assertNotEqual(identity(managed), before)
        self.assertIn('HostName 192.0.2.2', managed.read_text())

    def test_identical_atomic_write_preserves_inode_and_repairs_permissions(self):
        path = self.home / 'settings.txt'
        server.atomic_file(path, 'settings\n')
        before = path.stat()
        server.atomic_file(path, 'settings\n')
        self.assertEqual(path.stat().st_ino, before.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        path.chmod(0o644)
        server.atomic_file(path, 'settings\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        link = self.home / 'settings-link.txt'
        link.symlink_to(path)
        with self.assertRaises(server.InputError):
            server.atomic_file(link, 'settings\n')


class AssetPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='assets-', dir=Path(__file__).resolve().parents[1] / 'tmp')
        self.home = Path(self.tmp.name)
        self.store = server.Store(self.home / 'data', self.home, import_existing=False, export=False)
        self.app = server.Application(self.store, FakeEngine())
        self.http = server.make_server(self.app, 0)
        self.asset = self.home / 'app.js'
        self.asset.write_text('/* Local application */\n' * 300)
        self.http.assets = StaticAssets(self.home)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.app.pool.shutdown(wait=True)
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.request('GET', path, headers={'User-Agent': UA, **(headers or {})})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_compressed_assets_and_conditional_revalidation(self):
        status, packed, headers = self.request('/app.js', {'Accept-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertEqual(gzip.decompress(packed), self.asset.read_bytes())
        self.assertLess(len(packed), self.asset.stat().st_size // 4)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(headers['Vary'], 'Accept-Encoding')
        self.assertIn('must-revalidate', headers['Cache-Control'])
        status, body, cached = self.request('/app.js', {'Accept-Encoding': 'gzip', 'If-None-Match': headers['ETag']})
        self.assertEqual((status, body), (304, b''))
        self.assertNotIn('Content-Length', cached)
        self.assertEqual(cached['ETag'], headers['ETag'])
        plain = self.request('/app.js', {'Accept-Encoding': 'gzip;q=0, *;q=1'})
        self.assertNotIn('Content-Encoding', plain[2])
        self.assertNotEqual(plain[2]['ETag'], headers['ETag'])

    def test_asset_cache_detects_content_edits_and_never_caches_private_api(self):
        previous = self.request('/app.js')[2]['ETag']
        before = self.asset.stat()
        self.asset.write_text('/* Newer application */\n' * 300)
        os.utime(self.asset, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed = self.request('/app.js', {'If-None-Match': previous})
        self.assertEqual(changed[0], 200)
        self.assertNotEqual(changed[2]['ETag'], previous)
        status, body, headers = self.request('/api/servers', {'If-None-Match': '*'})
        self.assertEqual(status, 200)
        self.assertIn('servers', json.loads(body))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', headers)
        self.assertEqual(self.request('/app.js', {'Origin': 'https://example.org', 'If-None-Match': '*'})[0], 403)

    def test_view_and_status_routes(self):
        with mock.patch.object(self.app.desktop, 'snapshot', return_value={'pc': {}, 'jobs': {}}) as snapshot:
            self.assertEqual(self.request('/api/desktop-updates?view=pc')[0], 200)
            snapshot.assert_called_once_with(view='pc')
            for suffix in ('view=', 'view=other', 'view=pc&view=burp'):
                self.assertEqual(self.request('/api/desktop-updates?' + suffix)[0], 400)
        with mock.patch.object(self.app.desktop, 'status', return_value={'jobs': {}, 'revisions': {}}) as status:
            self.assertEqual(self.request('/api/desktop-updates/status')[0], 200)
            status.assert_called_once_with()

    def test_encoding_and_validator_parsing(self):
        self.assertTrue(accepts_gzip('br, gzip;q=0.5'))
        self.assertTrue(accepts_gzip('*;q=1'))
        for header in ('', 'gzip;q=0', 'gzip;q=broken', 'gzip;q=nan', 'gzip;q=0,*;q=1'):
            self.assertFalse(accepts_gzip(header))
        self.assertTrue(matches_etag('"older", W/"current"', '"current"'))
        self.assertTrue(matches_etag('*', '"current"'))
        self.assertFalse(matches_etag('"other"', '"current"'))


if __name__ == '__main__':
    unittest.main()
