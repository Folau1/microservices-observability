"""Serve only the frontend and health endpoints, with structured request logs."""

import json
import re
import signal
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INDEX = Path(__file__).with_name("index.html").read_bytes()
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "Frontend"
    sys_version = ""

    def log_message(self, format, *args):
        # Request records are emitted by log_request, never in the default text format.
        pass

    def log_request(self, code="-", size="-"):
        incoming = getattr(self, "headers", {}).get("X-Request-ID", "")
        self.request_id = incoming if REQUEST_ID.fullmatch(incoming) else str(uuid.uuid4())
        print(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "frontend",
            "level": "error" if isinstance(code, int) and code >= 500 else "info",
            "request_id": self.request_id,
            "method": getattr(self, "command", ""),
            "path": getattr(self, "path", "").partition("?")[0],
            "status": code,
        }, ensure_ascii=False), file=sys.stdout, flush=True)

    def send_response(self, code, message=None):
        super().send_response(code, message)
        self.send_header("X-Request-ID", self.request_id)

    def _serve(self, include_body):
        path = self.path.partition("?")[0]
        if path == "/":
            status, body, content_type = 200, INDEX, "text/html; charset=utf-8"
        elif path in ("/healthz", "/readyz"):
            status, body, content_type = 200, b"ok\n", "text/plain; charset=utf-8"
        else:
            status, body, content_type = 404, b"Not found\n", "text/plain; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


if __name__ == "__main__":
    def stop(signum, frame):
        # Raising in the main thread interrupts serve_forever without calling
        # shutdown from that same thread (which would deadlock).
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
