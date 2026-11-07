#!/usr/bin/env python3
"""Add a loopback Kibana destination to the existing dedicated SSH key."""
import argparse
import os
import pathlib
import stat
import tempfile

OLD = ('restrict,port-forwarding,permitopen="127.0.0.1:13000",'
       'permitopen="127.0.0.1:19090",permitlisten="127.0.0.1:13000",'
       'permitlisten="127.0.0.1:19090"')
NEW = OLD + ',permitopen="127.0.0.1:15601"'

parser = argparse.ArgumentParser()
parser.add_argument('public_key', help='Path to the dedicated ED25519 public key')
args = parser.parse_args()
public = pathlib.Path(args.public_key).read_text(encoding='utf-8').strip().split()
if len(public) < 2 or public[0] != 'ssh-ed25519':
    raise SystemExit('Expected an ED25519 public key')
path = pathlib.Path.home() / '.ssh' / 'authorized_keys'
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
    raise SystemExit('authorized_keys must be a regular file owned by this user')
original = path.read_text(encoding='utf-8')
lines = original.splitlines(keepends=True)
matches = [i for i, line in enumerate(lines) if f'ssh-ed25519 {public[1]}' in line]
if len(matches) != 1:
    raise SystemExit('Expected exactly one matching SSH key')
index = matches[0]
if lines[index].startswith(NEW + ' '):
    print('Kibana loopback forwarding is already enabled')
    raise SystemExit(0)
if not lines[index].startswith(OLD + ' '):
    raise SystemExit('Unexpected existing options; refusing to change the key')
backup = path.with_name('authorized_keys.before-kibana')
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as output:
    output.write(original)
lines[index] = NEW + lines[index][len(OLD):]
fd, temporary = tempfile.mkstemp(prefix='.authorized_keys.', dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(''.join(lines))
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print('Kibana loopback forwarding enabled; previous authorized_keys backed up')
