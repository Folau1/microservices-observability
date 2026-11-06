#!/usr/bin/env python3
"""Exercise the deployed API without printing credentials or writing uploaded data to disk."""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CONTENT = b"Microservices observability smoke test\n"


def request(base, path, *, method="GET", data=None, token=None, content_type=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.1.151")
    parser.add_argument("--username-file", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--file-id", help="Verify a previously uploaded smoke-test object")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    username = Path(args.username_file).read_text(encoding="utf-8").strip()
    password = Path(args.password_file).read_text(encoding="utf-8").strip()

    code, _ = request(base, "/")
    assert code == 200, f"frontend returned HTTP {code}"
    code, _ = request(base, "/api/security/verify", method="POST", token="invalid")
    assert code == 401, f"invalid token returned HTTP {code}"
    body = json.dumps({"username": username, "password": password}).encode()
    code, result = request(base, "/api/security/login", method="POST", data=body, content_type="application/json")
    assert code == 200, f"login returned HTTP {code}"
    token = json.loads(result)["access_token"]
    code, _ = request(base, "/api/uploader/files")
    assert code == 401, f"unauthenticated file list returned HTTP {code}"
    code, _ = request(base, "/api/uploader/files", token="invalid")
    assert code == 401, f"file list with invalid token returned HTTP {code}"

    file_id = args.file_id
    if not file_id:
        boundary = "codex-" + uuid.uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="codex-smoke.txt"\r\n'
                  "Content-Type: text/plain\r\n\r\n").encode()
        payload = prefix + CONTENT + f"\r\n--{boundary}--\r\n".encode()
        code, result = request(base, "/api/uploader/files", method="POST", data=payload, token=token,
                               content_type=f"multipart/form-data; boundary={boundary}")
        assert code == 201, f"upload returned HTTP {code}"
        file_id = json.loads(result)["id"]
    uuid.UUID(file_id)
    code, result = request(base, "/api/uploader/files", token=token)
    assert code == 200, f"file list returned HTTP {code}"
    assert any(item["id"] == file_id for item in json.loads(result)["files"]), "uploaded file is missing from list"
    code, downloaded = request(base, "/api/uploader/files/" + file_id, token=token)
    assert code == 200, f"download returned HTTP {code}"
    assert downloaded == CONTENT, "downloaded content differs"
    print("PASS frontend, login, invalid token, authorization, list and download")
    print("SMOKE_FILE_ID=" + file_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, KeyError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
