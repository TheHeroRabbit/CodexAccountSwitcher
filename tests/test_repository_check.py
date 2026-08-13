import json
import shutil
import tempfile
import unittest
from pathlib import Path

import provider_config
from tools import repository_check


class RepositoryCheckTests(unittest.TestCase):
    def test_rejects_runtime_and_credential_paths(self):
        self.assertTrue(repository_check.validate_path(Path("账号文件/auth.json")))
        self.assertTrue(repository_check.validate_path(Path("auth_0123456789abcdef.json")))
        self.assertTrue(repository_check.validate_path(Path("build/application.spec")))
        self.assertTrue(
            repository_check.validate_path(
                Path(".codex-account-switcher.config.toml." + "a" * 32 + ".tmp")
            )
        )
        self.assertTrue(
            repository_check.validate_path(
                Path("账号文件/.codex-account-switcher-account-transaction.dat")
            )
        )

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
        expected_hash = "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"
        self.assertEqual(repository_check.OFFICIAL_MODELS_SHA256, expected_hash)
        self.assertEqual(provider_config.OFFICIAL_MODELS_SHA256, expected_hash)
        self.assertEqual(repository_check.validate_models_asset(), [])

    def test_models_payload_requires_flash_and_pro(self):
        for present, missing in (
            ("deepseek-v4-flash", "deepseek-v4-pro"),
            ("deepseek-v4-pro", "deepseek-v4-flash"),
        ):
            with self.subTest(missing=missing):
                value = json.dumps({"models": [{"slug": present}]}).encode("utf-8")
                errors = repository_check.validate_models_payload(value)

                self.assertTrue(any(missing in error for error in errors))

    def test_models_payload_rejects_invalid_models_shapes_without_throwing(self):
        for payload in ({}, {"models": None}, {"models": 1}, {"models": "two"}, []):
            with self.subTest(payload=payload):
                errors = repository_check.validate_models_payload(
                    json.dumps(payload).encode("utf-8")
                )

                self.assertTrue(errors)

    def test_runtime_and_repository_model_contracts_match(self):
        self.assertEqual(
            frozenset(provider_config.SUPPORTED_DEEPSEEK_MODELS),
            repository_check.REQUIRED_DEEPSEEK_MODELS,
        )
        self.assertEqual(
            provider_config.OFFICIAL_MODELS_SHA256,
            repository_check.OFFICIAL_MODELS_SHA256,
        )

    def test_release_version_sources_are_aligned(self):
        self.assertEqual(repository_check.validate_version_alignment(), [])

    def test_version_alignment_detects_a_stale_packaging_title(self):
        relative_files = (
            "app.py",
            "version_info.txt",
            "CHANGELOG.md",
            "SECURITY.md",
            "README.md",
            "docs/BUILDING.md",
            "docs/VERIFICATION.md",
            "packaging/使用说明.txt",
            "packaging/更新说明.txt",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in relative_files:
                source = repository_check.PROJECT_ROOT / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(source), str(destination))
            stale = root / "packaging" / "更新说明.txt"
            stale.write_text("Codex 切换器 v0.0.0\n", encoding="utf-8")

            errors = repository_check.validate_version_alignment(root)

        self.assertTrue(any("packaging/更新说明.txt" in error for error in errors))

    def test_version_alignment_rejects_stale_verification_body_with_current_header(self):
        relative_files = (
            "app.py",
            "version_info.txt",
            "CHANGELOG.md",
            "SECURITY.md",
            "README.md",
            "docs/BUILDING.md",
            "docs/VERIFICATION.md",
            "packaging/使用说明.txt",
            "packaging/更新说明.txt",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in relative_files:
                source = repository_check.PROJECT_ROOT / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(source), str(destination))
            verification = root / "docs" / "VERIFICATION.md"
            text = verification.read_text(encoding="utf-8")
            text = text.replace("- FileVersion：1.5.0。", "- FileVersion：1.4.0。")
            verification.write_text(text, encoding="utf-8")

            errors = repository_check.validate_version_alignment(root)

        self.assertTrue(any("explicit version evidence" in error for error in errors))

    def test_text_file_without_final_newline_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.txt"
            path.write_text("content", encoding="utf-8")
            errors = repository_check.validate_text(path, path.read_bytes())

        self.assertIn("text file must end with a newline", errors)


if __name__ == "__main__":
    unittest.main()
