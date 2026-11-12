import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('alert_live_guard', HERE / 'alert_guard.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
g = module.guard


@unittest.skipUnless(os.name == 'posix' and hasattr(signal, 'SIGHUP'), 'requires Linux')
class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.calls = []
        self.inserts = []
        self.fail_timer = None
        self.fail_insert = None
        self.fail_cleanup = None
        self.signal_on_first_insert = False
        self.tokens = ['a' * 32, 'b' * 32]
        self.rules = {t: module.rule(ip, '10.42.0.73', t) for t, ip in zip(self.tokens, ['10.42.0.89', '10.42.0.72'])}
        self.pods = tuple({'metadata': {'uid': n, 'name': n}, 'spec': {'nodeName': 'node2'},
                           'status': {'podIP': ip}}
                          for n, ip in [('prometheus', '10.42.0.89'), ('uploader', '10.42.0.73'), ('traefik', '10.42.0.72')])

        def patch(obj, name, value):
            return self.stack.enter_context(mock.patch.object(obj, name, value))
        def path(*parts):
            if len(parts) == 1 and parts[0] == '/run':
                return self.root
            return Path(*parts)
        def command(args, **kwargs):
            self.calls.append(args)
            if args[0] == 'systemd-run':
                if self.fail_timer is not None and self.fail_timer in ' '.join(args):
                    raise RuntimeError('timer failed')
            if args[0] == '/bin/sh' and self.fail_cleanup and self.fail_cleanup in args[1]:
                if kwargs.get('check', True):
                    raise RuntimeError('cleanup failed')
                return SimpleNamespace(returncode=1, stdout='', stderr='failed')
            if args[0] == '/usr/sbin/iptables' and '-L' in args:
                text = '\n'.join('1 10 REJECT /* codex-vector-cut-' + t + ' */' for t in self.tokens)
                return SimpleNamespace(returncode=0, stdout=text, stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        def insert(state, deadline, timer, ipt, rule_args):
            if self.fail_insert and self.fail_insert in timer:
                raise RuntimeError('insert failed')
            self.inserts.append((timer, rule_args))
            self.calls.append(['INSERT', timer])
            if self.signal_on_first_insert and len(self.inserts) == 1:
                signal.raise_signal(signal.SIGTERM)
        patch(module, 'Path', path)
        patch(g.os, 'geteuid', lambda: 0)
        patch(g, 'fcntl', object())
        patch(g, 'assert_safe_persistence', lambda: None)
        patch(g, 'resource_guard', lambda *_a: 0)
        patch(g, 'node_guard', lambda: None)
        patch(module, 'snapshots', lambda: self.pods)
        patch(module, 'same_pods', lambda *_a: None)
        patch(g, 'kube', lambda *_a: SimpleNamespace(stdout=json.dumps({'spec': {'clusterIP': '10.43.1.1'}})))
        patch(g, 'cleanup_script', lambda *_a: '#!/bin/sh\nexit 0\n')
        patch(g, 'guarded_insert', insert)
        patch(g, 'cmd', command)
        self.stack.enter_context(mock.patch.object(module.secrets, 'token_hex', side_effect=self.tokens))

    def configure_success(self, statuses=(401, 502, 502, 401)):
        self.stack.enter_context(mock.patch.object(module, 'app_status', side_effect=statuses))
        self.stack.enter_context(mock.patch.object(module, 'target', side_effect=['up', 'down', 'up']))
        self.stack.enter_context(mock.patch.object(module, 'alert', side_effect=[None, 'firing', None]))
        self.stack.enter_context(mock.patch.object(module.time, 'sleep', return_value=None))

    def test_full_cycle_waits_for_http_recovery_and_stops_both_timers(self):
        self.configure_success()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            module.experiment()
        self.assertIn('FIRING:', out.getvalue())
        self.assertIn('RECOVERED:', out.getvalue())
        self.assertEqual(len(self.inserts), 2)
        starts = [i for i, c in enumerate(self.calls) if c[0] == 'systemd-run']
        self.assertEqual(len(starts), 2)
        self.assertLess(max(starts), min(i for i, c in enumerate(self.calls) if c[0] == 'INSERT'))
        self.assertEqual(len([c for c in self.calls if c[:2] == ['systemctl', 'stop']]), 2)
        self.assertEqual(len([c for c in self.calls if c[0] == '/bin/sh']), 4)

    def test_failed_second_timer_inserts_no_rules(self):
        self.fail_timer = self.tokens[1]
        self.configure_success()
        with self.assertRaisesRegex(RuntimeError, 'timer failed'):
            module.experiment()
        self.assertEqual(self.inserts, [])

    def test_failed_second_insertion_cleans_both_rules(self):
        self.fail_insert = self.tokens[1]
        self.configure_success()
        with self.assertRaisesRegex(RuntimeError, 'insert failed'):
            module.experiment()
        self.assertEqual(len(self.inserts), 1)
        self.assertEqual(len([c for c in self.calls if c[0] == '/bin/sh']), 2)

    def test_failed_cleanup_keeps_its_fallback_but_cleans_other(self):
        self.fail_cleanup = self.tokens[0]
        self.configure_success()
        with self.assertRaisesRegex(RuntimeError, 'firewall cleanup not confirmed'):
            module.experiment()
        stops = [' '.join(c) for c in self.calls if c[:2] == ['systemctl', 'stop']]
        self.assertEqual(len(stops), 1)
        self.assertIn(self.tokens[1], stops[0])

    def test_sigterm_during_partial_insertion_cleans_both(self):
        self.signal_on_first_insert = True
        self.configure_success()
        with self.assertRaises(InterruptedError):
            module.experiment()
        self.assertEqual(len(self.inserts), 1)
        self.assertEqual(len([c for c in self.calls if c[0] == '/bin/sh']), 2)


class Basic(unittest.TestCase):
    def test_rule_limits_source_destination_and_port(self):
        r = module.rule('10.42.0.89', '10.42.0.73', 'a' * 32)
        self.assertIn('10.42.0.89', r)
        self.assertIn('10.42.0.73', r)
        self.assertIn('8000', r)
        self.assertNotIn('9200', r)


if __name__ == '__main__':
    unittest.main()
