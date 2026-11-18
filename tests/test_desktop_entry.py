"""Round trip through GLib's real desktop-entry parser and launcher."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import backend
import install


@unittest.skipUnless(shutil.which('gio'), 'GLib gio CLI is unavailable')
class DesktopEntryTests(unittest.TestCase):
    def test_installer_and_autostart_preserve_special_path_characters(self):
        shim = """#!/usr/bin/python3
import json, os, sys
from pathlib import Path
dest = Path(os.environ["DESKTOP_TEST_ARGV"])
tmp = dest.with_suffix(".tmp")
tmp.write_text(json.dumps(sys.argv))
tmp.replace(dest)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program_dir = root / 'spaces %f %% $d "quotes" `ticks` \\ backslashes'
            program_dir.mkdir()
            app = program_dir / 'app.py'
            app.write_text(shim)
            desktop_file = root / 'entry.desktop'
            with patch.object(backend, 'APP_DIR', program_dir):
                entries = [(install.desktop_entry(app), [str(app)]),
                           (install.desktop_entry(app, tray=True), [str(app), '--tray']),
                           (backend.desktop_entry(tray=True), [str(app), '--tray'])]
            for i, (entry, expected) in enumerate(entries):
                with self.subTest(expected=expected):
                    dest = root / f'argv-{i}.json'
                    desktop_file.write_text(entry)
                    result = subprocess.run(['gio', 'launch', str(desktop_file)], capture_output=True,
                                            text=True, timeout=5,
                                            env={**os.environ, 'DESKTOP_TEST_ARGV': str(dest)})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    deadline = time.monotonic() + 2
                    while not dest.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(dest.is_file(), result.stderr)
                    self.assertEqual(json.loads(dest.read_text()), expected)
