"""User-installed tools: local metadata, explicit checks, single-item updates.

Inventory never executes an installed security tool. Updates use fixed module or
PyPI names, preserve destinations, and never install a catalogue of extra tools.
The caller supplies the terminal confirmation and checks the cached candidate.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import urllib.parse
import urllib.request

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
SYSTEM_PATH = '/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin'
MANAGERS = {'pdtm': 'ProjectDiscovery', 'go-tools': 'Go tools', 'pipx': 'pipx'}
MODULES = {
    'nuclei': ('github.com/projectdiscovery/nuclei/v3', 'github.com/projectdiscovery/nuclei/v3/cmd/nuclei'),
    'httpx': ('github.com/projectdiscovery/httpx', 'github.com/projectdiscovery/httpx/cmd/httpx'),
    'pdtm': ('github.com/projectdiscovery/pdtm', 'github.com/projectdiscovery/pdtm/cmd/pdtm'),
    'katana': ('github.com/projectdiscovery/katana', 'github.com/projectdiscovery/katana/cmd/katana'),
    'ffuf': ('github.com/ffuf/ffuf/v2', 'github.com/ffuf/ffuf/v2'),
    'rotproxy': ('github.com/ffuf/ffuf/v2', 'github.com/ffuf/ffuf/v2/tools/rotproxy'),
    'gau': ('github.com/lc/gau/v2', 'github.com/lc/gau/v2/cmd/gau'),
    'subjs': ('github.com/lc/subjs', 'github.com/lc/subjs'),
}
STABLE = re.compile(r'v?\d+(?:\.\d+){1,3}\Z')
PACKAGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def newer(candidate, installed):
    if not STABLE.fullmatch(candidate or '') or not STABLE.fullmatch(installed or ''):
        return False
    def version(value):
        parts = tuple(map(int, value.lstrip('v').split('.')))
        return parts + (0,) * (4 - len(parts))
    return version(candidate) > version(installed)


def _go_binary():
    for path in ('/usr/local/go/bin/go', '/usr/bin/go'):
        if os.access(path, os.X_OK):
            return path
    raise RuntimeError('Go is not installed in a supported location.')


def _run_read(command, timeout=10):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=timeout, env=dict(os.environ, GOTOOLCHAIN='local', GOENV='off', GOWORK='off', LC_ALL='C'))
    if result.returncode:
        raise RuntimeError('Could not read installed tool metadata.')
    if len(result.stdout) > 2_000_000:
        raise RuntimeError('Tool metadata exceeds the size limit.')
    return result.stdout


def parse_go_metadata(text, module):
    result = {'module': '', 'version': '', 'replaced': False, 'custom': False, 'path': ''}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == 'path' and len(fields) > 1:
            result['path'] = fields[1]
        if fields[0] == '=>' or line.lstrip().startswith('=>'):
            result['replaced'] = True
        if fields[0] in {'mod', 'dep'} and len(fields) >= 3 and fields[1] == module:
            result.update(module=fields[1], version=fields[2])
        if fields[0] == 'mod' and len(fields) > 1 and fields[1] != module:
            result['custom'] = True
        if 'vcs.modified=true' in line:
            result['custom'] = True
        if fields[0] == 'build' and '-ldflags=' in line:
            match = re.search(r'(?:^|\s)main\.version=(v?\d+(?:\.\d+){1,3})(?:\s|["\']|$)', line)
            if match:
                result['linker_version'] = match[1]
    return result


def _metadata(path, module):
    return parse_go_metadata(_run_read([_go_binary(), 'version', '-m', str(path)]), module)


def _owned_path(path, root):
    """Do not follow user-custom symlink layouts while replacing binaries."""
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        return False
    if root.is_symlink() or root.resolve() != root.absolute():
        return False
    return path.parent == root


def _active_path(name):
    found = shutil.which(name)
    return str(Path(found).absolute()) if found else None


def _go_rows(home):
    rows = {'pdtm': [], 'go-tools': []}
    roots = [home / '.pdtm/go/bin', home / 'go/bin']
    seen = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir())[:100]:
            if not (path.is_file() or path.is_symlink()):
                continue
            manager = 'pdtm' if path.name in {'nuclei', 'httpx', 'pdtm'} or root == roots[0] else 'go-tools'
            row = {'id': path.name, 'name': path.name, 'installed': 'Unknown', 'candidate': None, 'path': str(path),
                   'source': 'PDTM directory' if root == roots[0] else 'Go module', 'updatable': False,
                   'reason': '', 'version_unknown': False, 'active_path': _active_path(path.name)}
            module, package = MODULES.get(path.name, ('', ''))
            row['module'] = module
            if not _owned_path(path, root):
                row['reason'] = 'Custom symlink layout or ownership; manage this path manually.'
            elif not module:
                row['reason'] = 'No verified update source is configured for this tool.'
            else:
                try:
                    metadata = _metadata(path, module)
                    version = metadata.get('version', '')
                    if version in {'(devel)', ''} and path.name == 'httpx':
                        version = metadata.get('linker_version', version)
                    row['installed'] = version if version and version != '(devel)' else 'Unknown'
                    if metadata['module'] != module or metadata['custom'] or metadata['replaced']:
                        row['reason'] = 'Custom build, replacement, or unverified module origin.'
                    elif metadata['path'] not in {package, 'command-line-arguments'}:
                        row['reason'] = 'The installed command does not match the supported module.'
                    elif STABLE.fullmatch(version):
                        row['updatable'] = True
                    elif path.name == 'nuclei' and root == roots[0] and version == '(devel)':
                        row.update(updatable=True, version_unknown=True,
                                   reason='Installed release cannot be verified. Installing stable may replace a newer or custom build; a backup is retained.')
                    else:
                        row['reason'] = 'Development or custom version; automatic replacement is disabled.'
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    row['reason'] = 'Cannot read Go build metadata without running this tool.'
            if row['active_path'] and Path(row['active_path']).resolve() != path.resolve():
                row.update(updatable=False, reason='Another copy is first in PATH. Resolve duplicate installations before updating.')
            rows[manager].append(row)
            seen.setdefault(path.name, []).append(row)
    for duplicates in seen.values():
        if len(duplicates) > 1:
            for row in duplicates:
                row.update(id=row['name'] + '-' + hashlib.sha256(row['path'].encode()).hexdigest()[:8], updatable=False,
                           reason='Duplicate tool in the managed directories; choose one installation manually.')
    return rows


def _pipx_root(home):
    return home / '.local/share/pipx/venvs'


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise RuntimeError('Unsupported metadata file.')
    return json.loads(path.read_text())


def _pipx_rows(home):
    root = _pipx_root(home)
    if not root.is_dir():
        return []
    rows = []
    for venv in sorted(root.iterdir())[:100]:
        if not venv.is_dir():
            continue
        row = {'id': venv.name, 'name': venv.name, 'installed': 'Unknown', 'candidate': None, 'path': str(venv),
               'source': 'pipx', 'updatable': False, 'reason': '', 'version_unknown': False}
        try:
            metadata = _read_json(venv / 'pipx_metadata.json')
            package = metadata['main_package']
            name = package['package']
            spec = package['package_or_url']
            row.update(installed=package.get('package_version') or 'Unknown', distribution=name,
                       apps=package.get('apps', []))
            canonical = lambda value: re.sub(r'[-_.]+', '-', value).lower()
            if venv.is_symlink() or root.resolve() != root.absolute() or venv.stat().st_uid != os.getuid():
                row['reason'] = 'Custom symlink layout or ownership; manage this environment manually.'
            elif not PACKAGE.fullmatch(name) or canonical(name) != canonical(venv.name):
                row['reason'] = 'Custom pipx environment name or suffix; manual update required.'
            elif package.get('pinned') or '==' in spec:
                row['reason'] = 'Version-pinned package; its pin will not be changed automatically.'
            elif not PACKAGE.fullmatch(spec) or canonical(spec) != canonical(name) or package.get('pip_args'):
                row['reason'] = 'Local, editable, custom-index, or custom-source install; preserved unchanged.'
            elif metadata.get('injected_packages') or package.get('include_dependencies') or package.get('man_pages'):
                row['reason'] = 'Custom injected dependencies or exported files; manual update required.'
            elif not STABLE.fullmatch(row['installed']):
                row['reason'] = 'Non-stable installed version; manual update required.'
            elif not shutil.which('pipx', path=SYSTEM_PATH):
                row['reason'] = 'pipx is not available.'
            else:
                row.update(updatable=True, source='PyPI')
                for app in package.get('apps', []):
                    launcher = home / '.local/bin' / app
                    active = _active_path(app)
                    if (not PACKAGE.fullmatch(app) or not launcher.is_symlink()
                            or not launcher.resolve().is_relative_to(venv.resolve())):
                        row.update(updatable=False, reason='An application launcher is missing or customized; manual update required.')
                        break
                    if active and Path(active).resolve() != launcher.resolve():
                        row.update(updatable=False, reason='Another application copy is first in PATH. Resolve duplicate installations before updating.')
                        break
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            row['reason'] = 'Cannot read this pipx environment metadata.'
        rows.append(row)
    return rows


def _official_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in {'proxy.golang.org', 'pypi.org'} or parsed.username or parsed.password:
        raise RuntimeError('Only official package metadata endpoints are supported.')
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, _official_url(newurl))


def _fetch_json(url):
    request = urllib.request.Request(_official_url(url), headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urllib.request.build_opener(_Redirect()).open(request, timeout=8) as response:
        content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise RuntimeError('Package metadata exceeds the size limit.')
    return json.loads(content)


def _latest(row, manager):
    if manager == 'pipx':
        data = _fetch_json('https://pypi.org/pypi/' + urllib.parse.quote(row['distribution'], safe='') + '/json')
        value = data.get('info', {}).get('version', '')
    else:
        module = row['module']
        escaped = ''.join('!' + character.lower() if character.isupper() else character for character in module)
        value = _fetch_json('https://proxy.golang.org/' + escaped + '/@latest').get('Version', '')
    if not STABLE.fullmatch(value):
        raise RuntimeError('No supported stable release was returned.')
    return value


def _manager(manager, rows, check):
    data = {'id': manager, 'name': MANAGERS[manager], 'state': 'ok', 'checked_at': now(), 'updates_checked_at': None,
            'installed_count': len(rows), 'installed_packages': rows, 'packages': rows, 'count': None, 'error': '',
            'actions': ['check'], 'last_refresh_at': None, 'last_upgrade_at': None}
    if check:
        for row in rows:
            if not row['updatable']:
                continue
            try:
                row['latest'] = _latest(row, manager)
                row['candidate'] = row['latest'] if row['version_unknown'] or newer(row['latest'], row['installed']) else None
                row['updates_checked_at'] = now()
            except (OSError, ValueError, RuntimeError) as error:
                row['check_error'] = str(error)[:240]
                data['state'] = 'partial'
        data.update(updates_checked_at=now(), count=sum(bool(row.get('candidate')) for row in rows))
        if data['state'] == 'partial':
            data['error'] = 'Some release checks failed; their installed tools were not changed.'
    return data


def inventory(home, check=False):
    home = Path(home).absolute()
    rows = _go_rows(home)
    rows['pipx'] = _pipx_rows(home)
    return [_manager(manager, items, check) for manager, items in rows.items() if items]


def manager_inventory(manager, home, check=False):
    if manager not in MANAGERS:
        raise RuntimeError('Unsupported tool manager.')
    home = Path(home).absolute()
    rows = _pipx_rows(home) if manager == 'pipx' else _go_rows(home)[manager]
    if not rows:
        raise RuntimeError('No supported installation was detected for this manager.')
    return _manager(manager, rows, check)


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        while data := handle.read(1024 * 1024):
            value.update(data)
    return value.hexdigest()


def _backup_directory(home):
    parent = home / '.local/share/local-desk/tool-backups'
    if parent.is_symlink() or parent.resolve() != parent.absolute():
        raise RuntimeError('Unsupported backup directory layout.')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    return parent


def _run_install(command, **kwargs):
    """Cancellation must also stop compiler/pip children before rollback."""
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        return process.wait(timeout=1200)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        raise


def _update_go(row, version, home):
    target = Path(row['path'])
    module, package = MODULES[row['name']]
    original_hash = _digest(target)
    backup_parent = _backup_directory(home)
    with tempfile.TemporaryDirectory(prefix='.package-', dir=target.parent) as temporary:
        temporary = Path(temporary)
        environment = dict(os.environ, GOBIN=str(temporary), GOTOOLCHAIN='local', GOENV='off', GOWORK='off',
                           GOPROXY='https://proxy.golang.org', GOSUMDB='sum.golang.org', GOPRIVATE='', GONOPROXY='', GONOSUMDB='',
                           GOFLAGS='', LC_ALL='C')
        command = [_go_binary(), 'install', package + '@' + version]
        result = _run_install(command, cwd=temporary, env=environment)
        if result:
            raise RuntimeError('Go build failed. The installed tool was not replaced; check Go requirements in the terminal.')
        built = temporary / row['name']
        if built.is_symlink() or not built.is_file():
            raise RuntimeError('The requested tool was not produced. Existing binary is unchanged.')
        metadata = _metadata(built, module)
        if metadata['module'] != module or metadata['version'] != version or metadata['replaced'] or metadata['custom']:
            raise RuntimeError('Built module or version did not match the selected release. Existing binary is unchanged.')
        if not _owned_path(target, target.parent) or _digest(target) != original_hash:
            raise RuntimeError('Installed binary changed during the build. It was not overwritten.')
        backup = Path(tempfile.mkdtemp(prefix=row['name'] + '-', dir=backup_parent)) / row['name']
        shutil.copy2(target, backup)
        backup.chmod(0o700)
        built.chmod(target.stat().st_mode & 0o777)
        os.replace(built, target)
    return {'state': 'done', 'manager': 'pdtm' if row['name'] in {'nuclei', 'httpx', 'pdtm'} else 'go-tools',
            'item': row['id'], 'version': version, 'path': str(target), 'backup': str(backup), 'finished_at': now(),
            'message': row['name'] + ' updated; previous binary retained in backup.'}


def _owned_links(home, venv):
    directory = home / '.local/bin'
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise RuntimeError('Custom launcher directory; manual pipx update required.')
    links = {}
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_symlink() and path.resolve().is_relative_to(venv.resolve()):
                links[path.name] = os.readlink(path)
    return links


def _update_pipx(row, version, home):
    venv = Path(row['path'])
    metadata_path = venv / 'pipx_metadata.json'
    original_hash = _digest(metadata_path)
    links = _owned_links(home, venv)
    for app in row.get('apps', []):
        if not PACKAGE.fullmatch(app) or app not in links:
            raise RuntimeError('An application launcher is missing or customized. Existing environment is unchanged.')
    backup = Path(tempfile.mkdtemp(prefix=row['id'] + '-', dir=_backup_directory(home)))
    environment_backup = backup / 'venv'
    shutil.copytree(venv, environment_backup, symlinks=True)
    (backup / 'launchers.json').write_text(json.dumps(links))
    constraint = backup / 'constraints.txt'
    constraint.write_text(row['distribution'] + '==' + version + '\n')
    if _digest(metadata_path) != original_hash:
        raise RuntimeError('The pipx environment changed before the update. It was not modified.')
    pipx = shutil.which('pipx', path=SYSTEM_PATH)
    if not pipx:
        raise RuntimeError('pipx is not installed.')
    # Clear pip overrides; this row was verified as a plain, unpinned PyPI install.
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('PIP_', 'PIPX_'))}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL='https://pypi.org/simple',
                       PIP_CONSTRAINT=str(constraint),
                       PIPX_HOME=str(home / '.local/share/pipx'), PIPX_BIN_DIR=str(home / '.local/bin'),
                       PIP_DISABLE_PIP_VERSION_CHECK='1', LC_ALL='C')
    # pipx persists CLI pip_args in metadata. Keep this one-use version constraint
    # in pip's environment so future updates do not inherit a stale pin or path.
    command = [pipx, 'upgrade', row['id']]
    try:
        result = _run_install(command, env=environment)
        if result:
            raise RuntimeError('pipx update failed.')
        updated = _read_json(metadata_path)['main_package']
        if updated.get('package_version') != version:
            raise RuntimeError('pipx did not install the selected version.')
        if updated.get('pip_args'):
            raise RuntimeError('pipx unexpectedly changed the package installation arguments.')
    except BaseException as error:
        # Keep the failed environment too; rollback does not delete user files.
        failed = backup / 'failed-venv'
        if venv.exists() or venv.is_symlink():
            os.replace(venv, failed)
        os.replace(environment_backup, venv)
        current_links = _owned_links(home, venv)
        for name in set(current_links) | set(links):
            path = home / '.local/bin' / name
            if path.is_symlink() and (name in current_links or os.readlink(path) == links.get(name)):
                path.unlink()
            if name in links and not path.exists() and not path.is_symlink():
                path.symlink_to(links[name])
        raise RuntimeError('pipx update failed; previous environment and owned launchers restored. Backup: ' + str(backup)) from error
    return {'state': 'done', 'manager': 'pipx', 'item': row['id'], 'version': version, 'path': str(venv),
            'backup': str(backup), 'finished_at': now(), 'message': row['name'] + ' updated; previous environment retained in backup.'}


def update_item(manager, item_id, version, home, allow_unknown_version=False):
    """Call only after showing the pinned version and obtaining terminal consent."""
    if manager not in MANAGERS or not STABLE.fullmatch(version or ''):
        raise RuntimeError('Unsupported manager or release version.')
    home = Path(home).absolute()
    data = manager_inventory(manager, home, check=False)
    row = next((item for item in data['packages'] if item['id'] == item_id), None)
    if not row or not row['updatable']:
        raise RuntimeError(row.get('reason') if row else 'The selected tool is no longer installed here.')
    if row['version_unknown']:
        if allow_unknown_version is not True:
            raise RuntimeError('Explicitly confirm replacing an unverified installed version with stable.')
    elif not newer(version, row['installed']):
        raise RuntimeError('The selected release is not newer. Downgrades and reinstallations are disabled.')
    if manager != 'pipx' and not version.startswith('v'):
        raise RuntimeError('A pinned Go module version is required.')
    return _update_pipx(row, version, home) if manager == 'pipx' else _update_go(row, version, home)
