#!/usr/bin/env python3
"""Prototype: guarded Vector-to-Elasticsearch interruption. Not yet approved for use.

Requires root. A transient systemd timer removes the exact temporary firewall
rule; the lock and closed marker prevent a late insertion after cleanup.
Reboot recovery relies on kernel firewall state not being persisted.
"""
import ipaddress
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import shutil
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None


def rule_spec(source, dest, token):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('invalid token')
    src, dst = ipaddress.ip_address(source), ipaddress.ip_address(dest)
    if src.version != 4 or dst.version != 4 or src == dst or src.is_loopback or dst.is_loopback:
        raise ValueError('unsafe addresses')
    return ['-s', str(src), '-d', str(dst), '-p', 'tcp', '--dport', '9200',
            '-m', 'comment', '--comment', 'codex-vector-cut-' + token,
            '-j', 'REJECT', '--reject-with', 'tcp-reset']


def parse_packets(text, token):
    label = 'codex-vector-cut-' + token
    pattern = re.compile(r'/\*\s*' + re.escape(label) + r'\s*\*/')
    lines = [line for line in text.splitlines() if pattern.search(line)]
    if len(lines) != 1:
        raise ValueError('rule missing or ambiguous')
    value = lines[0].split()[0]
    if not value.isdigit():
        raise ValueError('invalid counter')
    return int(value)


def can_apply(closed, deadline, now, timer_active):
    return not closed and timer_active and deadline - now > 30


def get_ready_pod(items, app):
    matches = [p for p in items if p.get('metadata', {}).get('labels', {}).get('app') == app]
    if len(matches) != 1:
        raise ValueError('expected exactly one active pod: ' + app)
    pod = matches[0]
    if pod.get('metadata', {}).get('deletionTimestamp') or pod.get('status', {}).get('phase') != 'Running' or not any(
        c.get('type') == 'Ready' and c.get('status') == 'True'
        for c in pod.get('status', {}).get('conditions', [])
    ):
        raise ValueError('pod not ready: ' + app)
    if not pod.get('metadata', {}).get('uid') or not pod.get('metadata', {}).get('name'):
        raise ValueError('pod identity missing: ' + app)
    ip = ipaddress.ip_address(pod.get('status', {}).get('podIP', ''))
    if ip.version != 4 or ip.is_loopback:
        raise ValueError('unsafe pod IP')
    return pod


def cmd(args, *, data=None, timeout=15, check=True, pass_fds=()):
    env = dict(os.environ, LC_ALL='C')
    p = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout,
                       env=env, pass_fds=pass_fds)
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} failed: {p.stderr[-300:]}')
    return p


def kube(*args, data=None):
    return cmd(['k3s', 'kubectl', '--request-timeout=10s', *args], data=data)


def snapshot_pods():
    items = json.loads(kube('-n', 'logging', 'get', 'pods', '-o', 'json').stdout)['items']
    return get_ready_pod(items, 'vector'), get_ready_pod(items, 'elasticsearch')


def same_pods(original):
    current = snapshot_pods()
    for before, after in zip(original, current):
        if (before['metadata']['uid'] != after['metadata']['uid'] or
                before['status']['podIP'] != after['status']['podIP']):
            raise RuntimeError('pod identity changed')


def es_count(request_id):
    body = json.dumps({'query': {'term': {'request_id': request_id}}})
    result = kube('-n', 'logging', 'exec', '-i', 'elasticsearch-0', '-c', 'elasticsearch', '--',
                  'curl', '--config', '/usr/share/elasticsearch/config/auth/curl.conf', '-fsS',
                  '--connect-timeout', '3', '--max-time', '8', '-H', 'Content-Type: application/json',
                  '--data-binary', '@-', 'https://127.0.0.1:9200/logs-microservices-default/_count',
                  data=body)
    return json.loads(result.stdout)['count']


def send_event(request_id):
    req = urllib.request.Request('http://127.0.0.1/api/security/verify', data=b'', method='POST',
                                 headers={'Authorization': 'Bearer invalid-vector-test',
                                          'X-Request-ID': request_id})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as res:
            status = res.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status != 401:
        raise RuntimeError('unexpected request status')


def resource_guard(initial_swap=None):
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, _, val = line.partition(':')
        if key in ('MemAvailable', 'SwapFree', 'SwapTotal'):
            mem[key] = int(val.split()[0])
    if set(mem) != {'MemAvailable', 'SwapFree', 'SwapTotal'}:
        raise RuntimeError('cannot read memory')
    used_swap = mem['SwapTotal'] - mem['SwapFree']
    if (mem['MemAvailable'] < 786432 or mem['SwapFree'] < 262144 or
            (initial_swap is not None and used_swap - initial_swap >= 131072)):
        raise RuntimeError('memory guard')
    log_mount = '/var/lib/codex-elasticsearch'
    if not os.path.ismount(log_mount) or os.stat(log_mount).st_dev == os.stat('/').st_dev:
        raise RuntimeError('logging volume is not separately mounted')
    if shutil.disk_usage('/').free < 3 * 1024**3 or shutil.disk_usage(log_mount).free < 1024**3:
        raise RuntimeError('disk guard')
    psi = Path('/proc/pressure/io').read_text()
    match = re.search(r'^full avg10=([0-9.]+)', psi, re.M)
    if not match or float(match.group(1)) >= 30:
        raise RuntimeError('I/O pressure guard')
    return used_swap


def node_guard():
    node = json.loads(kube('get', 'node', 'node2', '-o', 'json').stdout)
    conditions = {item.get('type'): item.get('status') for item in node.get('status', {}).get('conditions', [])}
    if conditions.get('Ready') != 'True' or conditions.get('MemoryPressure') != 'False' or conditions.get('DiskPressure') != 'False':
        raise RuntimeError('node is not ready or under pressure')


def cleanup_script(state_dir, iptables, rule):
    """The timer and foreground use this same serialized cleanup command."""
    state = shlex.quote(str(state_dir))
    binary = shlex.quote(str(iptables))
    args = ' '.join(shlex.quote(s) for s in rule)
    return f'''#!/bin/sh
set -eu
export LC_ALL=C
cd {state}
exec 9>lock
flock -x -w 15 9
: >closed
{binary} -w 5 -S FORWARD >/dev/null
check_rule() {{
    set +e
    output=$({binary} -w 5 -C FORWARD {args} 2>&1)
    status=$?
    set -e
    case "$status" in
        0) return 0 ;;
        1) printf '%s' "$output" | grep -Fq 'Bad rule (does a matching rule exist in that chain?)' || return 2
           return 1 ;;
        *) return 2 ;;
    esac
}}
if check_rule; then
    {binary} -w 5 -D FORWARD {args}
else
    status=$?
    [ "$status" -eq 1 ] || exit 1
fi
if check_rule; then
    echo "ERROR: temporary firewall rule still exists" >&2
    exit 1
else
    status=$?
    [ "$status" -eq 1 ] || exit 1
fi
'''


def guarded_insert(state_dir, deadline, timer_unit, iptables, rule):
    """Inserting and marking cleanup complete share a lock to prevent races."""
    if fcntl is None:
        raise RuntimeError('requires Linux flock')
    with (state_dir / 'lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        active = cmd(['systemctl', 'is-active', '--quiet', timer_unit], check=False).returncode == 0
        if not can_apply((state_dir / 'closed').exists(), deadline, time.monotonic(), active):
            raise RuntimeError('cleanup already ran, timer inactive or deadline too close')
        cmd([iptables, '-w', '5', '-S', 'FORWARD'])
        existing = cmd([iptables, '-w', '5', '-C', 'FORWARD', *rule], check=False)
        if existing.returncode == 0:
            raise RuntimeError('temporary rule already exists')
        if existing.returncode != 1 or 'Bad rule (does a matching rule exist in that chain?)' not in existing.stderr:
            raise RuntimeError('cannot determine whether temporary rule exists')
        # The child retains the same flock if this process is killed during
        # insertion, so cleanup cannot run before a delayed iptables exits.
        cmd([iptables, '-w', '5', '-I', 'FORWARD', '1', *rule], pass_fds=(lock.fileno(),))
        cmd([iptables, '-w', '5', '-C', 'FORWARD', *rule])


def buffer_size():
    folder = Path('/var/lib/codex-elasticsearch/vector/buffer/v2/elasticsearch')
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError('Vector buffer directory missing or symlinked')
    files = list(folder.glob('buffer-data-*.dat'))
    if not files or any(p.is_symlink() or not p.is_file() for p in files):
        raise RuntimeError('Vector buffer data files invalid')
    return sum(p.stat().st_size for p in files)


def buffer_positions():
    folder = Path('/var/lib/codex-elasticsearch/vector/buffer/v2/elasticsearch')
    files = list(folder.glob('buffer-data-*.dat'))
    if not files or len(files) > 1024:
        raise RuntimeError('invalid number of Vector buffer files')
    positions = {}
    for path in files:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('invalid buffer file')
            positions[path.name] = (info.st_ino, info.st_size)
        finally:
            os.close(fd)
    return positions


def buffer_has_request(request_id, positions):
    """Search only new bytes, with strict time/byte budgets and no output."""
    marker = str(uuid.UUID(request_id)).encode('ascii')
    folder = Path('/var/lib/codex-elasticsearch/vector/buffer/v2/elasticsearch')
    remaining_budget = 4 * 1024 * 1024
    deadline = time.monotonic() + 1
    for path in folder.glob('buffer-data-*.dat'):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('invalid buffer file')
            old_inode, old_size = positions.get(path.name, (info.st_ino, 0))
            if old_inode != info.st_ino or info.st_size < old_size:
                raise RuntimeError('Vector buffer rotated or shrank during test')
            unread = info.st_size - old_size
            if unread > remaining_budget:
                raise RuntimeError('buffer read budget exceeded')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                stream.seek(old_size)
                tail = b''
                while unread:
                    if time.monotonic() > deadline:
                        raise RuntimeError('buffer scan exceeded time budget')
                    data = stream.read(min(65536, unread))
                    if not data:
                        raise RuntimeError('buffer changed during scan')
                    unread -= len(data)
                    remaining_budget -= len(data)
                    chunk = tail + data
                    if marker in chunk:
                        return True
                    tail = chunk[-(len(marker)-1):]
        finally:
            os.close(fd)
    return False


def kibana_status(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/login', timeout=4) as response:
        if response.status != 200:
            raise RuntimeError('Kibana login unavailable')


def start_kibana_forward(port):
    return subprocess.Popen(['k3s', 'kubectl', '-n', 'logging', 'port-forward',
                             '--address=127.0.0.1', 'svc/kibana', f'{port}:5601'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def assert_safe_persistence():
    """Conservative preflight: unknown persistence must be reviewed manually."""
    for service in ('netfilter-persistent.service', 'nftables.service', 'ufw.service'):
        result = cmd(['systemctl', 'show', service, '-p', 'LoadState', '-p', 'ActiveState',
                      '-p', 'UnitFileState'])
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if properties.get('LoadState') not in ('loaded', 'not-found') or properties.get('ActiveState') != 'inactive':
            raise RuntimeError('unknown or active firewall persistence: ' + service)
        if properties.get('UnitFileState') not in ('', 'disabled', 'masked'):
            raise RuntimeError('firewall persistence/filter manager enabled: ' + service)
    if Path('/proc/sys/net/bridge/bridge-nf-call-iptables').read_text().strip() != '1':
        raise RuntimeError('bridge netfilter is disabled')
    if not Path('/usr/sbin/iptables').resolve().name.startswith('xtables-nft'):
        raise RuntimeError('unexpected iptables backend')


def experiment(fallback_seconds=120):
    """Proposed live experiment. Keep disabled until independently reviewed."""
    if not 40 <= fallback_seconds <= 120:
        raise ValueError('unsafe fallback duration')
    if os.geteuid() != 0 or fcntl is None:
        raise RuntimeError('root and Linux required')
    assert_safe_persistence()
    initial_swap = resource_guard()
    node_guard()
    pods = snapshot_pods()
    vector, elastic = pods
    if vector['spec']['nodeName'] != elastic['spec']['nodeName']:
        raise RuntimeError('Vector and Elasticsearch are not on the same node')
    token = secrets.token_hex(16)
    request_id = str(uuid.uuid4())
    print('TEST_REQUEST_ID=' + request_id, flush=True)
    rule = rule_spec(vector['status']['podIP'], elastic['status']['podIP'], token)
    binary = '/usr/sbin/iptables'
    state = Path('/run') / ('codex-vector-cut-' + token)
    state.mkdir(mode=0o700)
    cleanup = state / 'cleanup.sh'
    cleanup.write_text(cleanup_script(state, binary, rule), encoding='utf-8')
    cleanup.chmod(0o700)
    unit = 'codex-vector-cut-' + token
    timer = unit + '.timer'
    forward = None
    cleanup_ok = False
    old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    def interrupted(number, _frame):
        raise InterruptedError('interrupted by signal ' + str(number))
    for sig in old_handlers:
        signal.signal(sig, interrupted)
    try:
        # A private forwarding process checks Kibana without changing its policy.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        forward = start_kibana_forward(port)
        deadline = time.monotonic() + 20
        while True:
            try:
                kibana_status(port)
                break
            except (OSError, urllib.error.URLError):
                if forward.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('cannot establish Kibana port-forward')
                time.sleep(1)
        if es_count(request_id) != 0:
            raise RuntimeError('request ID already exists')
        before = buffer_size()
        positions = buffer_positions()
        start = time.monotonic()
        since = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        # If systemd-run races with a signal or times out, the finally block
        # still closes the marker, removes a possible rule, and checks timer.
        cmd(['systemd-run', '--unit=' + unit, '--on-active=' + str(fallback_seconds) + 's',
             '--timer-property=AccuracySec=1s',
             '--property=Restart=on-failure', '--property=RestartSec=5s',
             '--property=StartLimitIntervalSec=0', '--property=TimeoutStartSec=30s',
             '/bin/sh', str(cleanup)])
        guarded_insert(state, start + fallback_seconds, timer, binary, rule)
        same_pods(pods)
        send_event(request_id)
        blocked = False
        retry_seen = False
        while time.monotonic() - start < 55:
            resource_guard(initial_swap)
            node_guard()
            same_pods(pods)
            kibana_status(port)
            packets = parse_packets(cmd([binary, '-w', '5', '-L', 'FORWARD', '-n', '-v', '-x']).stdout, token)
            logs = kube('-n', 'logging', 'logs', vector['metadata']['name'], '--since-time=' + since, '--tail=500').stdout
            retry_seen = retry_seen or bool(re.search(r'retrying after error|connection reset|request timed out', logs, re.I))
            # A retry log may appear only after the sink's request timeout;
            # identify our event in the buffer and absence in ES directly.
            if (packets > 0 and buffer_size() > before and
                    buffer_has_request(request_id, positions) and es_count(request_id) == 0):
                # The independent timer may have fired during the ES request.
                # Recheck pod identities and exact rule before declaring block.
                same_pods(pods)
                cmd([binary, '-w', '5', '-C', 'FORWARD', *rule])
                blocked = True
                print(f'BLOCKED: packets={packets}, test UUID in buffer and absent in ES; retry_seen={retry_seen}', flush=True)
                break
            time.sleep(3)
        if not blocked:
            raise RuntimeError('blocked delivery or disk buffering not confirmed')
        cmd(['/bin/sh', str(cleanup)], timeout=20)
        cleanup_ok = True
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            resource_guard(initial_swap)
            node_guard()
            same_pods(pods)
            kibana_status(port)
            count = es_count(request_id)
            if count > 0:
                print(f'RECOVERED: test event count={count} in Elasticsearch; Kibana login page reachable', flush=True)
                return
            time.sleep(3)
        raise RuntimeError('test event not observed after restore')
    finally:
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            try:
                result = cmd(['/bin/sh', str(cleanup)], timeout=20, check=False)
                cleanup_ok = result.returncode == 0
                if cleanup_ok:
                    cmd(['systemctl', 'stop', timer], check=False)
                else:
                    print('MANUAL ACTION: firewall cleanup failed; fallback timer remains scheduled', file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_ok = False
                print('MANUAL ACTION: firewall cleanup failed; fallback timer remains scheduled: ' + str(exc), file=sys.stderr)
        finally:
            try:
                if forward is not None and forward.poll() is None:
                    forward.terminate()
                    try:
                        forward.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        forward.kill()
                        forward.wait(timeout=5)
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
        if not cleanup_ok:
            raise RuntimeError('temporary firewall cleanup not confirmed')


def main():
    raise SystemExit('PROTOTYPE: orchestration incomplete; do not run')


if __name__ == '__main__':
    main()
