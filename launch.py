#!/usr/bin/env python3
"""Start the local user service and open its loopback-only page."""
import subprocess
import time
import urllib.request

URL='http://127.0.0.1:8787/'
subprocess.run(['systemctl','--user','start','local-desk.service'],check=True)
for attempt in range(30):
    try:
        with urllib.request.urlopen(URL,timeout=1):break
    except OSError:time.sleep(.1)
else:
    raise SystemExit('Could not start the panel. Check: systemctl --user status local-desk.service')
subprocess.Popen(['xdg-open',URL],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
