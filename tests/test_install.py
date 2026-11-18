"""Source-install file writes use temporary paths only."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallWriteTests(unittest.TestCase):
    def test_atomic_write_syncs_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'launcher'
            with patch.object(install.os, 'fsync', wraps=install.os.fsync) as sync:
                install.atomic_write(target, b'launcher contents', 0o755)
            sync.assert_called_once()
            self.assertEqual(target.read_bytes(), b'launcher contents')
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_sync_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'launcher'
            target.write_bytes(b'previous launcher')
            with patch.object(install.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'No space')):
                with self.assertRaises(OSError):
                    install.atomic_write(target, b'new launcher')
            self.assertEqual(target.read_bytes(), b'previous launcher')
            self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == '__main__':
    unittest.main()
