#!/usr/bin/env python3
"""Limit a dedicated existing SSH key to loopback monitoring destinations."""
import argparse
import os
import pathlib
import stat
import tempfile

OPTIONS = ('restrict,port-forwarding,permitopen="127.0.0.1:13000",'
           'permitopen="127.0.0.1:19090",permitlisten="127.0.0.1:13000",'
           'permitlisten="127.0.0.1:19090"')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("public_key", help="Path to the dedicated public key")
    args = parser.parse_args()
    public = pathlib.Path(args.public_key).read_text(encoding="utf-8").strip().split()
    if len(public) < 2 or public[0] != "ssh-ed25519":
        raise SystemExit("Expected an ED25519 public key")
    path = pathlib.Path.home() / ".ssh" / "authorized_keys"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit("authorized_keys must be a regular file owned by this user")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if f"ssh-ed25519 {public[1]}" in line]
    if len(matches) != 1:
        raise SystemExit("Expected exactly one matching SSH key")
    index = matches[0]
    line = lines[index]
    if line.startswith(OPTIONS + " "):
        print("Restricted monitoring forwarding already configured")
        return
    if not line.startswith("restrict ssh-ed25519 "):
        raise SystemExit("Unexpected existing options; refusing to change the key")
    backup = path.with_name("authorized_keys.before-monitoring")
    with backup.open("x", encoding="utf-8") as file:
        os.chmod(backup, 0o600)
        file.write(original)
    lines[index] = OPTIONS + line[len("restrict"):]
    fd, temporary = tempfile.mkstemp(prefix=".authorized_keys.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write("".join(lines))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Restricted forwarding enabled; previous authorized_keys saved privately")


if __name__ == "__main__":
    main()
