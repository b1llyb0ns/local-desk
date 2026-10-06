"""Local tool inventory and explicit, per-item terminal updates."""
import argparse
import fcntl
import json
import os
from pathlib import Path

from pc_packages import now, save_result

TOOL_MANAGERS = ('go', 'pdtm', 'go-tools', 'pipx')


def inventory(manager=None, check=False, home=None):
    import go_toolchain
    import tool_packages
    home = Path(home or Path.home())
    if manager is not None and manager not in TOOL_MANAGERS:
        raise RuntimeError('Unsupported tool manager.')
    if manager == 'go':
        return go_toolchain.check_inventory() if check else go_toolchain.local_inventory(home)
    if manager:
        return tool_packages.manager_inventory(manager, home, check=check)
    records = tool_packages.inventory(home, check=False)
    go = go_toolchain.local_inventory(home)
    if go.get('installed_version') or go.get('installed_count'):
        records.insert(0, go)
    return records


def interactive(manager, item, version, result_path, allow_unknown=False):
    from go_toolchain import confirm_yes
    if manager not in TOOL_MANAGERS:
        raise RuntimeError('Unsupported tool manager.')
    result = {'state': 'running', 'manager': manager, 'item': item, 'version': version,
              'operation': 'upgrade', 'completed_steps': [], 'pid': os.getpid(),
              'started_at': now(), 'message': 'Waiting for terminal confirmation'}
    save_result(result_path, result)
    with (Path(result_path).parent / 'package-operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(f'Update {item} to {version} on this PC.\n')
            if manager == 'go':
                if item != 'go' or allow_unknown:
                    raise RuntimeError('Invalid Go toolchain request.')
                from go_toolchain import interactive_update
                installed = interactive_update(version)
            else:
                if allow_unknown:
                    print('The installed release cannot be verified. Installing stable may replace a newer build. A backup will be retained.\n')
                if not confirm_yes('Type yes to update this tool: '):
                    raise RuntimeError('Cancelled. No tool was changed.')
                from tool_packages import update_item
                installed = update_item(manager, item, version, Path.home(), allow_unknown_version=allow_unknown)
            if not isinstance(installed, dict) or installed.get('state') != 'done' or installed.get('version') != version:
                raise RuntimeError('The tool update did not complete.')
            result['completed_steps'].append({'manager': manager, 'operation': 'upgrade', 'item': item,
                                              'version': version, 'finished_at': now()})
            result.update(state='done', message=f'{item}: {version} installed', backup=installed.get('backup'))
        except (KeyboardInterrupt, BlockingIOError):
            result.update(state='error', message='Cancelled, or another local package operation is running.')
        except Exception as error:
            result.update(state='error', message=str(error)[:700])
        finally:
            result['finished_at'] = now()
            save_result(result_path, result)
            print('\n' + result['message'])
            if result.get('backup'):
                print('Backup: ' + str(result['backup']))
            print('You can close this terminal.')
    return 0 if result['state'] == 'done' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['list', 'check', 'install'])
    parser.add_argument('--manager', choices=TOOL_MANAGERS)
    parser.add_argument('--item')
    parser.add_argument('--version')
    parser.add_argument('--result')
    parser.add_argument('--allow-unknown-version', action='store_true')
    args = parser.parse_args()
    if args.operation == 'install':
        if not all((args.manager, args.item, args.version, args.result)):
            parser.error('manager, item, version and result are required')
        raise SystemExit(interactive(args.manager, args.item, args.version, args.result, args.allow_unknown_version))
    if args.operation == 'check' and not args.manager:
        parser.error('manager is required for update checks')
    try:
        print(json.dumps(inventory(args.manager, args.operation == 'check')))
    except Exception as error:
        print(json.dumps({'error': str(error)[:700]}))
        raise SystemExit(1)
