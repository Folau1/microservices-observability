import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import unittest
from unittest import mock

GUARD_PATH = Path(__file__).resolve().parents[1] / 'guard.py'
spec = importlib.util.spec_from_file_location('guard_workflow_test', GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class FakeForward:
    def __init__(self): self.running = True
    def poll(self): return None if self.running else 0
    def terminate(self): self.running = False
    def wait(self, timeout=None): self.running = False; return 0
    def kill(self): self.running = False


@unittest.skipUnless(
    guard.fcntl is not None and hasattr(guard.os, 'geteuid') and hasattr(guard.signal, 'SIGHUP'),
    'requires Linux/POSIX process and signal primitives',
)
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard-workflow-mock-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.cleanup_rc = 0
        self.cleanup_timeout = False
        self.fail_systemd = False
        self.rule_inserted = False
        self.rule_check_failure = ''
        self.forward = FakeForward()
        self.pods = (
            {'metadata': {'name': 'vector-a', 'uid': 'v'}, 'status': {'podIP': '10.42.0.76'}, 'spec': {'nodeName': 'node2'}},
            {'metadata': {'name': 'elasticsearch-0', 'uid': 'e'}, 'status': {'podIP': '10.42.0.86'}, 'spec': {'nodeName': 'node2'}},
        )
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        def patch(name, value): return self.stack.enter_context(mock.patch.object(guard, name, value))
        def fake_path(*parts): return self.root if parts == ('/run',) else Path(*parts)
        def fake_cmd(args, **kwargs):
            self.calls.append(args)
            if args[0] == '/usr/sbin/iptables' and '-C' in args and self.rule_check_failure:
                if self.rule_check_failure == 'timeout': raise subprocess.TimeoutExpired(args, 15)
                raise RuntimeError('temporary rule already removed')
            if args[0] == 'systemd-run' and self.fail_systemd: raise RuntimeError('systemd-run failure')
            if args[0] == '/bin/sh':
                if self.cleanup_timeout: raise subprocess.TimeoutExpired(args, 20)
                if self.cleanup_rc and kwargs.get('check', True): raise RuntimeError('cleanup command failed')
                return SimpleNamespace(returncode=self.cleanup_rc, stdout='', stderr='mock cleanup error')
            return SimpleNamespace(returncode=0, stdout='mock output', stderr='')
        def insert(*args): self.rule_inserted = True
        patch('Path', fake_path)
        patch('cmd', fake_cmd)
        patch('assert_safe_persistence', lambda: None)
        patch('resource_guard', lambda *args: 0)
        patch('node_guard', lambda: None)
        patch('snapshot_pods', lambda: self.pods)
        patch('same_pods', lambda _: None)
        patch('kibana_status', lambda _: None)
        patch('send_event', lambda _: None)
        patch('parse_packets', lambda *_: 1)
        patch('buffer_size', mock.Mock(side_effect=[100, 110]))
        patch('buffer_positions', lambda: {})
        patch('buffer_has_request', lambda *_: True)
        patch('es_count', mock.Mock(side_effect=[0, 0, 1]))
        patch('kube', lambda *a, **kw: SimpleNamespace(stdout='retrying after error'))
        patch('guarded_insert', insert)
        self.stack.enter_context(mock.patch.object(guard.os, 'geteuid', return_value=0))
        self.stack.enter_context(mock.patch.object(guard.subprocess, 'Popen', return_value=self.forward))

    def test_success_removes_rule_then_stops_timer_and_forward(self):
        guard.experiment()
        self.assertTrue(self.rule_inserted)
        self.assertFalse(self.forward.running)
        self.assertTrue(any(c[0] == 'systemd-run' for c in self.calls))
        self.assertTrue(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))
        self.assertGreaterEqual(sum(c[0] == '/bin/sh' for c in self.calls), 2)

    def test_timer_setup_failure_never_inserts_rule(self):
        self.fail_systemd = True
        with self.assertRaisesRegex(RuntimeError, 'systemd-run failure'): guard.experiment()
        self.assertFalse(self.rule_inserted)
        self.assertFalse(self.forward.running)

    def test_fallback_removes_rule_or_rule_check_times_out_before_block_confirmation(self):
        for mode in ('removed', 'timeout'):
            with self.subTest(mode=mode):
                # Reset the one-use mocked sequences for each scenario.
                self.rule_check_failure = mode
                with mock.patch.object(guard, 'buffer_size', side_effect=[100, 110]), \
                     mock.patch.object(guard, 'es_count', side_effect=[0, 0, 1]), \
                     mock.patch.object(guard, 'start_kibana_forward', return_value=self.forward), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                        guard.experiment()
                self.assertNotIn('BLOCKED:', output.getvalue())
                self.assertNotIn('RECOVERED:', output.getvalue())
                self.assertTrue(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))
                self.assertFalse(self.forward.running)
                self.forward = FakeForward()

    def test_buffered_unique_event_counts_as_block_even_without_retry_log(self):
        tick = iter(range(40, 1000, 40))
        no_retry = SimpleNamespace(stdout='Vector: request remains in flight')
        with mock.patch.object(guard, 'kube', return_value=no_retry), \
             mock.patch.object(guard.time, 'monotonic', side_effect=lambda: next(tick)), \
             mock.patch.object(guard.time, 'sleep', return_value=None):
            guard.experiment()
        self.assertTrue(self.rule_inserted)
        self.assertFalse(self.forward.running)
        self.assertTrue(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))

    def test_unrelated_buffer_growth_cannot_prove_test_event_buffered(self):
        with mock.patch.object(guard, 'buffer_has_request', return_value=False), \
             mock.patch.object(guard.time, 'monotonic', side_effect=[80, 100, 101, 156]), \
             mock.patch.object(guard.time, 'sleep', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'buffering not confirmed'):
                guard.experiment()
        self.assertTrue(self.rule_inserted)
        self.assertFalse(self.forward.running)
        self.assertTrue(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))

    def test_cleanup_timeout_still_stops_forward_and_keeps_timer(self):
        self.cleanup_timeout = True
        with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)): guard.experiment()
        self.assertFalse(self.forward.running)
        self.assertFalse(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))

    def test_cleanup_failure_does_not_stop_fallback_timer(self):
        self.cleanup_rc = 1
        with self.assertRaisesRegex(RuntimeError, 'cleanup not confirmed'): guard.experiment()
        self.assertTrue(self.rule_inserted)
        self.assertFalse(self.forward.running)
        self.assertFalse(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))


if __name__ == '__main__': unittest.main()
