#!/usr/bin/env python3
"""Rotate the existing Kibana reader password without printing credentials."""
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Run as root on the Kubernetes node.')

base = Path('/var/lib/codex-logging-secrets')
base_info = base.lstat()
if not stat.S_ISDIR(base_info.st_mode) or base_info.st_uid != 0 or base_info.st_mode & 0o077:
    raise SystemExit('Unsafe credential directory.')
username_file = base / 'kibana-reader-username'
password_file = base / 'kibana-reader-password'
pending = base / 'kibana-reader-password.pending'
for path in (username_file, password_file):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise SystemExit(f'Unsafe credential file: {path}')
username = username_file.read_text(encoding='utf-8').strip()
if not re.fullmatch(r'log_reader_[0-9a-f]{32}', username):
    raise SystemExit('Unexpected reader username; refusing to change it.')

# Keep the replacement if the API result is uncertain. A failed network request
# can still have been committed by Elasticsearch, so do not silently delete it.
replacement = secrets.token_urlsafe(32)
fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as out:
    out.write(replacement + '\n')
    out.flush()
    os.fsync(out.fileno())
dir_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
payload = json.dumps({'password': replacement}).encode('utf-8')
command = [
    'k3s', 'kubectl', '-n', 'logging', 'exec', '-i', 'elasticsearch-0', '--',
    'curl', '--config', '/usr/share/elasticsearch/config/auth/curl.conf',
    '--fail', '--silent', '--show-error', '--max-time', '30', '-X', 'POST',
    '-H', 'Content-Type: application/json', '--data-binary', '@-',
    f'https://127.0.0.1:9200/_security/user/{username}/_password',
]
try:
    result = subprocess.run(command, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=45, check=False)
except (OSError, subprocess.TimeoutExpired) as exc:
    print(f'Password update uncertain ({type(exc).__name__}); keep {pending} for recovery.', file=sys.stderr)
    raise SystemExit(1)
if result.returncode != 0:
    print(f'Password update uncertain (exit {result.returncode}); keep {pending} for recovery.', file=sys.stderr)
    raise SystemExit(1)
try:
    os.replace(pending, password_file)
    dir_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except OSError as exc:
    print(f'Password updated; check credential files manually ({type(exc).__name__}).', file=sys.stderr)
    raise SystemExit(1)
print('Reader password rotated and saved in the root-only credential file.')
