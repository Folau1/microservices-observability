#!/usr/bin/env python3
"""Isolated Vector disk-buffer/retry experiment with an artificial ES endpoint."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vector', required=True)
    args = parser.parse_args()
    marker = str(uuid.uuid4())
    received = []
    guard = threading.Lock()
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            lines = body.decode('utf-8').splitlines()
            records = [json.loads(line) for line in lines]
            with guard:
                received.extend(records[1::2])
                requests_seen.append(self.path)
            result = json.dumps({'errors': False, 'items': [
                {'create': {'status': 201}} for _ in records[1::2]
            ]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *_):
            pass

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='vector-buffer-test-') as temp:
        root = Path(temp)
        config = {
            'data_dir': str(root / 'data'),
            'sources': {'test': {'type': 'stdin', 'decoding': {'codec': 'json'}}},
            'sinks': {'es': {
                'type': 'elasticsearch', 'inputs': ['test'],
                'endpoints': [f'http://127.0.0.1:{port}'], 'mode': 'data_stream',
                'data_stream': {'type': 'logs', 'dataset': 'microservices', 'namespace': 'default', 'auto_routing': False},
                'healthcheck': {'enabled': False}, 'compression': 'none',
                'batch': {'timeout_secs': 1},
                'buffer': {'type': 'disk', 'max_size': 536870912, 'when_full': 'block'}
            }}
        }
        (root / 'data').mkdir()
        config_path = root / 'vector.yaml'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        log_path = root / 'vector.log'
        process = None
        server = None
        try:
            with log_path.open('w+', encoding='utf-8') as log:
                def launch():
                    return subprocess.Popen([args.vector, '--config-yaml', str(config_path)],
                                            stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
                process = launch()
                process.stdin.write((json.dumps({'request_id': marker, 'message': 'isolated-buffer-test'}) + '\n').encode())
                process.stdin.flush()
                retry_deadline = time.monotonic() + 45
                while time.monotonic() < retry_deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'first Vector exited: {process.returncode}')
                    log.flush()
                    debug = log_path.read_text(encoding='utf-8', errors='replace')
                    if 'Retrying after error.' in debug:
                        print('Endpoint unavailable; Vector retry observed', flush=True)
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('Vector did not report retry while endpoint was unavailable')
                time.sleep(2)
                # Kill while the endpoint is still unavailable; only the disk buffer can retain the event.
                process.kill()
                process.wait(timeout=10)
                process = launch()
                time.sleep(3)
                if process.poll() is not None:
                    raise RuntimeError(f'restarted Vector exited: {process.returncode}')
                resumed = time.monotonic()
                server = Server(('127.0.0.1', port), Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    with guard:
                        matches = [r for r in received if r.get('request_id') == marker]
                    if matches:
                        print(f'RECOVERED events={len(matches)} delay_s={time.monotonic()-resumed:.2f} paths={requests_seen}', flush=True)
                        break
                    if process.poll() is not None:
                        raise RuntimeError(f'Vector exited while waiting: {process.returncode}')
                    time.sleep(0.5)
                else:
                    raise RuntimeError('event not delivered after restoring artificial endpoint')
        except BaseException:
            if log_path.exists():
                print(log_path.read_text(encoding='utf-8', errors='replace')[-6000:])
            raise
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if server is not None:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    main()
