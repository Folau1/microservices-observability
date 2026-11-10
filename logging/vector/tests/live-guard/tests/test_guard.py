import copy
import importlib.util
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

GUARD_PATH = pathlib.Path(__file__).resolve().parents[1] / 'guard.py'
spec = importlib.util.spec_from_file_location('logging_live_guard', GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
TOKEN = 'a' * 32
LABEL = 'codex-vector-cut-' + TOKEN
SRC = '10.42.0.76'
DST = '10.42.0.86'


def pod(app='vector', ip=SRC, uid='vector-uid', phase='Running', ready='True'):
    return {'metadata': {'name': app + '-pod', 'uid': uid, 'labels': {'app': app}},
            'status': {'phase': phase, 'podIP': ip,
                       'conditions': [{'type': 'Ready', 'status': ready}]}}


class RuleTests(unittest.TestCase):
    def test_rule_restricts_source_dest_port_and_marker(self):
        args = guard.rule_spec(SRC, DST, TOKEN)
        self.assertIsInstance(args, (list, tuple))
        joined = ' '.join(args)
        for item in (SRC, DST, LABEL, 'tcp', '9200', 'REJECT'):
            self.assertIn(item, joined)
        self.assertIn('-s', args)
        self.assertIn('-d', args)
        self.assertIn('--dport', args)

    def test_same_source_and_sink_rejected(self):
        with self.assertRaises((ValueError, RuntimeError)):
            guard.rule_spec(SRC, SRC, TOKEN)

    def test_invalid_ip_and_token_are_rejected(self):
        for bad in ('', '10.42.0.1; id', '0.0.0.0/0', '127.0.0.1\n-j ACCEPT',
                    '2001:db8::1', '999.42.0.2'):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, RuntimeError)):
                    guard.rule_spec(bad, DST, TOKEN)
                with self.assertRaises((ValueError, RuntimeError)):
                    guard.rule_spec(SRC, bad, TOKEN)
        for bad in ('', 'bad; iptables -F', 'x\n-j ACCEPT', 'a' * 500):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, RuntimeError)):
                    guard.rule_spec(SRC, DST, bad)

    def test_parser_accepts_only_one_exact_counter(self):
        line = f' 12 1000 REJECT tcp -- * * {SRC} {DST} tcp dpt:9200 /* {LABEL} */ reject-with tcp-reset'
        self.assertEqual(12, guard.parse_packets(line, TOKEN))
        for bad in ('', line + '\n' + line,
                    line.replace('12 1000', 'oops 1000'),
                    line.replace(LABEL, LABEL + '-extra')):
            with self.subTest(text=bad):
                with self.assertRaises((ValueError, RuntimeError)):
                    guard.parse_packets(bad, TOKEN)


class SchedulerTests(unittest.TestCase):
    def test_insert_requires_open_marker_active_timer_and_more_than_30_seconds(self):
        self.assertTrue(guard.can_apply(False, 131.0, 100.0, True))
        for closed, deadline, now, active in (
            (True, 131, 100, True), (False, 130, 100, True),
            (False, 129, 100, True), (False, 100, 100, True),
            (False, 131, 100, False)):
            with self.subTest(closed=closed, deadline=deadline, active=active):
                self.assertFalse(guard.can_apply(closed, deadline, now, active))


class PreflightTests(unittest.TestCase):
    def test_systemctl_bus_error_is_not_treated_as_inactive(self):
        failed = SimpleNamespace(returncode=1, stdout='', stderr='Failed to connect to bus')
        with mock.patch.object(guard, 'cmd', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'systemd|systemctl|bus|unknown|persistence'):
                guard.assert_safe_persistence()


class PodTests(unittest.TestCase):
    def test_only_one_matching_running_ready_pod_allowed(self):
        chosen = pod()
        self.assertEqual(chosen, guard.get_ready_pod([chosen, pod('kibana', DST, 'other')], 'vector'))

    def test_unready_terminating_ambiguous_invalid_ip_or_uid_rejected(self):
        not_ready = pod(ready='False')
        pending = pod(phase='Pending')
        terminating = pod(); terminating['metadata']['deletionTimestamp'] = '2026-11-10T00:00:00Z'
        no_uid = pod(uid='')
        ipv6 = pod(ip='2001:db8::1')
        invalid_ip = pod(ip='10.1.2.3; echo bad')
        for items in ([], [not_ready], [pending], [terminating], [no_uid], [ipv6], [invalid_ip],
                      [pod(), pod(uid='another-uid')], [pod(), terminating]):
            with self.subTest(items=items):
                with self.assertRaises((ValueError, RuntimeError)):
                    guard.get_ready_pod(copy.deepcopy(items), 'vector')


if __name__ == '__main__':
    unittest.main()
