"""PC package updates and official Burp stable-release management. No SSH."""
import copy
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

from pc_packages import manager_binary
from local_tool_job import TOOL_MANAGERS

HERE = Path(__file__).resolve().parent
DOWNLOADS = 'https://portswigger.net/burp/downloads'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
VERSION = re.compile(r'20\d{2}(?:\.\d{1,3}){1,3}')
MANAGER_ORDER = ('apt', 'snap', 'flatpak', 'go', 'pdtm', 'go-tools', 'pipx')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def official_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'portswigger.net' or parsed.username or parsed.password:
        raise RuntimeError('Only official HTTPS downloads from portswigger.net are supported.')
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, official_url(newurl))


def open_official(url):
    request = urllib.request.Request(official_url(url), headers={'User-Agent': USER_AGENT})
    return urllib.request.build_opener(OfficialRedirect()).open(request, timeout=25)


def fetch_page(url):
    with open_official(url) as response:
        content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise RuntimeError('Release metadata exceeds the size limit.')
    return content.decode('utf-8')


class ReleaseParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.version = self.sha256 = self.download = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('id') == 'CurrentVersion':
            self.version = attrs.get('value', '')
        if tag == 'option' and attrs.get('value', '').lower() == 'jar' and attrs.get('buildcategoryid') == 'desktop':
            self.sha256 = attrs.get('sha256checksum', '').lower()
        if tag == 'a' and 'download' in attrs:
            href = urllib.parse.urljoin('https://portswigger.net', attrs.get('href', ''))
            parsed = urllib.parse.urlsplit(href)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == '/burp/releases/startdownload' and query.get('product') == ['desktop'] and query.get('type') == ['jar']:
                self.download = official_url(href)


def stable_release(fetch=fetch_page):
    listing = ReleaseParser()
    listing.feed(fetch(DOWNLOADS))
    if not VERSION.fullmatch(listing.version):
        raise RuntimeError('Cannot identify the official stable version. No files changed.')
    release_url = 'https://portswigger.net/burp/releases/professional-community-' + listing.version.replace('.', '-')
    details = ReleaseParser()
    details.feed(fetch(release_url))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(details.download).query)
    if not re.fullmatch(r'[a-f0-9]{64}', details.sha256) or query.get('version') != [listing.version]:
        raise RuntimeError('Cannot verify the official JAR checksum or version. No files changed.')
    return {'version': listing.version, 'sha256': details.sha256, 'download_url': details.download,
            'release_url': release_url, 'channel': 'Stable', 'checked_at': now()}


def newer(candidate, installed):
    if not VERSION.fullmatch(candidate or '') or not VERSION.fullmatch(installed or ''):
        return False
    def parts(value):
        numbers = tuple(map(int, value.split('.')))
        return numbers + (0,) * (4 - len(numbers))
    return parts(candidate) > parts(installed)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_java(jar_path, java='/usr/bin/java'):
    """Read the main class header, never execute a downloaded JAR to inspect it."""
    with zipfile.ZipFile(jar_path) as archive:
        manifest_info = archive.getinfo('META-INF/MANIFEST.MF')
        if manifest_info.file_size > 65536:
            raise RuntimeError('Invalid JAR manifest size.')
        manifest = archive.read(manifest_info).decode('utf-8')
        match = re.search(r'^Main-Class:\s*([A-Za-z0-9_.$]+)\s*$', manifest, re.M)
        if not match:
            raise RuntimeError('Downloaded JAR has no valid main class.')
        with archive.open(match[1].replace('.', '/') + '.class') as main_class:
            header = main_class.read(8)
        if len(header) != 8 or header[:4] != b'\xca\xfe\xba\xbe':
            raise RuntimeError('Invalid JAR main class.')
        required = struct.unpack('>H', header[6:8])[0] - 44
    result = subprocess.run([java, '-version'], capture_output=True, text=True, timeout=8)
    match = re.search(r'version "(\d+)', result.stderr + result.stdout)
    if result.returncode or not match or int(match[1]) < required:
        raise RuntimeError(f'This release requires Java {required} or newer. The current launcher was not changed.')
    return required


def desktop_arg(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


class DesktopUpdates:
    def __init__(self, store, notify=None):
        self.store = store
        self.notify = notify
        self.home = store.home
        self.burp_directory = self.home / '.local/opt/burpsuite'
        self.launcher = self.home / '.local/share/applications/burpsuite.desktop'
        self.aliases = self.home / '.bash_aliases'
        self.lock = threading.RLock()
        self.jobs = {}
        self.stop_event = threading.Event()
        self.threads = []
        self.terminal_process = None
        self._revision_seed = uuid.uuid4().hex
        self._revision_counts = {'pc': 0, 'burp': 0}
        self._terminal_availability = None
        self._terminal_result_key = None
        self._terminal_result_data = None
        # Retire the old scheduler setting, including values saved by earlier versions.
        self.set_setting('burp_weekly', False)

    def get_setting(self, key, default=None, initialize=False):
        with self.store.lock:
            row = self.store.db.execute('SELECT value FROM metadata WHERE key=?', ('desktop_' + key,)).fetchone()
        if row:
            return json.loads(row['value'])
        if initialize:
            self.set_setting(key, default)
        return default

    def set_setting(self, key, value):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
        with self.lock, self.store.lock:
            name = 'desktop_' + key
            row = self.store.db.execute('SELECT value FROM metadata WHERE key=?', (name,)).fetchone()
            if row:
                previous = row['value']
                if previous != encoded:
                    try:
                        previous = json.dumps(json.loads(previous), sort_keys=True, separators=(',', ':'))
                    except (TypeError, ValueError):
                        pass  # Explicit writes can still repair malformed legacy metadata.
                if previous == encoded:
                    return False
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', (name, encoded))
            if key in {'pc_inventory', 'pc_operations', 'pc_error'}:
                self._revision_counts['pc'] += 1
            elif key in {'burp_release', 'burp_attempt', 'burp_weekly', 'burp_error', 'burp_last_install'}:
                self._revision_counts['burp'] += 1
            return True

    def _revisions(self):
        return {kind: self._revision_seed + ':' + str(count) for kind, count in self._revision_counts.items()}

    def _terminal_available(self):
        """A short UI-only cache; launch methods still resolve binaries freshly."""
        current = time.monotonic()
        if self._terminal_availability is None or current >= self._terminal_availability[1]:
            available = bool(shutil.which('konsole') and shutil.which('systemd-run'))
            self._terminal_availability = (available, current + 5)
        return self._terminal_availability[0]

    @staticmethod
    def _jar_launch(args):
        try:
            path = Path(args[args.index('-jar') + 1])
            match = re.fullmatch(r'burpsuite_(?:desktop|pro|community)_v(' + VERSION.pattern + r')\.jar', path.name)
            return {'args': args, 'jar': str(path), 'version': match[1] if match else '',
                    'installed': path.is_file(),
                    'custom_launcher': any(arg.startswith('-javaagent:') or arg == '-noverify' for arg in args)}
        except (ValueError, IndexError, OSError):
            return None

    def _desktop_launch(self):
        if not self.launcher.is_file() or self.launcher.stat().st_size > 65536:
            return None
        try:
            line = next(line[5:] for line in self.launcher.read_text().splitlines() if line.startswith('Exec='))
            return self._jar_launch(shlex.split(line))
        except (StopIteration, ValueError, OSError):
            return None

    def _alias_launch(self):
        if not self.aliases.is_file() or self.aliases.stat().st_size > 65536:
            return None
        try:
            for line in self.aliases.read_text().splitlines():
                match = re.match(r'^\s*alias\s+burp=(.*)$', line)
                if match:
                    value = shlex.split(match[1])
                    return self._jar_launch(shlex.split(value[0])) if len(value) == 1 else None
        except (ValueError, OSError):
            pass
        return None

    def detect_burp(self):
        out = {'launcher': str(self.launcher), 'alias_file': str(self.aliases), 'jar': '', 'version': '',
               'custom_launcher': False, 'installed': False}
        desktop, alias = self._desktop_launch(), self._alias_launch()
        # The shell alias is the recovery source when an older panel version
        # replaced a custom desktop Exec line with a plain Java invocation.
        selected = alias if alias and alias['custom_launcher'] and not (desktop or {}).get('custom_launcher') else desktop or alias
        if selected:
            out.update({key: selected[key] for key in ('jar', 'version', 'installed', 'custom_launcher')})
            out['launch_source'] = 'alias' if selected is alias else 'desktop'
        elif self.launcher.exists():
            out['custom_launcher'] = True
        return out

    def running_burp(self):
        pids = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                if path.stat().st_uid != os.getuid():
                    continue
                args = (path / 'cmdline').read_bytes().split(b'\0')
                if args and Path(os.fsdecode(args[0])).name == 'java' and b'-jar' in args:
                    jar = os.fsdecode(args[args.index(b'-jar') + 1])
                    if jar.startswith(str(self.burp_directory) + '/'):
                        pids.append(int(path.name))
            except (OSError, IndexError):
                continue
        return pids

    def _burp_snapshot(self):
        burp = self.detect_burp()
        latest = self.get_setting('burp_release', {})
        burp.update(latest=latest, update_available=newer(latest.get('version'), burp['version']),
                    installed_newer=newer(burp['version'], latest.get('version')),
                    weekly=False, next_check=None,
                    error=self.get_setting('burp_error', ''), last_install=self.get_setting('burp_last_install'),
                    running=self.running_burp())
        return burp

    def snapshot(self, view=None):
        """Read one visible inventory, or both for legacy clients."""
        if view not in {None, 'pc', 'burp'}:
            raise ValueError('Unsupported desktop view.')
        with self.lock:
            self.read_terminal_result()
            result = {'jobs': copy.deepcopy(self.jobs), 'terminal_available': self._terminal_available()}
            if view in {None, 'pc'}:
                # get_setting decoded a private object, so another full JSON copy is unnecessary.
                result.update(pc=self.with_package_history(self.get_setting('pc_inventory', {}), copy_data=False),
                              pc_error=self.get_setting('pc_error', ''))
            if view in {None, 'burp'}:
                result['burp'] = self._burp_snapshot()
            result['revisions'] = self._revisions()
            return result

    def status(self):
        """Small job-poll response: no inventory reads or Burp process discovery."""
        with self.lock:
            self.read_terminal_result()
            return {'jobs': copy.deepcopy(self.jobs), 'revisions': self._revisions(),
                    'pc_error': self.get_setting('pc_error', '')}

    def progress(self, kind, message, percent=None):
        with self.lock:
            self.jobs[kind].update(message=message, percent=percent)

    def submit(self, kind, operation, function):
        with self.lock:
            if self.jobs.get(kind, {}).get('state') == 'running':
                raise RuntimeError('An update task is already running.')
            self.jobs[kind] = {'operation': operation, 'state': 'running', 'message': 'Starting…', 'started_at': now(), 'percent': None}
        def run():
            try:
                message = function()
                with self.lock:
                    self.jobs[kind].update(state='done', message=message, finished_at=now(), percent=100)
            except Exception as error:
                with self.lock:
                    self.jobs[kind].update(state='error', message=str(error)[:700], finished_at=now())
        thread = threading.Thread(target=run, name='desktop-' + operation, daemon=True)
        self.threads = [t for t in self.threads if t.is_alive()]
        self.threads.append(thread)
        thread.start()

    def check_pc(self):
        def run():
            result = subprocess.run(['/usr/bin/python3', str(HERE / 'pc_packages.py'), 'list'], capture_output=True, text=True, timeout=40,
                                    env=dict(os.environ, LC_ALL='C'))
            data = json.loads(result.stdout)
            if result.returncode:
                message = data.get('error', 'Could not read the local APT cache.')
                self.set_setting('pc_error', message)
                raise RuntimeError(message)
            previous = {manager['id']: manager for manager in self.get_setting('pc_inventory', {}).get('managers', [])}
            for manager in data.get('managers', []):
                old = previous.get(manager['id'], {})
                if manager['id'] in {'snap', 'flatpak'} and old.get('updates_checked_at') and manager.get('state') == 'ok':
                    for key in ('count', 'packages', 'updates_checked_at'):
                        manager[key] = old.get(key)
                elif manager['id'] in TOOL_MANAGERS and old.get('updates_checked_at') and manager.get('state') == 'ok':
                    identity = ('id', 'installed', 'path', 'module', 'source', 'updatable', 'version_unknown', 'reason')
                    # A pin, custom-build marker or ownership change invalidates
                    # a previously offered update even when the version is equal.
                    if manager['id'] == 'go':
                        identity = ('id', 'installed', 'path', 'source')
                    before = [tuple(p.get(key) for key in identity) for p in old.get('packages', [])]
                    after = [tuple(p.get(key) for key in identity) for p in manager.get('packages', [])]
                    if before == after:
                        for key in ('count', 'packages', 'updates_checked_at', 'candidate'):
                            if key in old:
                                manager[key] = old[key]
            self.set_setting('pc_inventory', self.with_package_history(data))
            self.set_setting('pc_error', '')
            count = data.get('count')
            return (f'{count} APT updates in the local cache. ' if count is not None else '') + 'Installed package managers checked locally.'
        self.submit('pc', 'check', run)

    def with_package_history(self, data, *, copy_data=True):
        """Merge successful terminal steps without reading package tools on GET."""
        if copy_data:
            data = copy.deepcopy(data)
        history = self.get_setting('pc_operations', {})
        for manager in data.get('managers', []):
            recorded = history.get(manager['id'], {})
            for step, field in [('refresh-lists', 'last_refresh'), ('upgrade', 'last_upgrade')]:
                at = recorded.get(step)
                if at and at > (manager.get(field + '_at') or ''):
                    manager[field + '_at'] = at
                    manager[field + '_source'] = 'Successful command from Local Desk'
            if manager['id'] in {'snap', 'flatpak'} and (manager.get('last_upgrade_at') or '') > (manager.get('updates_checked_at') or ''):
                manager.update(count=None, packages=[], updates_checked_at=None)
            if manager['id'] in TOOL_MANAGERS:
                items = recorded.get('items', {})
                for item in manager.get('packages', []):
                    if item.get('id') in items:
                        item['last_updated_at'] = items[item['id']]['at']
            if manager['id'] == 'apt':
                for key in ('last_refresh_at', 'last_refresh_source', 'last_upgrade_at', 'last_upgrade_source'):
                    data[key] = manager.get(key)
        return data

    def check_manager(self, manager):
        manager_binary(manager)
        def run():
            result = subprocess.run(['/usr/bin/python3', str(HERE / 'pc_packages.py'), 'check', '--manager', manager],
                                    capture_output=True, text=True, timeout=90, env=dict(os.environ, LC_ALL='C'))
            item = json.loads(result.stdout)
            if result.returncode or item.get('state') == 'error':
                message = item.get('error', 'Could not check package updates.')
                self.set_setting('pc_error', message)
                raise RuntimeError(message)
            data = self.get_setting('pc_inventory', {})
            inventory_checked_at = data.get('checked_at')
            managers = {entry['id']: entry for entry in data.get('managers', [])}
            managers[manager] = item
            if manager == 'apt':
                data.update(item, manager='APT')
            data['managers'] = sorted(managers.values(), key=lambda entry: MANAGER_ORDER.index(entry['id']))
            # A single-manager check must not postpone the next full local inventory.
            data['checked_at'] = inventory_checked_at
            self.set_setting('pc_inventory', self.with_package_history(data))
            self.set_setting('pc_error', '')
            return f'{item.get("count", 0)} {item["name"]} updates available.'
        self.submit('pc', 'check-' + manager, run)
        with self.lock:
            self.jobs['pc']['manager'] = manager

    def launch_apt(self, operation):
        return self.launch_manager('apt', operation)

    def check_tools(self, manager):
        if manager not in TOOL_MANAGERS:
            raise RuntimeError('Unsupported tool manager.')
        def run():
            result = subprocess.run(['/usr/bin/python3', str(HERE / 'local_tool_job.py'), 'check', '--manager', manager],
                                    capture_output=True, text=True, timeout=180, env=dict(os.environ, LC_ALL='C', GOTOOLCHAIN='local'))
            item = json.loads(result.stdout)
            if result.returncode or item.get('state') == 'error':
                message = item.get('error') or 'Could not check tool versions.'
                self.set_setting('pc_error', message)
                raise RuntimeError(message)
            data = self.get_setting('pc_inventory', {})
            managers = {entry['id']: entry for entry in data.get('managers', [])}
            managers[manager] = item
            data['managers'] = sorted(managers.values(), key=lambda entry: MANAGER_ORDER.index(entry['id']))
            self.set_setting('pc_inventory', self.with_package_history(data))
            self.set_setting('pc_error', '')
            return item['name'] + ': version check finished. Review individual tools.'
        self.submit('pc', 'check-' + manager, run)
        with self.lock:
            self.jobs['pc']['manager'] = manager

    def launch_tool(self, manager, item_id, version, allow_unknown=False):
        if manager not in TOOL_MANAGERS or type(allow_unknown) is not bool or not isinstance(item_id, str) or not isinstance(version, str):
            raise RuntimeError('Invalid tool update request.')
        with self.lock:
            self.read_terminal_result()
            if self.jobs.get('pc', {}).get('state') == 'running':
                raise RuntimeError('A PC update task is already running.')
            record = next((m for m in self.get_setting('pc_inventory', {}).get('managers', []) if m['id'] == manager), {})
            item = next((p for p in record.get('packages', []) if p.get('id') == item_id), None)
            if not item or not item.get('updatable') or not item.get('candidate') or item['candidate'] != version or not record.get('updates_checked_at'):
                raise RuntimeError('Check versions first and choose an available tool update.')
            if bool(item.get('version_unknown')) != allow_unknown:
                raise RuntimeError('Review the warning about the unknown installed version.')
            terminal, runner = shutil.which('konsole'), shutil.which('systemd-run')
            if not terminal or not runner:
                raise RuntimeError('Konsole and systemd-run are required for interactive updates.')
            directory = self.store.directory / 'pc-jobs'
            directory.mkdir(mode=0o700, exist_ok=True)
            result_path = directory / (uuid.uuid4().hex + '.json')
            job = {'state': 'running', 'operation': 'upgrade', 'manager': manager, 'item': item_id,
                   'version': version, 'message': 'Opening update terminal…', 'started_at': now(), 'result_path': str(result_path)}
            self.jobs['pc'] = job
            self.set_setting('pc_terminal_job', job)
            command = [runner, '--user', '--collect', '--quiet', '--property=Type=exec',
                       '--unit=desktop-tool-update-' + result_path.stem, terminal, '--separate', '--hold', '-e',
                       '/usr/bin/python3', str(HERE / 'local_tool_job.py'), 'install', '--manager', manager,
                       '--item', item_id, '--version', version, '--result', str(result_path)]
            if allow_unknown:
                command.append('--allow-unknown-version')
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                failed = bool(result.returncode)
            except (OSError, subprocess.SubprocessError):
                failed = True
            if failed:
                job.update(state='error', message='Could not open the update terminal.')
                self.set_setting('pc_terminal_job', job)
                raise RuntimeError(job['message'])

    def launch_manager(self, manager, operation):
        manager_binary(manager)
        if operation not in ({'refresh-lists', 'upgrade'} if manager == 'apt' else {'upgrade'}):
            raise RuntimeError('Unsupported package action.')
        with self.lock:
            self.read_terminal_result()
            if self.jobs.get('pc', {}).get('state') == 'running':
                raise RuntimeError('A PC update task is already running.')
            terminal, runner = shutil.which('konsole'), shutil.which('systemd-run')
            if not terminal or not runner:
                raise RuntimeError('Konsole and systemd-run are required for interactive updates.')
            directory = self.store.directory / 'pc-jobs'
            directory.mkdir(mode=0o700, exist_ok=True)
            result_path = directory / (uuid.uuid4().hex + '.json')
            job = {'state': 'running', 'operation': operation, 'manager': manager, 'message': 'Opening terminal…', 'started_at': now(), 'result_path': str(result_path)}
            self.jobs['pc'] = job
            self.set_setting('pc_terminal_job', job)
            # A separate user-manager unit keeps sudo in the user's terminal,
            # outside the web service's NoNewPrivileges sandbox.
            command = [runner, '--user', '--collect', '--quiet', '--property=Type=exec',
                       '--unit=desktop-package-update-' + result_path.stem,
                       terminal, '--separate', '--hold', '-e', '/usr/bin/python3', str(HERE / 'pc_packages.py'), operation,
                       '--manager', manager, '--result', str(result_path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            if result.returncode:
                job.update(state='error', message='Could not open the update terminal. Check your desktop session.')
                self.set_setting('pc_terminal_job', job)
                raise RuntimeError(job['message'])

    def read_terminal_result(self):
        with self.lock:
            job = self.get_setting('pc_terminal_job', {})
            if job.get('state') != 'running':
                return
            path = Path(job['result_path'])
            try:
                information = path.lstat()
            except OSError:
                information = None
            if information and stat.S_ISREG(information.st_mode) and information.st_size < 10000:
                try:
                    key = (str(path), information.st_dev, information.st_ino, information.st_size,
                           information.st_mtime_ns, information.st_ctime_ns)
                    if key != self._terminal_result_key:
                        self._terminal_result_data = json.loads(path.read_text())
                        self._terminal_result_key = key
                    data = copy.deepcopy(self._terminal_result_data)
                    job.update(data)
                    if data.get('completed_steps'):
                        history = self.get_setting('pc_operations', {})
                        for step in data['completed_steps']:
                            manager, operation, finished = step.get('manager'), step.get('operation'), step.get('finished_at')
                            if manager in MANAGER_ORDER and operation in {'refresh-lists', 'upgrade'} and isinstance(finished, str):
                                dt.datetime.fromisoformat(finished)
                                history.setdefault(manager, {})[operation] = max(history.get(manager, {}).get(operation, ''), finished)
                                if manager in TOOL_MANAGERS and isinstance(step.get('item'), str):
                                    history[manager].setdefault('items', {})[step['item']] = {'at': finished, 'version': step.get('version')}
                        self.set_setting('pc_operations', history)
                    if job['state'] == 'running' and not Path('/proc/' + str(job.get('pid', ''))).exists():
                        job.update(state='error', message='The update terminal closed before completion was confirmed.')
                except (OSError, ValueError):
                    return
            elif (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(job['started_at'])).total_seconds() > 30:
                job.update(state='error', message='The update terminal did not start. Retry from your desktop session.')
            self.jobs['pc'] = job
            self.set_setting('pc_terminal_job', job)
            if job['state'] == 'done':
                self.check_pc()

    def check_burp(self):
        def run():
            self.set_setting('burp_attempt', now())
            try:
                data = stable_release()
            except Exception:
                self.set_setting('burp_error', 'Could not verify the latest stable release. The installed JAR is unchanged.')
                raise RuntimeError('Could not verify the latest stable release. Retry when online.') from None
            self.set_setting('burp_release', data)
            self.set_setting('burp_error', '')
            installed = self.detect_burp()
            if newer(data['version'], installed['version']) and self.notify and self.get_setting('burp_notified') != data['version']:
                try:
                    self.notify('Burp update available', 'Stable release ' + data['version'] + '. Open the Burp tab in Local Desk to review it.')
                    self.set_setting('burp_notified', data['version'])
                except Exception:
                    pass
            return 'Stable release: ' + data['version']
        self.submit('burp', 'check', run)

    def install_burp(self, version, preserve_launch=False):
        if preserve_launch is not True:
            raise RuntimeError('Confirm the Burp JAR update first.')
        latest = self.get_setting('burp_release', {})
        if version != latest.get('version') or not VERSION.fullmatch(version or ''):
            raise RuntimeError('Check for updates and review the current release first.')
        if newer(self.detect_burp()['version'], version):
            raise RuntimeError('The installed version is newer than stable. Automatic downgrades are disabled.')
        if self.running_burp():
            raise RuntimeError('Close Burp and save your projects before installing an update.')
        self.submit('burp', 'install', lambda: self.perform_install(dict(latest)))

    @staticmethod
    def _replace_jar_arg(args, target):
        updated = list(args)
        updated[updated.index('-jar') + 1] = str(target)
        return updated

    def _updated_desktop(self, original, args):
        exec_line = 'Exec=' + ' '.join(desktop_arg(arg) for arg in args)
        if original is None:
            icon = self.burp_directory / 'burpsuite.png'
            contents = '[Desktop Entry]\nName=Burp Suite\nType=Application\nTerminal=false\nCategories=Development;Security;\nStartupWMClass=burp-StartBurp\n'
            contents += exec_line + '\nIcon=' + (str(icon) if icon.is_file() else 'applications-development') + '\n'
            return contents.encode()
        text = original.decode('utf-8')
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith('Exec='):
                ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
                lines[index] = exec_line + ending
                return ''.join(lines).encode()
        raise RuntimeError('The current Burp shortcut has no Exec line. No launcher changed.')

    @staticmethod
    def _updated_aliases(original, args):
        if original is None:
            return None
        text = original.decode('utf-8')
        replacement = 'alias burp=' + shlex.quote(shlex.join(args))
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if re.match(r'^\s*alias\s+burp=', line):
                ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
                lines[index] = replacement + ending
                return ''.join(lines).encode()
        return original

    @staticmethod
    def _atomic_bytes(path, contents, mode):
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def perform_install(self, release):
        if self.burp_directory.is_symlink() or self.launcher.is_symlink():
            raise RuntimeError('Custom symlink installation: use a manual update. No launcher changed.')
        self.burp_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.launcher.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        original = self.launcher.read_bytes() if self.launcher.exists() else None
        aliases_original = self.aliases.read_bytes() if self.aliases.exists() else None
        desktop_launch, alias_launch = self._desktop_launch(), self._alias_launch()
        selected = alias_launch if alias_launch and alias_launch['custom_launcher'] and not (desktop_launch or {}).get('custom_launcher') else desktop_launch or alias_launch
        if selected:
            jar_parent = Path(selected['jar']).parent
            try:
                if not Path(selected['jar']).is_absolute() or not jar_parent.resolve().is_relative_to(self.burp_directory.resolve()):
                    raise RuntimeError('The current Burp JAR is outside the Burp directory. Use a manual update.')
            except OSError:
                raise RuntimeError('Cannot resolve the current Burp JAR directory. No launcher changed.') from None
            launch_args = selected['args']
        else:
            jar_parent = self.burp_directory
            launch_args = ['/usr/bin/java', '-jar', str(self.burp_directory / 'burpsuite_desktop_v0.0.jar')]
        if jar_parent.is_symlink():
            raise RuntimeError('Unexpected JAR directory symlink. No launcher changed.')
        jar_parent.mkdir(mode=0o700, exist_ok=True)
        target = jar_parent / ('burpsuite_desktop_v' + release['version'] + '.jar')
        if target.is_symlink():
            raise RuntimeError('Unexpected JAR symlink. No launcher changed.')
        if not target.exists() or file_hash(target) != release['sha256']:
            fd, temporary = tempfile.mkstemp(prefix='download-', suffix='.part', dir=jar_parent)
            started = time.monotonic()
            try:
                digest, size = hashlib.sha256(), 0
                with os.fdopen(fd, 'wb') as handle, open_official(release['download_url']) as response:
                    total = int(response.headers.get('Content-Length', 0))
                    if total > 2_000_000_000:
                        raise RuntimeError('Download exceeds the size limit.')
                    while chunk := response.read(1024 * 1024):
                        if self.stop_event.is_set():
                            raise RuntimeError('Download interrupted; the current launcher is unchanged.')
                        size += len(chunk)
                        if size > 2_000_000_000 or time.monotonic() - started > 1800:
                            raise RuntimeError('Download limit reached. The current launcher is unchanged.')
                        digest.update(chunk)
                        handle.write(chunk)
                        self.progress('burp', f'Downloading JAR · {size // 1048576} MiB', min(95, round(size * 95 / total)) if total else None)
                    handle.flush()
                    os.fsync(handle.fileno())
                if digest.hexdigest() != release['sha256'] or (total and total != size):
                    raise RuntimeError('SHA-256 or size verification failed. The current launcher is unchanged.')
                self.progress('burp', 'Verifying JAR and Java compatibility', 96)
                verify_java(temporary)
                if self.running_burp():
                    raise RuntimeError('Burp was opened during download. Close it and retry; the installation is unchanged.')
                if target.exists():
                    backup = self.burp_directory / ('previous-' + uuid.uuid4().hex + '-' + target.name)
                    shutil.copy2(target, backup)
                    backup.chmod(0o600)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            verify_java(target)
        if self.running_burp():
            raise RuntimeError('Burp is running. JAR downloaded, but the launcher was not changed. Close Burp and retry.')
        if ((self.launcher.read_bytes() if self.launcher.exists() else None) != original or
                (self.aliases.read_bytes() if self.aliases.exists() else None) != aliases_original):
            raise RuntimeError('The launcher or Burp alias changed during download. JAR saved; launch configuration left untouched.')
        updated_args = self._replace_jar_arg(launch_args, target)
        launcher_contents = self._updated_desktop(original, updated_args)
        aliases_contents = self._updated_aliases(aliases_original, updated_args)
        if original is not None:
            backups = self.store.directory / 'burp-backups'
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = backups / ('launcher-' + uuid.uuid4().hex + '.txt')
            backup.write_bytes(original)
            backup.chmod(0o600)
        if aliases_original is not None and aliases_contents != aliases_original:
            backups = self.store.directory / 'burp-backups'
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = backups / ('aliases-' + uuid.uuid4().hex + '.txt')
            backup.write_bytes(aliases_original)
            backup.chmod(0o600)
        try:
            if aliases_contents is not None and aliases_contents != aliases_original:
                self._atomic_bytes(self.aliases, aliases_contents, 0o644)
            self._atomic_bytes(self.launcher, launcher_contents, 0o644)
        except Exception:
            if aliases_original is not None:
                self._atomic_bytes(self.aliases, aliases_original, 0o644)
            if original is not None:
                self._atomic_bytes(self.launcher, original, 0o644)
            raise
        self.set_setting('burp_last_install', {'version': release['version'], 'at': now(), 'sha256': release['sha256']})
        return 'Official JAR verified. The existing Burp launch command and alias were preserved and updated to the new JAR.'

    def check_due(self, current=None):
        """Refresh local package inventory only; Burp checks require a user action."""
        current = current or dt.datetime.now(dt.timezone.utc)
        try:
            pc = self.get_setting('pc_inventory', {})
            if pc.get('inventory_version') != 2 or 'managers' not in pc or not pc.get('checked_at') or current - dt.datetime.fromisoformat(pc['checked_at']) >= dt.timedelta(days=1):
                self.check_pc()
        except Exception:
            pass

    def start(self):
        def schedule():
            while not self.stop_event.is_set():
                self.check_due()
                self.stop_event.wait(3600)
        thread = threading.Thread(target=schedule, name='desktop-update-checks', daemon=True)
        self.threads.append(thread)
        thread.start()

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)
