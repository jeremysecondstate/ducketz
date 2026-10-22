"""Filesystem-only CLI resolution; fixture files are never executed."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ml.codex_cli import resolve_codex_executable


class CodexCliResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def binary(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline fixture, never executed\n")
        return path

    def test_existing_configured_cli_retains_precedence_without_path_lookup(self):
        configured = self.binary("configured/codex.exe")
        self.binary("path/codex.exe")

        def lookup(_):
            self.fail("A valid explicit local binding must retain precedence")

        self.assertEqual(resolve_codex_executable(configured, which=lookup), str(configured))

    def test_current_path_cli_survives_a_removed_or_unusable_local_binding(self):
        current = self.binary("current-install/codex.exe")
        before = current.read_bytes()
        for kind in ("removed", "directory", "unset"):
            with self.subTest(configured=kind):
                configured = self.root / kind / "codex.exe"
                if kind == "directory":
                    configured.mkdir(parents=True)
                elif kind == "unset":
                    configured = None
                result = resolve_codex_executable(configured, which=lambda _: str(current))
                self.assertEqual(result, str(current))
                self.assertEqual(current.read_bytes(), before)
                if kind == "removed":
                    self.assertFalse(configured.exists())
                elif kind == "directory":
                    self.assertTrue(configured.is_dir())

    def test_no_available_cli_does_not_replace_the_local_binding(self):
        configured = self.root / "removed-install" / "codex.exe"
        for kind in ("absent", "removed", "directory"):
            with self.subTest(discovered=kind):
                discovered = self.root / kind / "codex.exe"
                if kind == "directory":
                    discovered.mkdir(parents=True)
                found = None if kind == "absent" else str(discovered)
                self.assertIsNone(resolve_codex_executable(configured, which=lambda _: found))
                self.assertFalse(configured.exists())

    def test_default_lookup_uses_the_current_codex_path(self):
        current = self.binary("updated-install/codex.exe")
        with patch("ml.codex_cli.shutil.which", return_value=str(current)) as lookup:
            self.assertEqual(resolve_codex_executable(), str(current))
        lookup.assert_called_once_with("codex")


if __name__ == "__main__":
    unittest.main()
