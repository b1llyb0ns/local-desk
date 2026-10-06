"""Bounded read-only HTTP measurements against the local dashboard."""
import argparse
import json
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
ASSETS = ['/', '/style.css', '/dark.css', '/app.js', '/desktop.js', '/icon.svg']
API = ['/api/servers', '/api/desktop-updates?view=pc', '/api/desktop-updates?view=burp', '/api/desktop-updates/status']


def measure(base, path, repeats):
    durations, sizes, statuses, validators = [], [], [], {}
    for _ in range(repeats):
        headers = {'User-Agent': UA, 'Accept-Encoding': 'gzip', **validators}
        start = time.perf_counter()
        try:
            response = urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            durations.append((time.perf_counter() - start) * 1000)
            sizes.append(len(body))
            statuses.append(response.status)
            if path in ASSETS and response.headers.get('ETag'):
                validators = {'If-None-Match': response.headers['ETag']}
    return {'requests': repeats, 'median_ms': round(statistics.median(durations), 3),
            'max_ms': round(max(durations), 3), 'first_body_bytes': sizes[0],
            'total_body_bytes': sum(sizes), 'statuses': statuses}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8787')
    parser.add_argument('--output', required=True)
    parser.add_argument('--repeats', type=int, default=8)
    args = parser.parse_args()
    if args.url not in {'http://127.0.0.1:8787', 'http://127.0.0.1:8788'} or not 1 <= args.repeats <= 12:
        parser.error('Use the local panel or isolated fixture, with 1–12 requests per route.')
    report = {path: measure(args.url, path, args.repeats) for path in API + ASSETS}
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
