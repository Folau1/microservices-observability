#!/usr/bin/env python3
"""Create an Elasticsearch Kibana service token without printing credentials.

Requires a local port-forward to the ES ClusterIP Service. The CA and server
name are still verified against the real service FQDN, not against localhost.
"""
import argparse
import base64
import http.client
import json
import os
import socket
import ssl
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--password-file", type=Path, required=True)
parser.add_argument("--ca-file", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--port", type=int, default=19200)
parser.add_argument("--name", default="kibana-logging")
args = parser.parse_args()
if not args.name or any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in args.name) or args.name.startswith("_"):
    raise SystemExit("Invalid token name")
password = args.password_file.read_text(encoding="utf-8").strip()
if not password or "\n" in password:
    raise SystemExit("Invalid password file")
auth = base64.b64encode(("elastic:" + password).encode()).decode("ascii")
fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
completed = False
try:
    context = ssl.create_default_context(cafile=str(args.ca_file))
    conn = http.client.HTTPSConnection("elasticsearch.logging.svc", args.port, context=context, timeout=20)
    # Connect to the SSH/kubectl tunnel while verifying the service DNS SAN.
    conn._create_connection = lambda address, timeout=None, source_address=None: socket.create_connection(("127.0.0.1", args.port), timeout, source_address)
    conn.request("POST", f"/_security/service/elastic/kibana/credential/token/{args.name}", headers={"Authorization": "Basic " + auth})
    response = conn.getresponse()
    if response.status != 200:
        raise RuntimeError(f"Elasticsearch returned HTTP {response.status}; token was not stored locally")
    result = json.loads(response.read())
    token = result["token"]["value"]
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
        fd = -1
        out.write(token + "\n")
    completed = True
    print("Created service token in private file; do not print it.")
finally:
    if fd >= 0:
        os.close(fd)
    if not completed:
        args.output.unlink(missing_ok=True)
