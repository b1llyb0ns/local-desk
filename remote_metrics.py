"""One-shot, read-only Linux snapshot. No daemon, environment or process arguments."""
import datetime
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time


def command(args, timeout=12):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL='C', TZ='UTC'))
        return p.stdout.strip() if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def cpu_sample():
    values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return sum(values), values[3] + values[4]


def process_sample():
    result = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit() or int(directory.name) == os.getpid():
            continue
        try:
            raw = (directory / 'stat').read_text()
            start, end = raw.index('('), raw.rindex(')')
            fields = raw[end + 2:].split()
            result[int(directory.name)] = {
                'name': raw[start + 1:end], 'ticks': int(fields[11]) + int(fields[12]),
                'rss': int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                'started': int(fields[19]), 'uid': directory.stat().st_uid,
            }
        except (OSError, ValueError, IndexError):
            continue
    return result


def service_sample():
    raw = command(['systemctl', 'list-units', '--type=service', '--state=running',
                   '--no-pager', '--no-legend', '--plain'])
    names = [line.split()[0] for line in raw.splitlines() if line.split()]
    if not names:
        return []
    raw = command(['systemctl', 'show', *names[:100],
                   '--property=Id,Description,MainPID,MemoryCurrent,TasksCurrent,ControlGroup'])
    result = []
    for block in raw.split('\n\n'):
        values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if values.get('Id'):
            result.append(values)
    return result


def group_cpu(group):
    if not group.startswith('/') or '..' in group.split('/'):
        return None
    try:
        values = dict(line.split() for line in Path('/sys/fs/cgroup' + group, 'cpu.stat').read_text().splitlines())
        return int(values['usage_usec']) / 1e6
    except (OSError, KeyError, ValueError):
        return None


def number(value):
    try:
        n = int(value)
        return n if 0 <= n < 2 ** 63 else None
    except (ValueError, TypeError):
        return None


def main():
    services = service_sample()
    groups0 = {s['Id']: (group_cpu(s.get('ControlGroup', '')), time.monotonic()) for s in services}
    c0, p0, t0 = cpu_sample(), process_sample(), time.monotonic()
    time.sleep(0.65)
    c1, p1, t1 = cpu_sample(), process_sample(), time.monotonic()
    elapsed = max(t1 - t0, 0.01)
    memory = dict((line.split(':', 1)[0], int(line.split()[1]) * 1024)
                  for line in Path('/proc/meminfo').read_text().splitlines())
    total_mem = memory['MemTotal']
    available = memory.get('MemAvailable', memory['MemFree'])
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    ticks = os.sysconf('SC_CLK_TCK')
    processes = []
    for pid, item in p1.items():
        previous = p0.get(pid)
        # PID reuse must not create a false CPU spike.
        delta = max(0, item['ticks'] - previous['ticks']) if previous and previous['started'] == item['started'] else 0
        try:
            user = pwd.getpwuid(item['uid']).pw_name
        except KeyError:
            user = str(item['uid'])
        processes.append({'pid': pid, 'name': item['name'], 'user': user,
                          'cpu_pct': round(delta / ticks / elapsed * 100, 1),
                          'memory_bytes': item['rss'], 'memory_pct': round(item['rss'] / total_mem * 100, 1),
                          'age_seconds': max(0, int(uptime - item['started'] / ticks))})
    by_pid = {p['pid']: p for p in processes}
    service_rows = []
    for service in services:
        main_pid = number(service.get('MainPID'))
        process = by_pid.get(main_pid, {})
        a, group_started = groups0[service['Id']]
        b = group_cpu(service.get('ControlGroup', ''))
        group_elapsed = max(time.monotonic() - group_started, .01)
        cpu = round(max(0, b - a) / group_elapsed * 100, 1) if a is not None and b is not None else process.get('cpu_pct')
        mem = number(service.get('MemoryCurrent'))
        service_rows.append({'name': service['Id'], 'description': service.get('Description', ''),
                             'pid': main_pid, 'cpu_pct': cpu,
                             'memory_bytes': mem if mem is not None else process.get('memory_bytes'),
                             'memory_scope': 'service' if mem is not None else 'main_process',
                             'tasks': number(service.get('TasksCurrent'))})
    os_data = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    disk = shutil.disk_usage('/')
    report = {
        'collected_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'hostname': os.uname().nodename, 'os': os_data.get('PRETTY_NAME', '').strip('"'),
        'kernel': os.uname().release, 'cpu_count': os.cpu_count(),
        'cpu_pct': round(100 * (1 - (c1[1] - c0[1]) / max(1, c1[0] - c0[0])), 1),
        'memory': {'total': total_mem, 'used': total_mem - available,
                   'pct': round((total_mem - available) / total_mem * 100, 1)},
        'disk': {'total': disk.total, 'used': disk.used, 'pct': round(disk.used / disk.total * 100, 1)},
        'uptime_seconds': int(uptime),
        'boot_at': datetime.datetime.fromtimestamp(time.time() - uptime, datetime.timezone.utc).isoformat(),
        'load': list(os.getloadavg()),
        'processes': sorted(processes, key=lambda p: (p['cpu_pct'], p['memory_bytes']), reverse=True)[:60],
        'services': sorted(service_rows, key=lambda s: (s['cpu_pct'] or 0, s['memory_bytes'] or 0), reverse=True),
        'failed_units': command(['systemctl', '--failed', '--no-pager', '--no-legend', '--plain']).splitlines(),
        'ports': command(['ss', '-H', '-lntup']).splitlines(),
        'containers': [], 'packages': [], 'manual_tools': [], 'privileged': os.getuid() == 0,
    }
    raw = command(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Status}\n'])
    for line in raw.splitlines():
        fields = line.split('\t')
        if len(fields) == 3 and fields[2] == 'install ok installed':
            report['packages'].append({'name': fields[0], 'version': fields[1]})
    for directory in ['/usr/local/bin', '/root/go/bin', '/root/.local/bin', *[str(p) for p in Path('/home').glob('*/go/bin')], *[str(p) for p in Path('/home').glob('*/.local/bin')]]:
        try:
            report['manual_tools'].extend({'name': p.name, 'path': str(p)} for p in Path(directory).iterdir() if p.is_file() and os.access(p, os.X_OK) and not p.name.startswith('.'))
        except OSError:
            pass
    if shutil.which('docker'):
        fields = {'id': '.ID', 'name': '.Names', 'image': '.Image', 'state': '.State', 'status': '.Status', 'ports': '.Ports'}
        template = '{' + ','.join('"' + k + '":{{json ' + v + '}}' for k, v in fields.items()) + '}'
        for line in command(['docker', 'ps', '-a', '--format', template]).splitlines():
            try:
                report['containers'].append(json.loads(line))
            except ValueError:
                pass
        if any(c['state'] == 'running' for c in report['containers']):
            stats = {}
            for line in command(['docker', 'stats', '--no-stream', '--format', '{{json .}}'], timeout=18).splitlines():
                try:
                    item = json.loads(line)
                    stats[item['Name']] = item
                except (ValueError, KeyError):
                    pass
            for container in report['containers']:
                item = stats.get(container['name'], {})
                container.update(cpu=item.get('CPUPerc'), memory=item.get('MemUsage'), memory_pct=item.get('MemPerc'))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
