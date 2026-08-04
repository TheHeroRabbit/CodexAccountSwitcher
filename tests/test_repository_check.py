import tempfile
import unittest
from pathlib import Path

from tools import repository_check


class RepositoryCheckTests(unittest.TestCase):
    def test_rejects_runtime_and_credential_paths(self):
        self.assertTrue(repository_check.validate_path(Path("账号文件/auth.json")))
        self.assertTrue(repository_check.validate_path(Path("auth_0123456789abcdef.json")))
        self.assertTrue(repository_check.validate_path(Path("build/application.spec")))

    def test_secret_scan_reports_category_without_echoing_value(self):
        secret = "sk-" + "a" * 24
        errors = repository_check.validate_text(Path("sample.txt"), (secret + "\n").encode())

        self.assertIn("possible API key detected", errors)
        self.assertNotIn(secret, "\n".join(errors))

    def test_clean_utf8_text_passes(self):
        self.assertEqual(
            repository_check.validate_text(Path("sample.md"), "安全说明。\n".encode("utf-8")),
            [],
        )

    def test_powershell_source_remains_windows_51_compatible(self):
        errors = repository_check.validate_text(
            Path("release.ps1"),
            "Write-Host '完成'\n".encode("utf-8"),
        )

        self.assertIn(
            "PowerShell scripts must remain ASCII-compatible with Windows PowerShell 5.1",
            errors,
        )

    def test_rejects_hard_coded_absolute_environment_path(self):
        absolute_environment = "C:" + "\\venv\\Scripts\\python.exe\n"
        errors = repository_check.validate_text(
            Path("README.md"),
            absolute_environment.encode("utf-8"),
        )

        self.assertIn("hard-coded absolute virtual environment path detected", errors)
        self.assertEqual(
            repository_check.validate_text(
                Path("README.md"),
                ".\\.venv\\Scripts\\python.exe\n".encode("utf-8"),
            ),
            [],
        )

    def test_official_models_asset_matches_pinned_hash(self):
        self.assertEqual(repository_check.validate_models_asset(), [])

    def test_text_file_without_final_newline_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.txt"
            path.write_text("content", encoding="utf-8")
            errors = repository_check.validate_text(path, path.read_bytes())

        self.assertIn("text file must end with a newline", errors)


if __name__ == "__main__":
    unittest.main()
