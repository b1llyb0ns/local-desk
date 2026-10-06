"""Root SSH migration with a systemd rollback timer and independent client checks.

Only the public key is sent to this controller. It is also stored in each remote
transaction directory so the rollback timer survives the local application.
"""
import base64
import contextlib
import datetime
import fcntl
import glob
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT_HOME = Path(pwd.getpwnam('root').pw_dir)
BASE = ROOT_HOME / '.local/share/local-desk/transactions'
CONFIG = Path('/etc/ssh/sshd_config')
MANAGED = Path('/etc/ssh/sshd_config.d/00-local-desk.conf')
SSHD = shutil.which('sshd') or '/usr/sbin/sshd'


def run(args, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if check and p.returncode:
        raise RuntimeError((p.stderr.strip() or p.stdout.strip() or 'Command failed')[:800])
    return p.stdout.strip()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic(path, data, mode=0o600, uid=0, gid=0):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Symlink found instead of SSH file: ' + str(path))
    fd, temporary = tempfile.mkstemp(prefix='.local-desk-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_write(directory, state):
    atomic(directory / 'state.json', json.dumps(state, ensure_ascii=False).encode())


@contextlib.contextmanager
def locked(directory):
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def effective():
    peer = os.environ.get('SSH_CONNECTION', '127.0.0.1').split()[0]
    result = run([SSHD, '-T', '-C', 'user=root,host=' + os.uname().nodename + ',addr=' + peer])
    return dict(line.split(' ', 1) for line in result.splitlines() if ' ' in line)


def inspect_includes(path, visited=None):
    visited = set() if visited is None else visited
    path = Path(path)
    if path in visited:
        return
    visited.add(path)
    if path.is_symlink():
        raise RuntimeError('Nonstandard SSH configuration symlink: ' + str(path))
    for line in path.read_text().splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0].lower() == 'match':
            raise RuntimeError('SSH contains Match rules. Manual configuration is required; no changes applied.')
        if words[0].lower() == 'include':
            for pattern in words[1:]:
                if not pattern.startswith('/'):
                    pattern = '/etc/ssh/' + pattern
                for child in glob.glob(pattern):
                    inspect_includes(child, visited)


def backup(directory, paths):
    files = []
    for index, path in enumerate(paths):
        if path.is_symlink():
            raise RuntimeError('Nonstandard SSH path: ' + str(path))
        item = {'path': str(path), 'existed': path.exists()}
        if path.exists():
            if not path.is_file():
                raise RuntimeError('Expected a regular file: ' + str(path))
            stat = path.stat()
            name = 'original-' + str(index)
            shutil.copyfile(path, directory / name)
            os.chmod(directory / name, 0o600)
            item.update(backup=name, mode=stat.st_mode & 0o777, uid=stat.st_uid, gid=stat.st_gid)
        files.append(item)
    return files


def restore(directory, state):
    if state.get('phase') == 'committed':
        return
    for item in state['files']:
        path = Path(item['path'])
        if item['existed']:
            atomic(path, (directory / item['backup']).read_bytes(), item['mode'], item['uid'], item['gid'])
        elif path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Could not automatically restore ' + str(path))
            path.unlink()
    run([SSHD, '-t'])
    run(['systemctl', 'reload', state['service']])
    state.update(phase='rolled_back', updated_at=now())
    state_write(directory, state)
    run(['systemctl', 'stop', state['timer'] + '.timer'], check=False)


def write_policy(state, final=False):
    root_policy = 'prohibit-password' if final else state['initial_root_policy']
    lines = ['# Managed by local VPS Desk', 'PubkeyAuthentication yes',
             'PermitRootLogin ' + root_policy]
    # With effective "any", public-key login is already allowed. Emitting
    # another "any" breaks older OpenSSH when the managed file is read both
    # through our explicit Include and the distribution's *.conf Include.
    if not final and state['initial_authentication'] != 'any':
        lines.append('AuthenticationMethods ' + state['initial_authentication'])
    if final:
        lines.extend(['PasswordAuthentication no', 'KbdInteractiveAuthentication no',
                      'ChallengeResponseAuthentication no', 'AuthenticationMethods publickey'])
    atomic(MANAGED, ('\n'.join(lines) + '\n').encode(), 0o644)
    include = 'Include ' + str(MANAGED) + '\n'
    config = CONFIG.read_text()
    if not config.startswith(include):
        metadata = CONFIG.stat()
        atomic(CONFIG, (include + config).encode(), metadata.st_mode & 0o777, metadata.st_uid, metadata.st_gid)
    run([SSHD, '-t'])
    if final:
        settings = effective()
        expected = {'pubkeyauthentication': 'yes', 'passwordauthentication': 'no',
                    'kbdinteractiveauthentication': 'no', 'authenticationmethods': 'publickey'}
        if any(settings.get(k) != v for k, v in expected.items()) or settings.get('permitrootlogin') not in {'without-password', 'prohibit-password'}:
            raise RuntimeError('SSH did not apply the required policy. Changes rolled back.')
    run(['systemctl', 'reload', state['service']])


def prepare(directory, payload):
    public_key = payload['public_key'].strip()
    parts = public_key.split()
    if len(parts) < 2 or parts[0] not in {'ssh-ed25519', 'ssh-rsa'} or '\n' in public_key:
        raise RuntimeError('Invalid public key format')
    base64.b64decode(parts[1], validate=True)
    if not shutil.which('systemd-run') or not shutil.which('systemctl'):
        raise RuntimeError('Automatic setup requires Linux with systemd and OpenSSH.')
    inspect_includes(CONFIG)
    run([SSHD, '-t'])
    settings = effective()
    if '.ssh/authorized_keys' not in settings.get('authorizedkeysfile', '').split():
        raise RuntimeError('Nonstandard authorized_keys location: automatic setup stopped.')
    if payload.get('only_managed', True):
        if set(settings.get('authorizedkeysfile', '').split()) - {'.ssh/authorized_keys', '.ssh/authorized_keys2'} or settings.get('authorizedkeyscommand', 'none') != 'none' or settings.get('trustedusercakeys', 'none') != 'none':
            raise RuntimeError('Additional SSH key sources or certificates found. Keeping only the selected key requires manual configuration; no changes applied.')
    if settings.get('authenticationmethods') not in {'any', 'publickey', 'password'}:
        raise RuntimeError('Composite SSH authentication detected; automatic setup stopped.')
    service = next((name for name in ('ssh.service', 'sshd.service') if run(['systemctl', 'is-active', name], check=False) == 'active'), None)
    if service is None:
        raise RuntimeError('No active SSH systemd service found')
    ssh_dir = ROOT_HOME / '.ssh'
    if ssh_dir.is_symlink() or MANAGED.parent.is_symlink():
        raise RuntimeError('Nonstandard SSH directories; automatic setup stopped.')
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    MANAGED.parent.mkdir(mode=0o755, exist_ok=True)
    auth = ssh_dir / 'authorized_keys'
    auth2 = ssh_dir / 'authorized_keys2'
    state = {'phase': 'prepared', 'created_at': now(), 'service': service,
             'timer': 'local-desk-rollback-' + directory.name, 'public_key': public_key,
             'only_managed': bool(payload.get('only_managed', True)),
             'initial_root_policy': 'yes' if settings.get('permitrootlogin') == 'yes' else 'prohibit-password',
             'initial_authentication': 'publickey password' if settings.get('authenticationmethods') == 'password' else settings.get('authenticationmethods', 'any'),
             'files': backup(directory, [CONFIG, MANAGED, auth, auth2])}
    atomic(directory / 'controller.py', payload['controller_source'].encode())
    state_write(directory, state)
    run(['systemd-run', '--quiet', '--unit=' + state['timer'], '--on-active=300s',
         '/usr/bin/python3', str(directory / 'controller.py'), 'rollback', directory.name])
    try:
        existing = auth.read_text() if auth.exists() else ''
        has_key = any(len(line.split()) >= 2 and line.split()[0:2] == parts[0:2] for line in existing.splitlines())
        if not has_key:
            atomic(auth, (existing.rstrip('\n') + '\n' + public_key + '\n').encode())
        os.chmod(ssh_dir, 0o700)
        write_policy(state)
        state.update(phase='key_installed', updated_at=now())
        state_write(directory, state)
        return {'ok': True, 'transaction': directory.name, 'backup': str(directory), 'rollback_seconds': 300}
    except Exception:
        restore(directory, state)
        raise


def main():
    os.umask(0o077)
    if os.getuid() != 0:
        raise RuntimeError('Root or sudo privileges required')
    action = sys.argv[1]
    token = sys.argv[2]
    if not re.fullmatch(r'[a-f0-9]{24}', token):
        raise RuntimeError('Invalid setup identifier')
    directory = BASE / token
    if action == 'prepare':
        payload = json.load(sys.stdin)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        with locked(directory):
            return prepare(directory, payload)
    if not directory.is_dir():
        raise RuntimeError('Saved SSH setup not found')
    with locked(directory):
        state = json.loads((directory / 'state.json').read_text())
        if action == 'rollback':
            restore(directory, state)
        elif action == 'finalize':
            if state['phase'] != 'key_installed':
                raise RuntimeError('Setup already completed or rolled back')
            try:
                if state['only_managed']:
                    atomic(ROOT_HOME / '.ssh/authorized_keys', (state['public_key'] + '\n').encode())
                    second = ROOT_HOME / '.ssh/authorized_keys2'
                    if second.exists():
                        atomic(second, b'')
                write_policy(state, final=True)
                state.update(phase='finalized', updated_at=now())
                state_write(directory, state)
            except Exception:
                restore(directory, state)
                raise
        elif action == 'commit':
            if state['phase'] != 'finalized':
                raise RuntimeError('Rollback already occurred or setup is incomplete')
            run([SSHD, '-t'])
            state.update(phase='committed', updated_at=now())
            state_write(directory, state)
            run(['systemctl', 'stop', state['timer'] + '.timer'], check=False)
        else:
            raise RuntimeError('Unknown action')
        return {'ok': True, 'phase': state['phase'], 'backup': str(directory)}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
