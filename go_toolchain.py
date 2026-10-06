"""Official Go toolchain maintenance, limited to /usr/local/go on Linux amd64."""
import argparse
import codecs
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import urllib.parse
import urllib.request
import uuid


INSTALL_ROOT = Path('/usr/local/go')
METADATA_URL = 'https://go.dev/dl/?mode=json'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
MAX_METADATA = 4 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_MEMBERS = 80000
DOWNLOAD_SECONDS = 180
_VERSION = re.compile(r'go([1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})(?:\.(0|[1-9][0-9]{0,2}))?')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def confirm_yes(prompt=''):
    """Read consent without broken multibyte erase or lossy input decoding.

    Kept in this standalone module so the isolated privileged Go entrypoint
    does not need to import sibling modules from a user-writable directory.
    """
    descriptor = original = None
    try:
        if sys.stdin.isatty() and codecs.lookup(sys.stdin.encoding or 'utf-8').name == 'utf-8':
            descriptor = sys.stdin.fileno()
            attributes = termios.tcgetattr(descriptor)
            if not attributes[0] & termios.IUTF8:
                updated = attributes.copy()
                updated[0] |= termios.IUTF8
                termios.tcsetattr(descriptor, termios.TCSANOW, updated)
                original = attributes
    except (OSError, ValueError, LookupError, termios.error):
        # If terminal configuration is unavailable, malformed input still
        # cannot authorize an operation below.
        original = None
    try:
        try:
            return input(prompt).strip().lower() == 'yes'
        except UnicodeDecodeError:
            print('The confirmation contained invalid text. Nothing was confirmed. Retry and type yes using the English keyboard layout.', file=sys.stderr)
            return False
        except EOFError:
            print('No confirmation was received. Nothing was confirmed.', file=sys.stderr)
            return False
    finally:
        if original is not None:
            try:
                termios.tcsetattr(descriptor, termios.TCSANOW, original)
            except (OSError, termios.error):
                pass


def version_tuple(value):
    match = _VERSION.fullmatch(value if isinstance(value, str) else '')
    if not match or (match[3] is None and (int(match[1]), int(match[2])) >= (1, 21)):
        raise RuntimeError('A stable, official Go release version is required.')
    return int(match[1]), int(match[2]), int(match[3] or 0)


def _supported_platform():
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'amd64'}:
        raise RuntimeError('Automatic Go toolchain updates support Linux amd64 only.')


def _trusted_node(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError('Custom or symlinked Go installations are not supported.')
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('The standard Go installation must be root-owned and not writable by other users.')
    return info


def _standard_version():
    _supported_platform()
    for parent in reversed(INSTALL_ROOT.parents):
        _trusted_node(parent, directory=True)
    for directory in (INSTALL_ROOT, INSTALL_ROOT / 'bin', INSTALL_ROOT / 'src', INSTALL_ROOT / 'pkg'):
        _trusted_node(directory, directory=True)
    for path in (INSTALL_ROOT / 'VERSION', INSTALL_ROOT / 'bin/go', INSTALL_ROOT / 'bin/gofmt'):
        _trusted_node(path)
    with (INSTALL_ROOT / 'VERSION').open('rb') as handle:
        first = handle.read(1024).splitlines()[0].decode('ascii')
    version_tuple(first)
    return first


def local_inventory(home=None):
    """Read the fixed system installation. Never select or download another toolchain."""
    record = {'id': 'go', 'name': 'Go toolchain', 'state': 'ok', 'checked_at': now(),
              'updates_checked_at': None, 'installed_count': 0, 'count': None,
              'packages': [], 'actions': ['check'], 'path': str(INSTALL_ROOT),
              'installed_version': None, 'managed': False, 'error': ''}
    try:
        version = _standard_version()
        result = subprocess.run([str(INSTALL_ROOT / 'bin/go'), 'version'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4,
                                cwd='/', env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GOTOOLCHAIN': 'local',
                                              'GOENV': 'off', 'GOWORK': 'off', 'GOROOT': str(INSTALL_ROOT)})
        if result.returncode or result.stdout.strip() != 'go version ' + version + ' linux/amd64':
            raise RuntimeError('The standard Go binary and VERSION file do not agree.')
        record.update(installed_count=1, installed_version=version, managed=True)
        record['packages'] = [{'id': 'go', 'name': 'Go', 'installed': version, 'candidate': None,
                               'path': str(INSTALL_ROOT), 'source': 'go.dev', 'updatable': True,
                               'reason': 'Check the official release before updating.'}]
    except (OSError, RuntimeError, ValueError, IndexError, subprocess.SubprocessError) as error:
        record.update(state='error', error=str(error)[:400])
    return record


def _allowed_url(url, filename=None):
    parsed = urllib.parse.urlsplit(url)
    if filename is None and url == METADATA_URL:
        return url
    if filename and parsed.scheme == 'https' and parsed.netloc in {'go.dev', 'dl.google.com'} and not parsed.query and not parsed.fragment:
        expected = '/dl/' + filename if parsed.netloc == 'go.dev' else '/go/' + filename
        if parsed.path == expected:
            return url
    raise RuntimeError('Only the exact official Go release endpoints are allowed.')


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def __init__(self, filename=None):
        self.filename = filename

    def redirect_request(self, request, fp, code, message, headers, newurl):
        _allowed_url(newurl, self.filename)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _open(url, filename=None):
    _allowed_url(url, filename)
    # No environment-controlled proxy is used by the privileged installer.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirect(filename))
    response = opener.open(urllib.request.Request(url, headers={'User-Agent': USER_AGENT}), timeout=15)
    _allowed_url(response.geturl(), filename)
    return response


def select_release(metadata):
    if not isinstance(metadata, list):
        raise RuntimeError('Invalid official Go release metadata.')
    candidates = []
    for release in metadata:
        if not isinstance(release, dict) or release.get('stable') is not True:
            continue
        version = release.get('version')
        try:
            version_tuple(version)
        except RuntimeError:
            continue
        filename = version + '.linux-amd64.tar.gz'
        files = release.get('files')
        if not isinstance(files, list):
            raise RuntimeError('Invalid official Go file list.')
        matches = [item for item in files if isinstance(item, dict) and item.get('os') == 'linux' and item.get('arch') == 'amd64' and item.get('kind') == 'archive']
        if len(matches) != 1:
            raise RuntimeError('The official release has no unique Linux amd64 archive.')
        item = matches[0]
        digest, size = item.get('sha256'), item.get('size')
        if item.get('filename') != filename or item.get('version') != version or not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest) or type(size) is not int or not 0 < size <= MAX_ARCHIVE:
            raise RuntimeError('Invalid official Go archive metadata.')
        candidates.append({'version': version, 'filename': filename, 'sha256': digest.lower(), 'size': size,
                           'url': 'https://go.dev/dl/' + filename, 'os': 'linux', 'arch': 'amd64', 'checked_at': now()})
    if not candidates:
        raise RuntimeError('No stable Linux amd64 Go release was found.')
    return max(candidates, key=lambda item: version_tuple(item['version']))


def check_release():
    """Explicit network check against the fixed official stable release list."""
    _supported_platform()
    started = time.monotonic()
    data = bytearray()
    with _open(METADATA_URL) as response:
        while True:
            if time.monotonic() - started > 30:
                raise RuntimeError('The official Go release check timed out.')
            chunk = response.read(min(65536, MAX_METADATA + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_METADATA:
                raise RuntimeError('The official Go release metadata is too large.')
    try:
        return select_release(json.loads(data))
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError('The official Go release metadata could not be read.') from error


def check_inventory():
    record = local_inventory()
    if not record['managed']:
        return record
    candidate = check_release()
    newer = version_tuple(candidate['version']) > version_tuple(record['installed_version'])
    record.update(candidate=candidate, count=int(newer), updates_checked_at=candidate['checked_at'])
    record['packages'][0].update(candidate=candidate['version'] if newer else None, latest=candidate['version'],
                                  updates_checked_at=candidate['checked_at'], updatable=True,
                                  reason='Verified stable release available.' if newer else 'The installed version is current or newer; downgrades are disabled.')
    return record


def _download(candidate, destination):
    digest = hashlib.sha256()
    total = 0
    started = time.monotonic()
    with _open(candidate['url'], candidate['filename']) as response, destination.open('xb') as handle:
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) != candidate['size']):
            raise RuntimeError('The Go archive size does not match official metadata.')
        while True:
            if time.monotonic() - started > DOWNLOAD_SECONDS:
                raise RuntimeError('The Go archive download timed out.')
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > candidate['size'] or total > MAX_ARCHIVE:
                raise RuntimeError('The Go archive exceeds its expected size.')
            digest.update(chunk)
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    if total != candidate['size'] or digest.hexdigest() != candidate['sha256']:
        raise RuntimeError('The Go archive failed SHA-256 or size verification.')


def _verify_archive(archive, candidate):
    digest = hashlib.sha256()
    size = 0
    with archive.open('rb') as handle:
        while chunk := handle.read(65536):
            size += len(chunk)
            if size > MAX_ARCHIVE or size > candidate['size']:
                raise RuntimeError('The Go archive is too large.')
            digest.update(chunk)
    if size != candidate['size'] or digest.hexdigest() != candidate['sha256']:
        raise RuntimeError('The Go archive failed SHA-256 verification.')


def _verify_tree(root, version):
    version_file = root / 'VERSION'
    if version_file.is_symlink() or not version_file.is_file() or version_file.stat().st_size > 1024:
        raise RuntimeError('The Go archive has no valid VERSION file.')
    if version_file.read_text(encoding='ascii').splitlines()[0] != version:
        raise RuntimeError('The Go archive VERSION does not match the selected release.')
    for relative in ('bin/go', 'bin/gofmt', 'pkg/tool/linux_amd64/compile'):
        path = root / relative
        if path.is_symlink() or not path.is_file() or not path.stat().st_mode & 0o111:
            raise RuntimeError('The Go archive is missing an executable toolchain file.')
        with path.open('rb') as handle:
            header = handle.read(64)
        if len(header) < 64 or header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 62 or int.from_bytes(header[16:18], 'little') not in {2, 3}:
            raise RuntimeError('The Go archive contains an unexpected executable format.')
    if not (root / 'src/runtime/runtime2.go').is_file():
        raise RuntimeError('The Go archive is missing the standard source layout.')


def safe_extract(archive, destination, candidate):
    """Verify first; create only ordinary files/directories in a fresh private tree."""
    _verify_archive(archive, candidate)
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise RuntimeError('Go extraction requires a new empty directory.')
    names = set()
    expanded = 0
    started = time.monotonic()
    with tarfile.open(archive, mode='r|gz') as contents:
        for member in contents:
            name = member.name.rstrip('/')
            parts = PurePosixPath(name).parts
            if time.monotonic() - started > 120 or len(names) >= MAX_MEMBERS:
                raise RuntimeError('The Go archive exceeds extraction limits.')
            if not name or name.startswith('/') or '\\' in name or '//' in name or name in names or any(part in {'', '.', '..'} for part in name.split('/')) or not parts or parts[0] != 'go':
                raise RuntimeError('The Go archive contains an unsafe or duplicate path.')
            if not (member.isdir() or member.isfile()) or member.sparse is not None:
                raise RuntimeError('Links and special archive entries are not allowed.')
            if member.size < 0 or member.size > MAX_ARCHIVE:
                raise RuntimeError('The Go archive contains an oversized file.')
            names.add(name)
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            expanded += member.size
            if expanded > MAX_EXPANDED:
                raise RuntimeError('The Go archive exceeds the expanded size limit.')
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            source = contents.extractfile(member)
            if source is None:
                raise RuntimeError('The Go archive contains an unreadable file.')
            with source, target.open('xb') as handle:
                remaining = member.size
                while remaining:
                    if time.monotonic() - started > 120:
                        raise RuntimeError('The Go archive extraction timed out.')
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        raise RuntimeError('The Go archive contains a truncated file.')
                    handle.write(chunk)
                    remaining -= len(chunk)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    root = destination / 'go'
    for directory in [root, *(path for path in root.rglob('*') if path.is_dir())]:
        directory.chmod(0o755)
    _verify_tree(root, candidate['version'])
    return root


def _running_toolchain():
    running = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            executable = os.readlink(entry / 'exe')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise RuntimeError('Could not check whether the current Go toolchain is in use.') from None
        if executable.startswith(str(INSTALL_ROOT) + '/'):
            running.append(int(entry.name))
    return running


def _exchange(first, second):
    """Linux atomic directory exchange; there is no non-atomic fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise RuntimeError('Atomic directory exchange is unavailable; Go was not replaced.')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(first), -100, os.fsencode(second), 2):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _replace_ready(ready, previous):
    if _standard_version() != previous:
        raise RuntimeError('The installed Go version changed during preparation. Retry after reviewing it.')
    if _running_toolchain():
        raise RuntimeError('The current Go toolchain is running. Finish builds and retry; no process was interrupted.')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = INSTALL_ROOT.parent / ('go.backup-' + previous + '-' + stamp + '-' + uuid.uuid4().hex[:12])
    os.rename(ready, backup)
    try:
        _exchange(backup, INSTALL_ROOT)
    except (OSError, RuntimeError) as error:
        raise RuntimeError('Go was not replaced. The prepared candidate is retained at ' + str(backup) + ': ' + str(error)) from error
    return backup


def _root_install(version):
    version_tuple(version)
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise RuntimeError('The Go installer requires sudo in an interactive terminal.')
    previous = _standard_version()
    candidate = check_release()
    if candidate['version'] != version:
        raise RuntimeError('The official stable release changed. Check updates again before installing.')
    if version_tuple(version) <= version_tuple(previous):
        raise RuntimeError('Go downgrades and same-version replacements are disabled.')
    if _running_toolchain():
        raise RuntimeError('The current Go toolchain is running. Finish builds before updating.')
    print(f'Go {previous} → {version}\nReplace only {INSTALL_ROOT}. The old tree will remain in a sibling backup.\nDownload: {candidate["url"]}\nSHA-256: {candidate["sha256"]}\nType yes to install:', file=sys.stderr, flush=True)
    if not confirm_yes():
        raise RuntimeError('Cancelled. Go was not changed.')
    lock_path = INSTALL_ROOT.parent / '.go-update.lock'
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        info = os.fstat(lock.fileno())
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise RuntimeError('The Go update lock is not trusted.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another Go toolchain update is running.') from None
        with tempfile.TemporaryDirectory(prefix='.go-stage-', dir=INSTALL_ROOT.parent) as temporary:
            stage = Path(temporary)
            archive = stage / 'release.tar.gz'
            _download(candidate, archive)
            extracted = stage / 'extracted'
            extracted.mkdir(mode=0o700)
            ready = safe_extract(archive, extracted, candidate)
            backup = _replace_ready(ready, previous)
    return {'state': 'done', 'version': version, 'backup': str(backup), 'path': str(INSTALL_ROOT)}


def interactive_update(version=None):
    """Called from the user terminal. Sudo credentials remain in that terminal."""
    version = version or check_release()['version']
    version_tuple(version)
    script = Path(__file__).resolve()
    command = ['/usr/bin/sudo', '--', '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'LC_ALL=C',
               '/usr/bin/python3', '-I', str(script), 'install', '--version', version]
    completed = subprocess.run(command, stdout=subprocess.PIPE, text=True)
    if completed.returncode:
        raise RuntimeError('Go update cancelled or failed. Review the terminal output; no success was recorded.')
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise RuntimeError('The Go installer returned no valid completion result.') from None
    if not isinstance(result, dict) or result.get('state') != 'done' or result.get('version') != version:
        raise RuntimeError('The Go installer did not confirm the selected version.')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['list', 'check', 'install'])
    parser.add_argument('--version')
    args = parser.parse_args()
    try:
        if args.operation == 'install':
            if not args.version:
                raise RuntimeError('Select a specific verified release version.')
            result = _root_install(args.version)
        else:
            if args.version:
                raise RuntimeError('A version is accepted only for installation.')
            result = local_inventory() if args.operation == 'list' else check_inventory()
        print(json.dumps(result))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if args.operation == 'install':
            print(str(error) or 'Go update cancelled.', file=sys.stderr)
        else:
            print(json.dumps({'state': 'error', 'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
