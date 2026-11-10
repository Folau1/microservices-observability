import os
import subprocess
import sys
import tempfile
import unittest

try:
    import fcntl
except ImportError:
    fcntl = None


@unittest.skipIf(fcntl is None, 'requires Linux flock')
class FlockSanity(unittest.TestCase):
    def test_another_process_cannot_acquire_held_lock(self):
        with tempfile.TemporaryDirectory(prefix='guard-flock-test-') as directory:
            path = os.path.join(directory, 'lock')
            with open(path, 'a+') as first:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                code = 'import fcntl,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
                locked = subprocess.run([sys.executable, '-c', code, path], capture_output=True, timeout=5)
                self.assertNotEqual(0, locked.returncode)
                fcntl.flock(first, fcntl.LOCK_UN)
                unlocked = subprocess.run([sys.executable, '-c', code, path], capture_output=True, timeout=5)
                self.assertEqual(0, unlocked.returncode, unlocked.stderr.decode())


if __name__ == '__main__':
    unittest.main()
