"""Small, file-versioned cache for public UI assets; never caches API data."""
import gzip
import hashlib
import mimetypes
from pathlib import Path
import threading


FILES = {'/': 'index.html', '/app.js': 'app.js', '/tasks.js': 'tasks.js', '/style.css': 'style.css',
         '/diary.js': 'diary.js',
         '/dark.css': 'dark.css', '/desktop.js': 'desktop.js', '/expenses.js': 'expenses.js', '/icon.svg': 'icon.svg'}


def accepts_gzip(header):
    qualities = {}
    for entry in header.lower().split(','):
        parts = [part.strip() for part in entry.split(';')]
        quality = 1.0
        try:
            for parameter in parts[1:]:
                if parameter.startswith('q='):
                    quality = float(parameter[2:])
            if not 0 <= quality <= 1:
                quality = 0
        except ValueError:
            quality = 0
        qualities[parts[0]] = quality
    return qualities.get('gzip', qualities.get('*', 0)) > 0


def matches_etag(header, etag):
    return any(value.strip().removeprefix('W/') in {'*', etag} for value in header.split(','))


class StaticAssets:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.entries = {}

    def get(self, route, accept_encoding=''):
        filename = FILES[route]
        path = self.directory / filename
        with self.lock:
            info = path.stat()
            fingerprint = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            cached = self.entries.get(filename)
            if cached is None or cached['fingerprint'] != fingerprint:
                plain = path.read_bytes()
                packed = gzip.compress(plain, compresslevel=6, mtime=0) if len(plain) >= 512 else plain
                cached = {'fingerprint': fingerprint, 'plain': plain,
                          'gzip': packed if len(packed) < len(plain) else None,
                          'type': mimetypes.guess_type(filename)[0] or 'application/octet-stream'}
                cached['plain_etag'] = '"' + hashlib.sha256(plain).hexdigest() + '"'
                if cached['gzip'] is not None:
                    cached['gzip_etag'] = '"' + hashlib.sha256(cached['gzip']).hexdigest() + '"'
                self.entries[filename] = cached
            compressed = cached['gzip'] is not None and accepts_gzip(accept_encoding)
            key = 'gzip' if compressed else 'plain'
            headers = {'ETag': cached[key + '_etag'], 'Vary': 'Accept-Encoding'}
            if compressed:
                headers['Content-Encoding'] = 'gzip'
            return cached[key], cached['type'], headers
