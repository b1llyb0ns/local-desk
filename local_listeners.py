"""Read local TCP/UDP listening sockets without connecting or changing services."""
import datetime
import ipaddress
import os
import re
import selectors
import shutil
import subprocess
import time


TIMEOUT_SECONDS = 3.0
OUTPUT_LIMIT = 2 * 1024 * 1024
ERROR_LIMIT = 16 * 1024
PROCESS_NOTICE = (
    'Process names and PIDs are limited to what the current user can see. '
    'Other owners may be hidden, even when one owner is shown; no elevation is used.'
)
EXPOSURE_NOTICE = (
    'Bind scope describes the local address only. It does not establish reachability '
    'through a firewall, router, container boundary, or the internet.'
)
_PROCESS = re.compile(r'\("((?:\\.|[^"\\])*)",pid=(\d+)(?:,|\))')
_PRIVATE_V4 = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def classify_exposure(address):
    """Classify a bind address, without inferring firewall or internet reachability."""
    unknown = ' Firewall and NAT reachability are unknown; no connection check was made.'
    host, _, scope = address.partition('%')
    if host == '*':
        return 'wildcard', 'Wildcard bind; it may accept traffic on local network interfaces.' + unknown
    parsed = ipaddress.ip_address(host)
    parsed = getattr(parsed, 'ipv4_mapped', None) or parsed
    if parsed.is_loopback:
        return 'local-only', 'Loopback bind: directly reachable only within this network namespace. Forwarding was not checked.'
    if parsed.is_unspecified:
        detail = ' on interface ' + scope if scope else ' on all interfaces of this address family'
        return 'wildcard', 'Wildcard bind' + detail + '.' + unknown
    private = (parsed.version == 4 and any(parsed in network for network in _PRIVATE_V4)) or (
        parsed.version == 6 and parsed in ipaddress.ip_network('fc00::/7'))
    if private or parsed.is_link_local:
        return 'private-LAN', 'Private or link-local bind; it may be reachable from its local or routed private network.' + unknown
    if parsed.is_multicast or parsed.is_reserved or getattr(parsed, 'is_site_local', False) or not parsed.is_global:
        return 'special-use', 'Special-use address; public reachability cannot be inferred from this bind.' + unknown
    return 'public-address', 'Bind uses a globally routable address; that alone does not establish internet access.' + unknown


def _endpoint(value):
    if value.startswith('['):
        match = re.fullmatch(r'\[([^\]]+)\]:(\d+|\*)', value)
        if not match:
            raise ValueError('Invalid bracketed address')
        address, port = match.groups()
    else:
        address, separator, port = value.rpartition(':')
        if not separator or not address or not re.fullmatch(r'\d+|\*', port):
            raise ValueError('Invalid address')
    if port != '*' and not 0 <= int(port) <= 65535:
        raise ValueError('Invalid port')
    if address == '*':
        return address, port, 'unspecified', 'all_interfaces'
    host, separator, scope = address.partition('%')
    if separator and (not scope or '%' in scope):
        raise ValueError('Invalid address scope')
    parsed = ipaddress.ip_address(host)
    family = 'ipv4' if parsed.version == 4 else 'ipv6'
    mapped = getattr(parsed, 'ipv4_mapped', None)
    if parsed.is_loopback or (mapped is not None and mapped.is_loopback):
        exposure = 'loopback'
    elif parsed.is_unspecified and not scope:
        exposure = 'all_interfaces'
    else:
        exposure = 'specific_interface'
    return address, port, family, exposure


def parse_ss(output):
    """Parse numeric, headerless ss output; preserve distinct socket rows."""
    listeners = []
    skipped = 0
    for line in output.splitlines():
        fields = line.split(None, 6)
        if not fields or fields[0].lower() in {'netid', 'u_str', 'u_dgr', 'u_seq', 'unix'}:
            continue
        try:
            if len(fields) < 6 or fields[0].lower() not in {'tcp', 'tcp6', 'udp', 'udp6'}:
                raise ValueError('Not a TCP/UDP socket')
            netid, state, receive, send, local, peer = fields[:6]
            protocol = netid.lower()[:3]
            if state not in ({'LISTEN'} if protocol == 'tcp' else {'UNCONN', 'LISTEN'}):
                raise ValueError('Not a listening socket')
            if not receive.isdecimal() or not send.isdecimal():
                raise ValueError('Invalid queue counters')
            address, port, family, exposure = _endpoint(local)
            _, _, peer_family, _ = _endpoint(peer)
            if port == '*':
                raise ValueError('No numeric local port')
            if family == 'unspecified':
                family = 'ipv6' if netid.lower().endswith('6') else peer_family
            owners = {}
            for name, pid in _PROCESS.findall(fields[6] if len(fields) > 6 else ''):
                if int(pid) > 0:
                    name = re.sub(r'\\([\\"])', r'\1', name)
                    owners[(int(pid), name)] = {'name': name, 'pid': int(pid)}
            processes = [owners[key] for key in sorted(owners)]
            exposure_class, exposure_reason = classify_exposure(address)
            listeners.append({
                'protocol': protocol, 'state': state, 'address': address,
                'port': int(port), 'family': family, 'exposure': exposure,
                'processes': processes,
                'process_visibility': 'visible' if processes else 'unavailable',
                'exposure_class': exposure_class, 'exposure_reason': exposure_reason,
                'internet_reachability': 'not_checked',
            })
        except ValueError:
            skipped += 1
    listeners.sort(key=lambda item: (item['protocol'], item['port'], item['family'], item['address']))
    return listeners, skipped


def _ss_path():
    if os.path.isfile('/usr/bin/ss') and os.access('/usr/bin/ss', os.X_OK):
        return '/usr/bin/ss'
    path = shutil.which('ss', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not path:
        raise RuntimeError('The local ss utility is unavailable.')
    return path


def _run_ss():
    """Run one fixed command and bound both runtime and captured pipe output."""
    command = [_ss_path(), '-H', '-lntup']
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    limits = {'stdout': OUTPUT_LIMIT, 'stderr': ERROR_LIMIT}
    error = None
    truncated = False
    deadline = time.monotonic() + TIMEOUT_SECONDS
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'LC_ALL': 'C', 'LANG': 'C', 'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'},
        shell=False,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = 'The local socket listing timed out; results may be incomplete.'
                        truncated = True
                        break
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = buffers[key.data]
                        available = limits[key.data] - len(buffer)
                        buffer.extend(chunk[:available])
                        if len(chunk) > available:
                            error = 'The local socket listing exceeded its output limit; results are incomplete.'
                            truncated = True
                            break
                    if truncated:
                        break
            if not truncated:
                try:
                    returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    error = 'The local socket listing timed out; results may be incomplete.'
                    truncated = True
                else:
                    if returncode:
                        error = 'The local ss utility could not complete the socket listing.'
                    elif buffers['stderr']:
                        error = 'The local ss utility reported a warning; results may be incomplete.'
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
    output = bytes(buffers['stdout'])
    if truncated and output and not output.endswith(b'\n'):
        output = output.rsplit(b'\n', 1)[0] + b'\n' if b'\n' in output else b''
    return output.decode('utf-8', errors='replace'), error, truncated


def snapshot():
    """Return a JSON-ready local inventory. No caller-provided commands or hosts."""
    result = {
        'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'state': 'ok', 'listeners': [], 'count': 0,
        'process_visibility': 'unknown', 'process_visibility_notice': PROCESS_NOTICE,
        'exposure_notice': EXPOSURE_NOTICE, 'skipped_lines': 0,
        'truncated': False, 'error': None,
    }
    try:
        output, error, truncated = _run_ss()
        listeners, skipped = parse_ss(output)
        result.update(listeners=listeners, count=len(listeners), skipped_lines=skipped,
                      truncated=truncated, error=error)
        if listeners:
            result['process_visibility'] = (
                'limited' if any(not row['processes'] for row in listeners) else 'visible'
            )
        if error or skipped:
            result['state'] = 'partial' if listeners else 'error'
            result['error'] = error or 'Some socket rows could not be parsed; results are incomplete.'
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result.update(state='error', error=str(error)[:300])
    return result
