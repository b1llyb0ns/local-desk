import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent


class SSHError(Exception):
    def __init__(self, message, kind='ssh_error', retry_hint=''):
        super().__init__(message)
        self.kind = kind
        self.retry_hint = retry_hint


def explain_error(stderr):
    lowered = stderr.lower()
    if 'REMOTE HOST IDENTIFICATION HAS CHANGED' in stderr:
        return SSHError('Host key changed. Verify the change before updating the saved fingerprint.', 'host_key_changed')
    if 'Host key verification failed' in stderr:
        return SSHError('Host key not confirmed. Use access setup.', 'host_key_unknown')
    if 'Permission denied' in stderr or 'incorrect password' in stderr:
        return SSHError('Login failed. Check the user, password or selected SSH key.', 'credentials_needed')
    if 'No route to host' in stderr:
        return SSHError('SSH unreachable: no route to the server. Check the VPS and hosting firewall.', 'unreachable')
    if 'Connection refused' in stderr:
        return SSHError('SSH connection refused. Check the port and SSH service.', 'unreachable')
    if 'timeout, server ' in lowered and 'not responding' in lowered:
        return SSHError('Server response timed out.', 'timeout', 'ipqos_ef')
    if 'timed out' in lowered:
        return SSHError('Server response timed out.', 'timeout')
    if 'sudo:' in stderr:
        return SSHError('Collecting data requires root or passwordless sudo.', 'sudo_required')
    return SSHError((stderr.strip() or 'SSH connection failed.')[-900:])


@contextlib.contextmanager
def password_channel(password):
    if password is None:
        yield {}
        return
    with tempfile.TemporaryDirectory(prefix='local-desk-auth-') as directory:
        path = str(Path(directory) / 'password.sock')
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(0.25)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    connection.settimeout(3)
                    try:
                        if connection.recv(100) == b'password\n':
                            connection.sendall(password.encode() + b'\n')
                    except OSError:
                        pass
                return  # One password prompt per connection.

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield {'SSH_ASKPASS': str(HERE / 'askpass.py'), 'SSH_ASKPASS_REQUIRE': 'force',
                   'DISPLAY': 'local-desk:0', 'VPS_DESK_SECRET_SOCKET': path}
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=1)


def limited_run(args, data=b'', env=None, timeout=45, maximum=3_000_000):
    """Bound output and lifetime; neither passwords nor output are put in temp files."""
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, start_new_session=True)

    def write():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise SSHError('SSH request timed out.', 'timeout')
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if sum(map(len, output.values())) > maximum:
                        raise SSHError('Server response exceeds the size limit.', 'invalid_response')
        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        return code, output['stdout'].decode(errors='replace'), output['stderr'].decode(errors='replace')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()
        process.stderr.close()
        writer.join(timeout=1)


class SSHEngine:
    def __init__(self, user_home, data_dir):
        self.home = Path(user_home)
        self.ssh_dir = self.home / '.ssh'
        self.key = self.ssh_dir / 'id_ed25519'
        self.known_hosts = self.ssh_dir / 'known_hosts'
        self.data_dir = Path(data_dir)
        self.host_lock = threading.Lock()
        self.public_key = (self.ssh_dir / 'id_ed25519.pub').read_text().strip()
        parts = self.public_key.split()
        if len(parts) < 2 or parts[0] not in {'ssh-ed25519', 'ssh-rsa'}:
            raise ValueError('A valid id_ed25519.pub public key was not found.')
        self.fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(parts[1])).digest()).decode().rstrip('=')

    def key_options(self):
        keys = []
        candidates = list(self.ssh_dir.glob('*.pub')) + list((self.ssh_dir / 'backups').glob('*/legacy-keys/*.pub'))
        for public in sorted(candidates):
            private = public.with_suffix('')
            if private.is_file() and not private.is_symlink():
                identifier = hashlib.sha256(str(private).encode()).hexdigest()[:20]
                keys.append({'id': identifier, 'name': private.name, 'path': str(private),
                             'archived': 'backups' in private.parts})
        return keys

    def run(self, server, command, data=b'', auth=None, user=None, accept_new=False, timeout=45, ipqos=None):
        if ipqos is None:
            ipqos = server.get('metrics_ipqos') or None
        if ipqos not in {None, 'ef'}:
            raise ValueError('Unsupported SSH IPQoS mode.')
        user = user or server.get('ssh_user', 'root')
        auth = auth or {'mode': 'managed'}
        mode = auth.get('mode', 'managed')
        key = self.key
        if mode == 'key':
            matches = [k for k in self.key_options() if k['id'] == auth.get('key_id')]
            if not matches:
                raise SSHError('Selected key no longer exists.', 'credentials_needed')
            key = Path(matches[0]['path'])
        args = ['ssh', '-F', '/dev/null', '-T', '-o', 'ConnectTimeout=8',
                '-o', 'ConnectionAttempts=1', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ForwardAgent=no',
                '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
                '-o', 'StrictHostKeyChecking=' + ('accept-new' if accept_new else 'yes'),
                '-o', 'UserKnownHostsFile=' + str(self.known_hosts),
                '-p', str(server.get('port', 22))]
        if ipqos:
            args += ['-o', 'IPQoS=' + ipqos]
        if mode == 'password':
            args += ['-o', 'BatchMode=no', '-o', 'PubkeyAuthentication=no', '-o', 'PasswordAuthentication=yes',
                     '-o', 'KbdInteractiveAuthentication=no', '-o', 'PreferredAuthentications=password',
                     '-o', 'NumberOfPasswordPrompts=1']
        else:
            args += ['-o', 'BatchMode=yes', '-o', 'PasswordAuthentication=no',
                     '-o', 'KbdInteractiveAuthentication=no', '-o', 'PreferredAuthentications=publickey', '-i', str(key)]
        args += [user + '@' + server['host'], command]
        with password_channel(auth.get('password') if mode == 'password' else None) as additions:
            return limited_run(args, data=data, env=dict(os.environ, **dict(additions, LC_ALL='C')), timeout=timeout)

    def probe(self, server, auth=None, user=None, accept_new=False):
        code = "import os,json,subprocess; p=subprocess.run(['sudo','-n','true'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) if os.getuid()!=0 else None; print(json.dumps({'uid':os.getuid(),'sudo':p is None or p.returncode==0}))"
        result, stdout, stderr = self.run(server, 'python3 -c ' + shlex.quote(code), auth=auth, user=user, accept_new=accept_new)
        if result and not server.get('metrics_ipqos') and explain_error(stderr).retry_hint == 'ipqos_ef':
            # Only replay the read-only login check. Setup mutations must use
            # the working transport from the start, never be retried blindly.
            result, stdout, stderr = self.run(server, 'python3 -c ' + shlex.quote(code),
                                              auth=auth, user=user, accept_new=accept_new, ipqos='ef')
            if not result:
                server['metrics_ipqos'] = 'ef'
        if result:
            raise explain_error(stderr)
        try:
            return json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise SSHError('Python 3 is required on the server.', 'python_required')

    def check(self, server):
        """Measure a fresh authenticated SSH connection without collecting metrics."""
        def connect(ipqos=None):
            started = time.monotonic()
            code, _, stderr = self.run(server, 'true', timeout=15, ipqos=ipqos)
            if code:
                raise explain_error(stderr)
            return round((time.monotonic() - started) * 1000, 1)

        try:
            return connect()
        except SSHError as error:
            if error.kind != 'timeout' or server.get('metrics_ipqos'):
                raise
            latency = connect('ef')
            server['metrics_ipqos'] = 'ef'
            return latency

    def collect(self, server):
        prefix = 'python3 -' if server['ssh_user'] == 'root' else 'sudo -n python3 -'
        ipqos = server.get('metrics_ipqos') or None
        code, stdout, stderr = self.run(server, prefix, (HERE / 'remote_metrics.py').read_bytes(),
                                        timeout=50, ipqos=ipqos)
        if code:
            raise explain_error(stderr)
        try:
            result = json.loads(stdout)
            if not isinstance(result.get('cpu_pct'), (float, int)) or not isinstance(result.get('processes'), list):
                raise ValueError('Unexpected snapshot')
            return result
        except ValueError:
            raise SSHError('Could not read the server snapshot.', 'invalid_response')

    def controller(self, server, action, token, auth=None, user='root', sudo_password=None, payload=None):
        source = (HERE / 'remote_setup.py').read_text()
        encoded = base64.b64encode(source.encode()).decode()
        expression = "import base64; exec(compile(base64.b64decode('" + encoded + "'),'<local-desk>','exec'))"
        cmd = 'python3 -c ' + shlex.quote(expression) + ' ' + action + ' ' + token
        data = json.dumps(payload or {}).encode()
        if user != 'root':
            if sudo_password is None:
                cmd = 'sudo -n ' + cmd
            else:
                cmd = "sudo -S -p '' " + cmd
                data = sudo_password.encode() + b'\n' + data
        code, stdout, stderr = self.run(server, cmd, data=data, auth=auth, user=user, timeout=100)
        try:
            response = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise explain_error(stderr)
        if code or not response.get('ok'):
            raise SSHError(response.get('error', 'Could not configure SSH.'), 'setup_failed')
        return response

    def onboard(self, server, credentials, progress):
        token = secrets.token_hex(12)
        initial_user = credentials.get('user', 'root')
        auth = {k: credentials.get(k) for k in ('mode', 'password', 'key_id')}
        prepared = False
        setup_attempted = False
        final_verified = False
        try:
            progress(1, 'Checking SSH login; no configuration changes yet')
            if initial_user == 'root' and auth['mode'] != 'managed':
                try:
                    probe = self.probe(server, user='root', accept_new=True)
                    auth = {'mode': 'managed'}
                except SSHError as error:
                    if error.kind != 'credentials_needed':
                        raise
                    progress(1, 'Checking the supplied login; no configuration changes yet')
                    probe = self.probe(server, auth=auth, user=initial_user, accept_new=True)
            else:
                probe = self.probe(server, auth=auth, user=initial_user, accept_new=True)
            sudo_password = None
            if probe['uid'] != 0 and not probe['sudo']:
                sudo_password = credentials.get('sudo_password') or credentials.get('password')
                if not sudo_password:
                    raise SSHError('This user requires a sudo password.', 'sudo_required')
            progress(2, 'Installing SSH key and preparing automatic rollback')
            setup_attempted = True
            response = self.controller(server, 'prepare', token, auth=auth, user=initial_user,
                                       sudo_password=sudo_password,
                                       payload={'public_key': self.public_key,
                                                'controller_source': (HERE / 'remote_setup.py').read_text(),
                                                'only_managed': credentials.get('only_managed', True)})
            prepared = True
            progress(3, 'Verifying separate root login with SSH key')
            if self.probe(server, user='root')['uid'] != 0:
                raise SSHError('Root verification login failed.', 'setup_failed')
            progress(4, 'Disable SSH password login')
            self.controller(server, 'finalize', token)
            progress(5, 'Verifying login after configuration changes')
            if self.probe(server, user='root')['uid'] != 0:
                raise SSHError('Final root verification failed.', 'setup_failed')
            final_verified = True
            self.controller(server, 'commit', token)
            progress(6, 'Done: root + SSH key; SSH password login disabled')
            return {'backup': response['backup'], 'ssh_user': 'root'}
        except Exception as error:
            if setup_attempted:
                try:
                    rollback = self.controller(server, 'rollback', token)
                    if rollback.get('phase') == 'committed' and final_verified:
                        progress(6, 'Done: root + SSH key. Confirmation recovered after connection loss.')
                        return {'backup': rollback['backup'], 'ssh_user': 'root'}
                    if rollback.get('phase') != 'rolled_back':
                        raise SSHError('Rollback state not confirmed.')
                    message = str(error) + ' Original SSH settings restored.'
                except Exception:
                    if prepared:
                        message = str(error) + ' Rollback requested. If the connection was lost, the preinstalled timer will run within 5 minutes while the VPS is running.'
                    else:
                        message = str(error) + ' Setup not confirmed. If changes started, the rollback timer will revert them within 5 minutes while the VPS is running.'
                raise SSHError(message, getattr(error, 'kind', 'setup_failed')) from None
            if isinstance(error, SSHError):
                raise SSHError(str(error) + ' Setup stopped before key installation. SSH configuration was not changed by this attempt.', error.kind) from None
            raise
        finally:
            # Passwords only lived in this request/worker and the one-use socket.
            auth.clear()
            credentials.clear()

    def accept_host_key(self, server):
        host, port = server['host'], server['port']
        code, output, _ = limited_run(['ssh-keyscan', '-T', '6', '-p', str(port), '-t', 'ed25519', host], timeout=10)
        lines = [line for line in output.splitlines() if not line.startswith('#') and len(line.split()) == 3]
        if code or not lines:
            raise SSHError('Could not retrieve the host key.', 'unreachable')
        expected = host if port == 22 else '[' + host + ']:' + str(port)
        for line in lines:
            parts = line.split()
            if parts[0] != expected or parts[1] != 'ssh-ed25519':
                raise SSHError('Unexpected SSH response.', 'invalid_response')
            base64.b64decode(parts[2], validate=True)
        with self.host_lock:
            backup = self.data_dir / ('known_hosts-' + str(time.time_ns()))
            if self.known_hosts.exists():
                shutil.copy2(self.known_hosts, backup)
                backup.chmod(0o600)
            subprocess.run(['ssh-keygen', '-R', expected, '-f', str(self.known_hosts)], capture_output=True, check=False)
            with self.known_hosts.open('a') as target:
                target.write('\n' + '\n'.join(lines) + '\n')
            self.known_hosts.chmod(0o600)
        return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(lines[0].split()[2])).digest()).decode().rstrip('=')
