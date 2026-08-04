import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomlkit

import provider_config
from provider_config import (
    DEEPSEEK_MODEL,
    ProviderConfigConflictError,
    ProviderConfigManager,
    ProviderRestoreUnavailableError,
)


class IdentityProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("invalid test payload")
        return value[len(b"protected:") :]


class ProviderConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / ".codex"
        self.state_directory = self.root / "账号文件"
        self.codex_home.mkdir()
        self.asset = Path(provider_config.__file__).parent / "assets" / "deepseek_models.json"
        self.manager = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @property
    def config_path(self) -> Path:
        return self.codex_home / "config.toml"

    @property
    def models_path(self) -> Path:
        return self.codex_home / "models.json"

    def write_config(self, text: str) -> None:
        with self.config_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write(text)

    def parse_config(self):
        return tomlkit.parse(self.config_path.read_text(encoding="utf-8"))

    def test_activate_preserves_unrelated_config_and_applies_official_fields(self) -> None:
        self.write_config(
            '# user settings\nmodel = "gpt-5"\nmodel_context_window = 200000\n\n'
            '[mcp_servers.local]\ncommand = "tool.exe"\n\n'
            '[projects."C:/work"]\ntrust_level = "trusted"\n'
        )
        previous_models = {"models": [{"slug": "local-model"}]}
        self.models_path.write_text(json.dumps(previous_models), encoding="utf-8")

        result = self.manager.activate_deepseek("sk-test-1234")
        document = self.parse_config()

        self.assertTrue(result.changed)
        self.assertEqual(str(document["model"]), DEEPSEEK_MODEL)
        self.assertEqual(str(document["model_provider"]), "deepseek")
        self.assertEqual(str(document["preferred_auth_method"]), "apikey")
        self.assertNotIn("model_context_window", document)
        self.assertEqual(str(document["mcp_servers"]["local"]["command"]), "tool.exe")
        self.assertEqual(str(document["projects"]["C:/work"]["trust_level"]), "trusted")
        self.assertEqual(
            str(document["model_providers"]["deepseek"]["wire_api"]),
            "responses",
        )
        self.assertEqual(
            str(document["model_providers"]["deepseek"]["experimental_bearer_token"]),
            "sk-test-1234",
        )
        self.assertEqual(self.models_path.read_bytes(), self.asset.read_bytes())
        state_bytes = self.manager.state_path.read_bytes()
        self.assertNotIn(b"sk-test-1234", state_bytes)
        self.assertEqual(self.manager.status().mode, "deepseek")
        self.assertFalse(self.manager.status().conflict)

    def test_restore_is_field_scoped_and_preserves_later_unrelated_changes(self) -> None:
        original = (
            'model = "gpt-5.4"\nmodel_reasoning_effort = "xhigh"\n'
            'profile = "work"\n\n[profiles.work]\nmodel = "gpt-5.4"\n\n'
            '[mcp_servers.original]\ncommand = "original.exe"\n'
        )
        self.write_config(original)
        original_models = b'{"models":[{"slug":"local"}]}\n'
        self.models_path.write_bytes(original_models)
        self.manager.activate_deepseek("sk-test-5678")

        document = self.parse_config()
        new_mcp = tomlkit.table()
        new_mcp.add("command", "added-later.exe")
        document["mcp_servers"]["added_later"] = new_mcp
        self.write_config(tomlkit.dumps(document))

        result = self.manager.restore_openai()
        restored = self.parse_config()

        self.assertTrue(result.changed)
        self.assertEqual(str(restored["model"]), "gpt-5.4")
        self.assertEqual(str(restored["model_reasoning_effort"]), "xhigh")
        self.assertEqual(str(restored["profile"]), "work")
        self.assertIn("work", restored["profiles"])
        self.assertEqual(
            str(restored["mcp_servers"]["added_later"]["command"]),
            "added-later.exe",
        )
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())

    def test_restore_removes_files_that_did_not_exist_before_activation(self) -> None:
        self.manager.activate_deepseek("sk-test-0000")

        self.manager.restore_openai()

        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.models_path.exists())

    def test_managed_field_change_is_reported_without_overwrite(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-1111")
        document = self.parse_config()
        document["model_reasoning_effort"] = "max"
        self.write_config(tomlkit.dumps(document))
        before = self.config_path.read_bytes()

        with self.assertRaises(ProviderConfigConflictError):
            self.manager.restore_openai()

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertTrue(self.manager.state_path.exists())

    def test_models_change_is_reported_without_overwrite(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-2222")
        self.models_path.write_text('{"models": []}', encoding="utf-8")

        with self.assertRaises(ProviderConfigConflictError):
            self.manager.restore_openai()

        self.assertEqual(self.models_path.read_text(encoding="utf-8"), '{"models": []}')

    def test_invalid_toml_aborts_without_creating_state_or_models(self) -> None:
        self.write_config('model = "unterminated\n')
        before = self.config_path.read_bytes()

        with self.assertRaises(provider_config.InvalidCodexConfigError):
            self.manager.activate_deepseek("sk-test-3333")

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.models_path.exists())
        self.assertFalse(self.manager.state_path.exists())

    def test_invalid_model_providers_shape_aborts_cleanly(self) -> None:
        self.write_config('model_providers = "invalid"\n')
        before = self.config_path.read_bytes()

        with self.assertRaises(provider_config.InvalidCodexConfigError):
            self.manager.activate_deepseek("sk-test-3434")

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.manager.state_path.exists())

    def test_second_file_write_failure_rolls_back_models_and_state(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_config = self.config_path.read_bytes()
        original_models = b'{"models":[{"slug":"original"}]}\n'
        self.models_path.write_bytes(original_models)
        original_replace = provider_config._replace_optional
        failed = False

        def fail_config_once(path, value):
            nonlocal failed
            if Path(path) == self.config_path and not failed:
                failed = True
                raise OSError("simulated config failure")
            return original_replace(path, value)

        with mock.patch("provider_config._replace_optional", side_effect=fail_config_once):
            with self.assertRaises(provider_config.ProviderConfigError):
                self.manager.activate_deepseek("sk-test-4444")

        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())

    def test_restore_write_failure_keeps_deepseek_configuration_and_state(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-4545")
        deepseek_config = self.config_path.read_bytes()
        deepseek_models = self.models_path.read_bytes()
        original_replace = provider_config._replace_optional
        failed = False

        def fail_models_once(path, value):
            nonlocal failed
            if Path(path) == self.models_path and not failed:
                failed = True
                raise OSError("simulated models restore failure")
            return original_replace(path, value)

        with mock.patch("provider_config._replace_optional", side_effect=fail_models_once):
            with self.assertRaises(provider_config.ProviderConfigError):
                self.manager.restore_openai()

        self.assertEqual(self.config_path.read_bytes(), deepseek_config)
        self.assertEqual(self.models_path.read_bytes(), deepseek_models)
        self.assertTrue(self.manager.state_path.exists())

    def test_api_key_update_does_not_replace_original_restore_baseline(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-old1")

        self.manager.activate_deepseek("sk-test-new2")
        self.manager.restore_openai()

        self.assertEqual(str(self.parse_config()["model"]), "gpt-5")

    def test_official_backup_can_be_adopted_for_safe_restore(self) -> None:
        backup = self.codex_home / "backup-deepseek" / "config.toml"
        backup.parent.mkdir()
        backup.write_text('model = "gpt-5"\n', encoding="utf-8")
        self.write_config(
            'model = "deepseek-v4-flash"\nmodel_provider = "deepseek"\n'
            'preferred_auth_method = "apikey"\nforced_login_method = "api"\n'
            'model_reasoning_effort = "high"\n'
            'model_catalog_json = "{0}"\n\n'
            '[model_providers.deepseek]\nname = "deepseek"\n'
            'base_url = "https://api.deepseek.com/"\nwire_api = "responses"\n'
            'experimental_bearer_token = "sk-external-test"\n'.format(self.models_path.as_posix())
        )
        self.models_path.write_bytes(self.asset.read_bytes())

        result = self.manager.restore_openai()

        self.assertTrue(result.changed)
        self.assertEqual(str(self.parse_config()["model"]), "gpt-5")
        self.assertFalse(self.models_path.exists())

    def test_external_configuration_without_backup_is_not_guessed(self) -> None:
        self.write_config('model_provider = "deepseek"\n')
        self.models_path.write_bytes(self.asset.read_bytes())

        with self.assertRaises(ProviderRestoreUnavailableError):
            self.manager.restore_openai()


if __name__ == "__main__":
    unittest.main()
