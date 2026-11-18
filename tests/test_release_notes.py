"""Release validation uses temporary source/changelog fixtures; no GitHub access."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_notes", ROOT / "packaging/release_notes.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseNotesTests(unittest.TestCase):
    def fixture(self, root, version="1.0.0", changelog=None):
        for filename in ("settings.py", "install.py"):
            (root / filename).write_text(f'VERSION = "{version}"\n')
        (root / "docs").mkdir()
        (root / "docs/CHANGELOG.md").write_text(changelog or
            "# Changelog\n\n## 1.0.0 — Initial public release\n\n### Features\n\n- Guided setup.\n")

    def test_version_tag_formats(self):
        for tag in ("v1.0.0", "1.0.0", "v2.10.3"):
            self.assertEqual(release.tag_version(tag), tag[1:] if tag.startswith("v") else tag)
        for tag in ("v1", "v1.0", "v01.0.0", "v1.0.0-rc1", "v1.0.0+build", "other",
                    "v1.0.0\nvalue=evil", "v1.0.0; command", "../v1.0.0"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.tag_version(tag)

    def test_literal_versions_only_no_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.py"
            path.write_text('raise RuntimeError("must not execute")\nVERSION = "1.0.0"\n')
            self.assertEqual(release.read_version(path), "1.0.0")
            for content in ('VERSION = str(1)\n', 'VERSION = "1.0.0"\nVERSION = "2.0.0"\n', 'NAME = "missing"\n'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    release.read_version(path)

    def test_matching_entry_only_preserves_changes(self):
        changelog = ("# Changelog\n\n## Unreleased\n\n- Not shipped.\n\n"
                     "## 1.1.0 — Update\n\n### Added\n\n- New feature.\n\n"
                     "## 1.0.0 — First release\n\n- Old feature.\n")
        section = release.changelog_entry(changelog, "1.1.0")
        self.assertIn("### Added", section)
        self.assertIn("New feature", section)
        self.assertNotIn("Old feature", section)
        self.assertNotIn("Not shipped", section)

    def test_missing_duplicate_and_empty_changes_fail(self):
        for changelog in ("# Changelog\n", "## 1.0.0\n\n", "## 1.0.0\n### Features\n",
                          "## 1.0.0\n<!-- TODO -->\n", "## 1.0.0\n- A\n## 1.0.0\n- B\n"):
            with self.subTest(changelog=changelog), self.assertRaises(ValueError):
                release.changelog_entry(changelog, "1.0.0")

    def test_bracketed_and_plain_version_headings(self):
        for heading in ("## [1.0.0] - 2026-11-18", "## 1.0.0", "## v1.0.0 — First release"):
            self.assertEqual(release.changelog_entry(heading + "\n- Changes\n", "1.0.0"), "- Changes")

    def test_tag_and_each_source_version_must_agree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "settings.py"):
                release.release_notes("v2.0.0", root)
            (root / "install.py").write_text('VERSION = "2.0.0"\n')
            with self.assertRaisesRegex(ValueError, "install.py"):
                release.release_notes("v1.0.0", root)

    def test_public_notes_include_changes_assets_and_install_instructions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            version, body = release.release_notes("v1.0.0", root)
            self.assertEqual(version, "1.0.0")
            for value in ("Guided setup", "sync-webdav-desktop_1.0.0_all.deb",
                          "sync-webdav-desktop-1.0.0.tar.gz", "sync-webdav-desktop-1.0.0-1.noarch.rpm",
                          "sudo dnf install", "sudo zypper install", "SHA256SUMS", "sudo apt install",
                          "/blob/v1.0.0/README.md", "not affiliated"):
                self.assertIn(value, body)

    def test_cli_outputs_are_validated_single_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "notes.md"
            github_output = Path(temp) / "github-output"
            version = release.read_version(ROOT / "install.py")
            tag = "v" + version
            self.assertEqual(release.main(["--tag", tag, "--output", str(output),
                                           "--github-output", str(github_output)]), 0)
            self.assertEqual(github_output.read_text(), f"version={version}\ntag={tag}\n")
            changes = release.changelog_entry((ROOT / "docs/CHANGELOG.md").read_text(), version)
            self.assertIn(changes, output.read_text())


if __name__ == "__main__":
    unittest.main()
