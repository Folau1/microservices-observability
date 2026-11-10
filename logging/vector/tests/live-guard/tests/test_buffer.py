import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

GUARD_PATH = Path(__file__).resolve().parents[1] / 'guard.py'
spec = importlib.util.spec_from_file_location('guard_buffer_test', GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
UUID = '7d236c3a-42a6-4a3f-981e-c09b028d2b4a'


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'requires Linux O_NOFOLLOW')
class BufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard-buffer-mock-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.patch = mock.patch.object(guard, 'Path', return_value=self.directory)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_request_id_spanning_64k_chunk_boundary(self):
        path = self.directory / 'buffer-data-1.dat'
        path.write_bytes(b'prefix')
        positions = guard.buffer_positions()
        with path.open('ab') as stream:
            stream.write(b'x' * (65536 - 12) + UUID.encode('ascii') + b'end')
        self.assertTrue(guard.buffer_has_request(UUID, positions))
        self.assertFalse(guard.buffer_has_request('5cbe326e-8e96-4c2e-b852-a2ca23fc0876', positions))

    def test_old_uuid_ignored_new_uuid_found_and_new_file_supported(self):
        path = self.directory / 'buffer-data-1.dat'
        path.write_bytes(UUID.encode('ascii'))
        positions = guard.buffer_positions()
        self.assertFalse(guard.buffer_has_request(UUID, positions))
        (self.directory / 'buffer-data-2.dat').write_bytes(UUID.encode('ascii'))
        self.assertTrue(guard.buffer_has_request(UUID, positions))

    def test_shrink_and_inode_change_rejected(self):
        path = self.directory / 'buffer-data-1.dat'
        path.write_bytes(b'abcdef')
        positions = guard.buffer_positions()
        path.write_bytes(b'a')
        with self.assertRaisesRegex(RuntimeError, 'rotated or shrank'): guard.buffer_has_request(UUID, positions)
        path.rename(self.directory / 'old-data')
        path.write_bytes(b'abcdefghi')
        with self.assertRaisesRegex(RuntimeError, 'rotated or shrank'): guard.buffer_has_request(UUID, positions)

    def test_new_data_over_four_mib_rejected(self):
        path = self.directory / 'buffer-data-1.dat'
        path.write_bytes(b'')
        positions = guard.buffer_positions()
        path.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(RuntimeError, 'budget'): guard.buffer_has_request(UUID, positions)

    def test_time_budget_exceeded_aborts(self):
        path = self.directory / 'buffer-data-1.dat'
        path.write_bytes(b'')
        positions = guard.buffer_positions()
        path.write_bytes(b'new data')
        with mock.patch.object(guard.time, 'monotonic', side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(RuntimeError, 'time budget'):
                guard.buffer_has_request(UUID, positions)

    def test_rejects_invalid_uuid_and_symlinked_data_file(self):
        with self.assertRaises(ValueError): guard.buffer_has_request('not-a-uuid', {})
        original = self.directory / 'another-file'
        original.write_bytes(UUID.encode('ascii'))
        (self.directory / 'buffer-data-1.dat').symlink_to(original)
        with self.assertRaises(OSError): guard.buffer_positions()
        with self.assertRaises(OSError): guard.buffer_has_request(UUID, {})


if __name__ == '__main__': unittest.main()
