#!/usr/bin/env python3
"""Create a read-only Kibana user. Requires root and local ES/Kibana tunnels."""
import argparse
import base64
import http.client
import json
import os
import secrets
import socket
import ssl
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--secrets-dir", type=Path, default=Path("/var/lib/codex-logging-secrets"))
parser.add_argument("--es-port", type=int, default=19201)
parser.add_argument("--kibana-port", type=int, default=15601)
args = parser.parse_args()
if os.geteuid() != 0:
    raise SystemExit("Run as root on the server.")
secret_dir = args.secrets_dir
password_file = secret_dir / "kibana-reader-password"
username_file = secret_dir / "kibana-reader-username"
if password_file.exists() or username_file.exists():
    raise SystemExit("Reader credential file already exists; refusing to change credentials.")
admin_password = (secret_dir / "password").read_text(encoding="utf-8").strip()
auth = "Basic " + base64.b64encode(("elastic:" + admin_password).encode()).decode("ascii")
tls_context = ssl.create_default_context(cafile=str(secret_dir / "ca.crt"))


def call_es(method, path, body=None):
    conn = http.client.HTTPSConnection("elasticsearch.logging.svc", args.es_port, context=tls_context, timeout=30)
    conn._create_connection = lambda address, timeout=None, source_address=None: socket.create_connection(("127.0.0.1", args.es_port), timeout, source_address)
    payload = None if body is None else json.dumps(body).encode()
    conn.request(method, path, body=payload, headers={"Authorization": auth, "Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else None


def call_kibana(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", args.kibana_port, timeout=30)
    payload = None if body is None else json.dumps(body).encode()
    conn.request(method, path, body=payload, headers={"Authorization": auth, "Content-Type": "application/json", "kbn-xsrf": "true"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else None


def expect(actual, accepted, label):
    if actual not in accepted:
        raise RuntimeError(f"{label}: HTTP {actual}")


try:
    status, _ = call_kibana("GET", "/api/security/role/microservices_log_reader")
    if status != 404:
        raise RuntimeError(f"Role already exists or lookup failed: HTTP {status}")
    status, _ = call_kibana("GET", "/api/data_views/data_view/microservices-logs")
    if status != 404:
        raise RuntimeError(f"Data view already exists or lookup failed: HTTP {status}")
    # Keep the generated password on disk even on a network failure after the
    # request, since the server may have committed the user before disconnect.
    password = secrets.token_urlsafe(32)
    # ES user PUT is create-or-update. A 128-bit random suffix avoids relying
    # on an unsafe GET-before-PUT check of a predictable user name.
    username = "log_reader_" + secrets.token_hex(16)
    fd = os.open(username_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(username + "\n")
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(password + "\n")
    role = {"elasticsearch": {"cluster": [], "indices": [{"names": ["logs-microservices-*"], "privileges": ["read", "view_index_metadata"]}]}, "kibana": [{"base": [], "feature": {"discover_v2": ["read"]}, "spaces": ["default"]}]}
    status, _ = call_kibana("PUT", "/api/security/role/microservices_log_reader?createOnly=true", role)
    expect(status, {200, 204}, "Create role")
    data_view = {"data_view": {"id": "microservices-logs", "title": "logs-microservices-*", "name": "Логи микросервисов", "timeFieldName": "@timestamp", "allowNoIndex": True}}
    status, _ = call_kibana("POST", "/api/data_views/data_view", data_view)
    expect(status, {200}, "Create data view")
    status, result = call_es("PUT", "/_security/user/" + username, {"password": password, "roles": ["microservices_log_reader"], "full_name": "Microservices log reader"})
    expect(status, {200}, "Create user")
    if not result or not result.get("created"):
        raise RuntimeError("User response did not confirm creation; investigate before continuing.")
    print("Reader user, role and data view created; password stored in root-only file.")
except (OSError, ValueError, RuntimeError, KeyError, http.client.HTTPException) as exc:
    print(f"Stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("A partial operation may remain; inspect the user, role, data view and password file before retrying.", file=sys.stderr)
    raise SystemExit(1)
