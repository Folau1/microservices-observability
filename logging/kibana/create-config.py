#!/usr/bin/env python3
"""Create one private Kibana config; never print the token or keys."""
import argparse
import json
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--token-file", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
token = args.token_file.read_text(encoding="utf-8").strip()
if not token or any(c.isspace() for c in token):
    raise SystemExit("Token file is empty or contains whitespace")

settings = {
    "server.host": "0.0.0.0",
    "server.port": 5601,
    "elasticsearch.hosts": ["https://elasticsearch.logging.svc:9200"],
    "elasticsearch.ssl.certificateAuthorities": ["/usr/share/kibana/config/es-ca/ca.crt"],
    "elasticsearch.ssl.verificationMode": "full",
    "elasticsearch.serviceAccountToken": token,
    "xpack.security.encryptionKey": secrets.token_urlsafe(48),
    "xpack.encryptedSavedObjects.encryptionKey": secrets.token_urlsafe(48),
    "xpack.reporting.encryptionKey": secrets.token_urlsafe(48),
}
# JSON scalar/array syntax is a valid subset of YAML and quotes token safely.
config = "".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items())
fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(config)
print("Created private config; back it up securely without printing it.")
