#!/usr/bin/env python3
"""Opt-in test interrupting gateway and Prometheus access to Uploader.

Real Uploader API requests through the gateway will fail temporarily.
This modifies a live firewall. Do not run before independent review.
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Reuse the tested firewall guard; do not keep a second copy in the repository.
_guard_path = Path(__file__).resolve().parents[3] / 'logging/vector/tests/live-guard/guard.py'
_spec = importlib.util.spec_from_file_location('logging_live_guard', _guard_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError('Cannot load the existing firewall guard')
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

IPT = '/usr/sbin/iptables'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_pod(ns, name, label_key='app'):
    items = json.loads(guard.kube('-n', ns, 'get', 'pods', '-o', 'json').stdout)['items']
    selected = [p for p in items if p.get('metadata', {}).get('labels', {}).get(label_key) == name]
    if len(selected) != 1:
        raise RuntimeError('unexpected pod count: ' + name)
    # get_ready_pod validates identity, readiness, and IPv4. Traefik uses a
    # different label key, so a shallow copy is used for that validation only.
    candidate = dict(selected[0])
    candidate['metadata'] = dict(candidate['metadata'])
    candidate['metadata']['labels'] = dict(candidate['metadata'].get('labels', {}), app=name)
    p = guard.get_ready_pod([candidate], name)
    if p['spec'].get('hostNetwork'):
        raise RuntimeError('host-network pods are not supported')
    return p


def snapshots():
    a = get_pod('observability', 'prometheus')
    b = get_pod('microservices', 'uploader')
    c = get_pod('kube-system', 'traefik', 'app.kubernetes.io/name')
    if any(p['spec']['nodeName'] != 'node2' for p in (a, b, c)):
        raise RuntimeError('unexpected pod placement')
    hpas = json.loads(guard.kube('-n', 'microservices', 'get', 'hpa', '-o', 'json').stdout)
    if any(x.get('spec', {}).get('scaleTargetRef', {}).get('name') == 'uploader' for x in hpas['items']):
        raise RuntimeError('Uploader has an HPA')
    return a, b, c


def same_pods(original):
    for old, new in zip(original, snapshots()):
        if (old['metadata']['uid'], old['status']['podIP']) != (new['metadata']['uid'], new['status']['podIP']):
            raise RuntimeError('pod identity changed')


def request(path):
    try:
        with OPENER.open('http://127.0.0.1' + path, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def app_status():
    if request('/') != 200:
        raise RuntimeError('frontend failed')
    code = request('/api/uploader/files')
    if code not in (401, 502, 503, 504):
        raise RuntimeError('unexpected Uploader HTTP status: ' + str(code))
    return code


def api(url):
    with OPENER.open(url, timeout=5) as response:
        value = json.load(response)
    if value.get('status') != 'success':
        raise RuntimeError('Prometheus API did not report success')
    return value['data']


def target(base, pod):
    targets = api(base + '/api/v1/targets')['activeTargets']
    matches = [t for t in targets if t.get('labels', {}).get('job') == 'apps'
               and t.get('labels', {}).get('service') == 'uploader']
    if len(matches) != 1 or matches[0]['labels'].get('pod') != pod['metadata']['name']:
        raise RuntimeError('expected exactly one Uploader target')
    address = matches[0].get('discoveredLabels', {}).get('__address__', '')
    if address != pod['status']['podIP'] + ':8000':
        raise RuntimeError('target address does not match intended Pod IP and port')
    return matches[0]['health']


def alert(base):
    matches = [a for a in api(base + '/api/v1/alerts')['alerts']
               if a.get('labels', {}).get('alertname') == 'AppServiceDown'
               and a.get('labels', {}).get('service') == 'uploader']
    if len(matches) > 1:
        raise RuntimeError('ambiguous alert states')
    return matches[0]['state'] if matches else None


def rule(source, dest, token):
    spec = guard.rule_spec(source, dest, token)
    index = spec.index('9200')
    spec[index] = '8000'
    return spec


def experiment():
    if os.geteuid() != 0 or guard.fcntl is None:
        raise RuntimeError('requires root and Linux')
    guard.assert_safe_persistence()
    swap = guard.resource_guard()
    guard.node_guard()
    pods = snapshots()
    prometheus, uploader, traefik = pods
    service = json.loads(guard.kube('-n', 'observability', 'get', 'svc', 'prometheus', '-o', 'json').stdout)
    ip = service['spec']['clusterIP']
    base = 'http://' + ip + ':9090'
    if target(base, uploader) != 'up' or alert(base) is not None:
        raise RuntimeError('Uploader target is not initially up or alert is active')
    if app_status() != 401:
        raise RuntimeError('Uploader API is not initially available')

    rules = []
    for label, source in (('prometheus', prometheus), ('traefik', traefik)):
        token = secrets.token_hex(16)
        if any(r['token'] == token for r in rules):
            raise RuntimeError('duplicate operation token')
        rule_args = rule(source['status']['podIP'], uploader['status']['podIP'], token)
        state = Path('/run') / ('codex-vector-cut-' + token)
        state.mkdir(mode=0o700)
        cleanup = state / 'cleanup.sh'
        cleanup.write_text(guard.cleanup_script(state, IPT, rule_args), encoding='utf-8')
        cleanup.chmod(0o700)
        rules.append({'name': label, 'token': token, 'args': rule_args, 'cleanup': cleanup,
                      'state': state, 'unit': 'codex-vector-cut-' + token})
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    def interrupted(n, _frame):
        raise InterruptedError('signal ' + str(n))
    for sig in handlers:
        signal.signal(sig, interrupted)
    cleanup_ok = True
    try:
        start = time.monotonic()
        # Both independent timers exist before either live rule is inserted.
        for r in rules:
            guard.cmd(['systemd-run', '--unit=' + r['unit'], '--on-active=180s',
                       '--timer-property=AccuracySec=1s', '--property=Restart=on-failure',
                       '--property=RestartSec=5s', '--property=StartLimitIntervalSec=0',
                       '--property=TimeoutStartSec=30s', '/bin/sh', str(r['cleanup'])])
        for r in rules:
            guard.guarded_insert(r['state'], start + 180, r['unit'] + '.timer', IPT, r['args'])
        fired = False
        while time.monotonic() - start < 120:
            guard.resource_guard(swap)
            guard.node_guard()
            same_pods(pods)
            code = app_status()
            listing = guard.cmd([IPT, '-w', '5', '-L', 'FORWARD', '-n', '-v', '-x']).stdout
            packets = {r['name']: guard.parse_packets(listing, r['token']) for r in rules}
            if code in (502, 503, 504) and all(n > 0 for n in packets.values()) and target(base, uploader) == 'down' and alert(base) == 'firing':
                same_pods(pods)
                for r in rules:
                    guard.cmd([IPT, '-w', '5', '-C', 'FORWARD', *r['args']])
                fired = True
                print(f'FIRING: Uploader gateway HTTP {code}, metrics down, AppServiceDown firing', flush=True)
                break
            time.sleep(3)
        if not fired:
            raise RuntimeError('alert did not fire before the deadline')
        for r in rules:
            guard.cmd(['/bin/sh', str(r['cleanup'])], timeout=25)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            guard.resource_guard(swap)
            guard.node_guard()
            same_pods(pods)
            code = app_status()
            if code == 401 and target(base, uploader) == 'up' and alert(base) is None:
                print('RECOVERED: target up and alert cleared', flush=True)
                return
            time.sleep(3)
        raise RuntimeError('target or alert did not recover by the deadline')
    finally:
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            for r in rules:
                try:
                    result = guard.cmd(['/bin/sh', str(r['cleanup'])], timeout=25, check=False)
                    if result.returncode == 0:
                        guard.cmd(['systemctl', 'stop', r['unit'] + '.timer'], check=False)
                    else:
                        cleanup_ok = False
                        print('MANUAL ACTION: cleanup failed for ' + r['name'] + '; fallback retained', file=sys.stderr)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_ok = False
                    print('MANUAL ACTION: cleanup failed for ' + r['name'] + '; fallback retained: ' + str(exc), file=sys.stderr)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if not cleanup_ok:
            raise RuntimeError('firewall cleanup not confirmed')


if __name__ == '__main__':
    raise SystemExit('NOT ENABLED: independent review and explicit invocation required')
