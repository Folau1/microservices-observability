#!/usr/bin/env python3
"""Exercise the repository's alert rule in a separate local Prometheus."""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RULES = Path(__file__).resolve().parent.parent / 'prometheus' / 'alerts.yml'
SERVICE = 'controlled-alert-test'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/metrics':
            self.send_error(404)
            return
        content = b'# TYPE isolated_test_metric gauge\nisolated_test_metric 1\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_):
        pass


class MetricsServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_metrics(port):
    server = MetricsServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def get_json(url):
    with OPENER.open(url, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status}')
        result = json.load(response)
    if result.get('status') != 'success':
        raise RuntimeError('Prometheus API returned an error')
    return result['data']


def wait_for(description, predicate, proc, seconds=100):
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f'Prometheus exited ({proc.returncode}) while waiting for {description}')
        try:
            if predicate():
                print(description, flush=True)
                return
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f'timed out waiting for {description}: {last_error or "condition not met"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prometheus', default='prometheus', help='path to a local Prometheus executable')
    args = parser.parse_args()
    executable = shutil.which(args.prometheus)
    if not executable:
        raise SystemExit('Prometheus executable not found; supply --prometheus')
    if not RULES.is_file():
        raise SystemExit(f'Alert rules missing: {RULES}')

    # The live cluster is never contacted. Both servers bind only to loopback.
    metrics = start_metrics(0)
    metrics_port = metrics.server_address[1]
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        prom_port = reservation.getsockname()[1]
    base = f'http://127.0.0.1:{prom_port}'
    marker = 'isolated-' + uuid.uuid4().hex
    query = urllib.parse.urlencode({'query': f'up{{job="apps",service="{SERVICE}",test_run="{marker}"}}'})

    def value():
        result = get_json(base + '/api/v1/query?' + query)['result']
        return result[0]['value'][1] if len(result) == 1 else None

    def alert_state():
        alerts = get_json(base + '/api/v1/alerts')['alerts']
        matches = [a for a in alerts if a['labels'].get('alertname') == 'AppServiceDown'
                   and a['labels'].get('service') == SERVICE]
        if len(matches) > 1:
            raise RuntimeError('multiple matching AppServiceDown alerts')
        return matches[0]['state'] if matches else None

    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix='isolated-prometheus-') as temp:
            temp_path = Path(temp)
            # JSON strings are valid YAML scalars and correctly quote Windows paths.
            config = ('global:\n  scrape_interval: 2s\n  evaluation_interval: 2s\n'
                      '  scrape_timeout: 1s\nrule_files:\n  - ' + json.dumps(str(RULES)) + '\n'
                      'scrape_configs:\n  - job_name: isolated-probe\n    static_configs:\n'
                      f'      - targets: ["127.0.0.1:{metrics_port}"]\n'
                      f'        labels: {{service: "{SERVICE}", test_run: "{marker}"}}\n'
                      '    relabel_configs:\n      - target_label: job\n        replacement: apps\n')
            config_path = temp_path / 'prometheus.yml'
            config_path.write_text(config, encoding='utf-8')
            log_path = temp_path / 'prometheus.log'
            with log_path.open('w+', encoding='utf-8') as log:
                proc = subprocess.Popen([executable, '--config.file=' + str(config_path),
                                         '--storage.tsdb.path=' + str(temp_path / 'data'),
                                         '--web.listen-address=127.0.0.1:' + str(prom_port),
                                         '--log.level=error'], stdout=log, stderr=subprocess.STDOUT)
                try:
                    wait_for('Цель доступна (up=1)', lambda: value() == '1', proc)
                    metrics.shutdown()
                    metrics.server_close()
                    metrics = None
                    wait_for('Цель недоступна (up=0)', lambda: value() == '0', proc)
                    wait_for('Штатный AppServiceDown сработал', lambda: alert_state() == 'firing', proc)
                    metrics = start_metrics(metrics_port)
                    wait_for('Цель восстановлена (up=1)', lambda: value() == '1', proc)
                    wait_for('AppServiceDown снят', lambda: alert_state() is None, proc)
                except BaseException:
                    log.flush()
                    log.seek(0)
                    print('Журнал изолированного Prometheus:', log.read()[-4000:], flush=True)
                    raise
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
    finally:
        if metrics is not None:
            metrics.shutdown()
            metrics.server_close()


if __name__ == '__main__':
    main()
