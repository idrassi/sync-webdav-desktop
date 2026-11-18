"""Minimum-runtime and dependency-name regression checks; no installations."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from settings import is_within
from setup_engine import distro_family, package_commands

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bootstrap_compatibility', ROOT/'scripts/bootstrap.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class CompatibilityTests(unittest.TestCase):
    def test_lexical_containment_does_not_follow_symlinks(self):
        self.assertTrue(is_within(Path('/home/user/drive'),Path('/home/user')))
        self.assertTrue(is_within(Path('/home/user'),Path('/home/user')))
        self.assertFalse(is_within(Path('/home/username'),Path('/home/user')))
        self.assertFalse(is_within(Path('/elsewhere'),Path('/home/user')))

    def test_old_and_new_polkit_package_names(self):
        with patch.object(bootstrap.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'Package: pkexec\n','')):
            self.assertEqual(bootstrap.authorization_package('apt-get'),'pkexec')
        with patch.object(bootstrap.subprocess,'run',return_value=subprocess.CompletedProcess([],100,'','')):
            self.assertEqual(bootstrap.authorization_package('apt-get'),'policykit-1')
        with patch.object(bootstrap.subprocess,'run') as no_probe:
            self.assertEqual(bootstrap.authorization_package('dnf'),'polkit')
            no_probe.assert_not_called()

    def test_rpm_distribution_detection_and_package_commands(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)/'os-release'
            for ident, like, expected in [('fedora','', 'fedora'), ('rocky','rhel centos fedora','rhel'),
                                          ('centos','rhel fedora','rhel'), ('opensuse-leap','suse opensuse','suse')]:
                release.write_text(f'ID={ident}\nID_LIKE="{like}"\n')
                self.assertEqual(distro_family(release),expected)
        self.assertEqual(package_commands('rhel')[0][0],'/usr/bin/dnf')
        self.assertEqual(package_commands('suse')[0][0],'/usr/bin/zypper')
        self.assertIn('--non-interactive',package_commands('suse')[0])
        self.assertIn('rclone',package_commands('suse')[0])

    def test_universal_package_does_not_pin_host_abi(self):
        control=(ROOT/'packaging/control.in').read_text()
        self.assertIn('Architecture: all',control)
        self.assertIn('python3 (>= 3.8)',control)
        self.assertIn('gir1.2-gtk-3.0 (>= 3.16)',control)
        self.assertIn('pkexec | policykit-1',control)
        self.assertNotIn('libc6',control)


if __name__=='__main__': unittest.main()
