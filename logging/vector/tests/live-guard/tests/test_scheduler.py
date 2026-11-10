import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

GUARD_PATH = Path(__file__).resolve().parents[1] / 'guard.py'
spec = importlib.util.spec_from_file_location('guard_scheduler_test', GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

FAKE_IPTABLES = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
path = pathlib.Path(os.environ['MOCK_RULES'])
args = sys.argv[1:]
op = args[2]
if op == '-S': sys.exit(0)
if op == '-C' and os.environ.get('MOCK_CHECK_ERR') == '1': sys.exit(2)
if op == '-C' and os.environ.get('MOCK_CHECK_ERR') == '2':
    print('permission denied', file=sys.stderr); sys.exit(1)
rules = json.loads(path.read_text())
if op == '-I':
    pathlib.Path(os.environ['MOCK_STARTED']).touch()
    time.sleep(float(os.environ.get('MOCK_DELAY', '0')))
    rule = args[5:]
    rules.append(rule)
    path.write_text(json.dumps(rules))
    sys.exit(0)
rule = args[4:]
if op == '-C':
    if rule in rules: sys.exit(0)
    print('iptables: Bad rule (does a matching rule exist in that chain?)', file=sys.stderr)
    sys.exit(1)
if op == '-D':
    if rule not in rules: sys.exit(1)
    rules.remove(rule)
    path.write_text(json.dumps(rules))
    sys.exit(0)
sys.exit(2)
'''
FAKE_SYSTEMCTL = '#!/bin/sh\n[ "$MOCK_TIMER_ACTIVE" = "1" ]\n'


@unittest.skipIf(guard.fcntl is None, 'requires Linux flock')
class SchedulerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard-scheduler-fake-')
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.iptables = self.bin / 'iptables'; self.iptables.write_text(FAKE_IPTABLES); self.iptables.chmod(0o700)
        self.systemctl = self.bin / 'systemctl'; self.systemctl.write_text(FAKE_SYSTEMCTL); self.systemctl.chmod(0o700)
        self.rules = self.root / 'fake-rules.json'; self.rules.write_text('[]')
        self.started = self.root / 'started'
        self.rule = guard.rule_spec('10.42.0.76', '10.42.0.86', 'a' * 32)
        self.foreign = guard.rule_spec('10.42.0.76', '10.42.0.86', 'b' * 32)
        self.env = {'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                    'MOCK_RULES': str(self.rules), 'MOCK_STARTED': str(self.started),
                    'MOCK_TIMER_ACTIVE': '1', 'MOCK_CHECK_ERR': '0', 'MOCK_DELAY': '0'}
        self.env_patch = mock.patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def insert(self, seconds=80):
        guard.guarded_insert(self.state, time.monotonic() + seconds, 'fake.timer', str(self.iptables), self.rule)

    def cleanup(self):
        script = guard.cleanup_script(self.state, str(self.iptables), self.rule)
        return subprocess.run(['/bin/sh', '-c', script], capture_output=True, timeout=10)

    def test_cleanup_is_idempotent_and_preserves_other_rules(self):
        self.rules.write_text(json.dumps([self.foreign]))
        self.insert()
        self.assertEqual(0, self.cleanup().returncode)
        self.assertEqual(0, self.cleanup().returncode)
        self.assertEqual([self.foreign], json.loads(self.rules.read_text()))
        self.assertTrue((self.state / 'closed').exists())

    def test_cleanup_before_insertion_disallows_late_rule(self):
        self.assertEqual(0, self.cleanup().returncode)
        with self.assertRaises(RuntimeError): self.insert()
        self.assertEqual([], json.loads(self.rules.read_text()))

    def test_inactive_timer_or_expired_deadline_prevents_insertion(self):
        os.environ['MOCK_TIMER_ACTIVE'] = '0'
        with self.assertRaises(RuntimeError): self.insert()
        os.environ['MOCK_TIMER_ACTIVE'] = '1'
        with self.assertRaises(RuntimeError): self.insert(30)
        self.assertEqual([], json.loads(self.rules.read_text()))

    def test_concurrent_cleanup_waits_for_insert_then_removes_exact_rule(self):
        self.rules.write_text(json.dumps([self.foreign]))
        os.environ['MOCK_DELAY'] = '1'
        errors = []
        def run():
            try: self.insert()
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=run); thread.start()
        deadline = time.monotonic() + 5
        while not self.started.exists() and time.monotonic() < deadline: time.sleep(0.02)
        self.assertTrue(self.started.exists())
        cleaned = self.cleanup()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(0, cleaned.returncode, cleaned.stderr.decode())
        self.assertEqual([self.foreign], json.loads(self.rules.read_text()))
        with self.assertRaises(RuntimeError): self.insert()

    def test_sigkill_while_iptables_runs_cannot_leave_late_rule(self):
        os.environ['MOCK_DELAY'] = '2'
        code = ("import importlib.util,sys,time; from pathlib import Path; "
                "s=importlib.util.spec_from_file_location('g',sys.argv[1]); "
                "g=importlib.util.module_from_spec(s); s.loader.exec_module(g); "
                "g.guarded_insert(Path(sys.argv[2]),time.monotonic()+80,'fake.timer',sys.argv[3],"
                "g.rule_spec('10.42.0.76','10.42.0.86','a'*32))")
        worker = subprocess.Popen([sys.executable, '-c', code, str(GUARD_PATH), str(self.state), str(self.iptables)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: worker.kill() if worker.poll() is None else None)
        self.addCleanup(worker.stdout.close)
        self.addCleanup(worker.stderr.close)
        deadline = time.monotonic() + 5
        while not self.started.exists() and time.monotonic() < deadline: time.sleep(0.02)
        if not self.started.exists() and worker.poll() is not None:
            self.fail('worker exited early: ' + worker.stderr.read().decode())
        self.assertTrue(self.started.exists())
        worker.kill(); worker.wait(timeout=5)
        cleaned = self.cleanup()
        self.assertEqual(0, cleaned.returncode, cleaned.stderr.decode())
        time.sleep(2.5)
        self.assertEqual([], json.loads(self.rules.read_text()), 'orphaned iptables inserted after cleanup')

    def test_iptables_check_error_must_not_be_treated_as_absence(self):
        for mode in ('1', '2'):
            with self.subTest(mode=mode):
                os.environ['MOCK_CHECK_ERR'] = mode
                with self.assertRaises(RuntimeError): self.insert()
                self.assertEqual([], json.loads(self.rules.read_text()))
                self.assertNotEqual(0, self.cleanup().returncode)


if __name__ == '__main__': unittest.main()
