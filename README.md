# Local Desk

A local dashboard for managing your Linux computer and VPS servers.
Runs in your browser, stores data on your computer, and connects over SSH.

## Features

- Servers: resource usage, processes, services, notes, and renewal dates.
- Computer: installed packages, updates, and open ports.
- Tasks, a diary, and expense tracking.

## Getting started

Requires Linux, Python 3.10+, and OpenSSH. No additional Python packages are needed.

```sh
git clone https://github.com/b1llyb0ns/local-desk.git
cd local-desk
python3 server.py
```

Open **http://127.0.0.1:8787**.

Before starting, you need your own SSH key pair at `~/.ssh/id_ed25519`
and `~/.ssh/id_ed25519.pub`. If you do not have one, create it with
`ssh-keygen -t ed25519`. Do not overwrite an existing key.

A new installation starts empty. Add your servers and records through the interface.
Data is stored in `~/.local/share/local-desk/`. To use another directory, run
`python3 server.py --data-dir /path/to/data`.

## Server access

Refreshing statistics only reads data over SSH. Access setup is a separate action
that installs your SSH key and disables SSH password login. Keep access to your
provider's console before using it.

The dashboard is intended for a personal computer and listens on localhost only.
Do not expose it to the internet. Your keys and records are not included in this repository.

## Checks

```sh
mkdir -p tmp
python3 -m unittest discover -s tests
node tests/frontend_performance.mjs
```

The frontend checks require Node.js.
