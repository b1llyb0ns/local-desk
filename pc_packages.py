"""Cached package inventory and allowlisted, user-confirmed terminal updates."""
import argparse
import datetime
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


MANAGERS = {'apt': ('APT', 'apt-get'), 'snap': ('Snap', 'snap'), 'flatpak': ('Flatpak', 'flatpak')}
SYSTEM_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


def manager_binary(manager):
    if manager not in MANAGERS:
        raise RuntimeError('Unsupported package manager.')
    binary = shutil.which(MANAGERS[manager][1], path=SYSTEM_PATH)
    if not binary:
        raise RuntimeError(MANAGERS[manager][0] + ' is not installed.')
    return binary


def read_command(command, timeout=10):
    """Only fixed package-manager argument lists reach this function."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                            env=dict(os.environ, LC_ALL='C', LANG='C', PATH=SYSTEM_PATH), stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or 'Package manager did not respond.')[:500])
    if len(result.stdout) > 2_000_000:
        raise RuntimeError('Package inventory exceeds the size limit.')
    return result.stdout


def timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value).astimezone(datetime.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def apt_history(directory=Path('/var/log/apt')):
    """Completed APT transaction dates; never call a repository timestamp an update."""
    latest_upgrade = latest_transaction = None
    paths = sorted(directory.glob('history.log*'), key=lambda p: p.stat().st_mtime, reverse=True)[:4]
    for path in paths:
        try:
            opener = gzip.open if path.suffix == '.gz' else open
            with opener(path, 'rt', errors='replace') as handle:
                text = handle.read(2_000_001)
            if len(text) > 2_000_000:
                continue
            for block in text.split('\n\n'):
                end = re.search(r'^End-Date:\s*(.+)$', block, re.M)
                if not end:
                    continue
                finished = timestamp(re.sub(r'\s+', ' ', end[1].strip()))
                if not finished:
                    continue
                latest_transaction = max(latest_transaction or finished, finished)
                if re.search(r'^Upgrade:', block, re.M):
                    latest_upgrade = max(latest_upgrade or finished, finished)
        except (OSError, EOFError, ValueError):
            continue
    return {'last_upgrade_at': latest_upgrade, 'last_upgrade_source': 'Completed APT history transaction' if latest_upgrade else None,
            'last_transaction_at': latest_transaction}


def apt_inventory():
    try:
        import apt
        import apt_pkg
    except ImportError:
        raise RuntimeError('python3-apt is required to read the local package cache.') from None
    packages = []
    installed_count = 0
    for package in apt.Cache():
        installed_count += bool(package.is_installed)
        if package.is_upgradable and package.installed and package.candidate:
            origins = package.candidate.origins
            packages.append({'name': package.name, 'installed': package.installed.version,
                             'candidate': package.candidate.version,
                             'security': any('security' in o.archive.lower() for o in origins),
                             'trusted': any(o.trusted for o in origins),
                             'held': package._pkg.selected_state == apt_pkg.SELSTATE_HOLD})
    packages.sort(key=lambda p: (not p['security'], p['name']))
    stamps = [p.stat().st_mtime for p in Path('/var/lib/apt/lists').glob('*InRelease') if p.is_file()]
    stamp = Path('/var/lib/apt/periodic/update-success-stamp')
    success = datetime.datetime.fromtimestamp(stamp.stat().st_mtime, datetime.timezone.utc).isoformat() if stamp.is_file() else None
    checked = now()
    return {'checked_at': checked, 'updates_checked_at': checked, 'packages': packages, 'count': len(packages), 'installed_count': installed_count,
            'security_count': sum(p['security'] for p in packages),
            'list_timestamp': datetime.datetime.fromtimestamp(max(stamps), datetime.timezone.utc).isoformat() if stamps else None,
            'list_timestamp_notice': 'Repository metadata file timestamp; not the time apt update last succeeded.',
            'last_refresh_at': success, 'last_refresh_source': 'APT periodic success stamp' if success else None,
            'reboot_requested': Path('/var/run/reboot-required').exists(), **apt_history()}


def snap_inventory(binary, check_updates=False):
    installed = []
    for line in read_command([binary, 'list', '--unicode=never', '--color=never']).splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] != 'Name':
            installed.append({'name': fields[0], 'installed': fields[1], 'revision': fields[2]})
    data = {'installed_packages': installed, 'installed_count': len(installed), 'count': None, 'packages': [],
            'updates_checked_at': None, 'last_refresh_at': None, 'last_refresh_source': None,
            'last_upgrade_at': None, 'last_upgrade_source': None, 'last_auto_refresh_at': None, 'next_auto_refresh_at': None}
    try:
        for line in read_command([binary, 'refresh', '--time', '--abs-time']).splitlines():
            key, _, value = line.partition(': ')
            if key in {'last', 'next'}:
                data['last_auto_refresh_at' if key == 'last' else 'next_auto_refresh_at'] = timestamp(value)
        for line in read_command([binary, 'changes', '--abs-time']).splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5 and fields[1] == 'Done' and 'refresh' in fields[4].lower():
                finished = timestamp(fields[3])
                if finished:
                    data['last_upgrade_at'] = max(data['last_upgrade_at'] or finished, finished)
        if data['last_upgrade_at']:
            data['last_upgrade_source'] = 'Completed snapd refresh change'
    except (RuntimeError, subprocess.SubprocessError) as error:
        data['history_error'] = str(error)[:500]
    if check_updates:
        output = read_command([binary, 'refresh', '--list', '--unicode=never', '--color=never'], timeout=35)
        versions = {p['name']: p['installed'] for p in installed}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] != 'Name' and fields[2].isdigit():
                data['packages'].append({'name': fields[0], 'installed': versions.get(fields[0], ''), 'candidate': fields[1],
                                         'security': False, 'trusted': True, 'held': False})
        data.update(count=len(data['packages']), updates_checked_at=now())
    return data


def flatpak_inventory(binary, check_updates=False):
    installed = []
    for line in read_command([binary, 'list', '--columns=application,version,installation']).splitlines():
        fields = line.split('\t')
        if len(fields) >= 2:
            installed.append({'name': fields[0], 'installed': fields[1], 'installation': fields[2] if len(fields) > 2 else ''})
    data = {'installed_packages': installed, 'installed_count': len(installed), 'count': None, 'packages': [],
            'updates_checked_at': None, 'last_refresh_at': None, 'last_refresh_source': None,
            'last_upgrade_at': None, 'last_upgrade_source': None}
    if check_updates:
        versions = {p['name']: p['installed'] for p in installed}
        for scope in ('--user', '--system'):
            output = read_command([binary, scope, 'remote-ls', '--updates', '--columns=application,version,branch'], timeout=35)
            for line in output.splitlines():
                fields = line.split('\t')
                if len(fields) >= 2:
                    data['packages'].append({'name': fields[0], 'installed': versions.get(fields[0], ''), 'candidate': fields[1],
                                             'installation': scope[2:], 'security': False, 'trusted': True, 'held': False})
        data.update(count=len(data['packages']), updates_checked_at=now())
    return data


def manager_inventory(manager, check_updates=False):
    binary = manager_binary(manager)
    data = {'id': manager, 'name': MANAGERS[manager][0], 'state': 'ok', 'checked_at': now(), 'error': '',
            'actions': ['check', 'refresh-lists', 'upgrade'] if manager == 'apt' else ['check', 'upgrade']}
    try:
        data.update(apt_inventory() if manager == 'apt' else snap_inventory(binary, check_updates) if manager == 'snap'
                    else flatpak_inventory(binary, check_updates))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        data.update(state='error', error=str(error)[:500], packages=[], count=None)
    return data


def inventory():
    managers = [manager_inventory(manager) for manager, (_, binary) in MANAGERS.items() if shutil.which(binary, path=SYSTEM_PATH)]
    apt = next((manager for manager in managers if manager['id'] == 'apt'), {})
    from local_tool_job import inventory as tool_inventory
    managers.extend(tool_inventory())
    return {**apt, 'checked_at': now(), 'manager': 'APT', 'managers': managers,
            'inventory_version': 2,
            'packages': apt.get('packages', []), 'count': apt.get('count'), 'security_count': apt.get('security_count', 0),
            'reboot_requested': Path('/var/run/reboot-required').exists()}


def save_result(path, result):
    path = Path(path)
    if path.is_symlink() or not path.parent.is_dir():
        raise RuntimeError('Invalid result file.')
    fd, temporary = tempfile.mkstemp(prefix='.result-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(result, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def interactive(operation, result_path, manager='apt'):
    """No shell, no sudo password collection, no automatic yes or removals."""
    from go_toolchain import confirm_yes
    binary = manager_binary(manager)
    if operation not in ({'refresh-lists', 'upgrade'} if manager == 'apt' else {'upgrade'}):
        raise RuntimeError('Unsupported package action.')
    result = {'state': 'running', 'operation': operation, 'manager': manager, 'completed_steps': [],
              'pid': os.getpid(), 'started_at': now(), 'message': 'Waiting in terminal'}
    save_result(result_path, result)
    lock_path = Path(result_path).parent / 'package-operation.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print('PC updates — this computer only.\n')
            print('sudo credentials stay in this terminal. Close or press Ctrl+C to cancel.\n')
            if manager == 'apt':
                commands = [('refresh-lists', ['/usr/bin/sudo', binary, '-o', 'APT::Update::Error-Mode=any', 'update'])]
                if operation == 'upgrade':
                    commands.append(('upgrade', ['/usr/bin/sudo', binary, '--no-remove', 'upgrade']))
            else:
                commands = [('upgrade', ['/usr/bin/sudo', binary, 'refresh'])] if manager == 'snap' else [
                    ('upgrade', [binary, '--user', 'update']), ('upgrade', [binary, '--system', 'update'])]
            for _, command in commands:
                print(' '.join(command))
            if not confirm_yes('\nRun these commands? Type yes to continue: '):
                raise RuntimeError('Cancelled. No package commands were run.')
            for step, command in commands:
                result['message'] = 'Refreshing APT package lists' if step == 'refresh-lists' else 'Review updates in the terminal'
                save_result(result_path, result)
                completed = subprocess.run(command)
                if completed.returncode:
                    raise RuntimeError(MANAGERS[manager][0] + ' cancelled or failed. Review the terminal output.')
                result['completed_steps'].append({'manager': manager, 'operation': step, 'finished_at': now()})
                save_result(result_path, result)
            result.update(state='done', message='Package lists refreshed' if operation == 'refresh-lists' else MANAGERS[manager][0] + ' operation finished')
        except (KeyboardInterrupt, BlockingIOError):
            result.update(state='error', message='Cancelled, or another package update is already running.')
        except Exception as error:
            result.update(state='error', message=str(error)[:500])
        finally:
            result['finished_at'] = now()
            save_result(result_path, result)
            print('\n' + result['message'])
            print('You can close this terminal.')
    return 0 if result['state'] == 'done' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['list', 'check', 'refresh-lists', 'upgrade'])
    parser.add_argument('--manager', choices=MANAGERS)
    parser.add_argument('--result')
    args = parser.parse_args()
    if args.operation in {'list', 'check'}:
        try:
            print(json.dumps(manager_inventory(args.manager, check_updates=args.operation == 'check') if args.manager else inventory()))
        except Exception as error:
            print(json.dumps({'error': str(error)}))
            raise SystemExit(1)
    elif not args.result:
        parser.error('--result is required for terminal operations')
    else:
        raise SystemExit(interactive(args.operation, args.result, args.manager or 'apt'))
