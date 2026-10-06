#!/usr/bin/env python3
"""Read a one-use SSH password over a private local socket. Never persist it."""
import os
import socket
import sys

if not sys.argv[1:] or 'password' not in sys.argv[1].lower():
    raise SystemExit(1)
try:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(8)
        connection.connect(os.environ['VPS_DESK_SECRET_SOCKET'])
        connection.sendall(b'password\n')
        chunks = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        sys.stdout.buffer.write(b''.join(chunks))
except Exception:
    raise SystemExit(1)
