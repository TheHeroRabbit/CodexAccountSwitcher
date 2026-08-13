import base64
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomlkit

import provider_config
from provider_config import (
    DEEPSEEK_MODEL,
    DEEPSEEK_MODEL_FLASH,
    DEEPSEEK_MODEL_PRO,
    DEFAULT_DEEPSEEK_MODEL,
    MANAGED_ROOT_KEYS,
    SUPPORTED_DEEPSEEK_MODELS,
    ProviderConfigConflictError,
    ProviderConfigError,
    ProviderConfigManager,
    ProviderRestoreUnavailableError,
    ProviderTransactionRecoveryError,
)


class IdentityProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("invalid test payload")
        return value[len(b"protected:") :]


class RandomizedProtector:
    """Model DPAPI's randomized ciphertext while preserving round trips."""

    def __init__(self) -> None:
        self.counter = 0

    def protect(self, value: bytes) -> bytes:
        self.counter += 1
        return "{:08d}:".format(self.counter).encode("ascii") + value

    def unprotect(self, value: bytes) -> bytes:
        return value.split(b":", 1)[1]


def _exit_after_provider_config_temp_is_synced(codex_home, state_directory, asset) -> None:
    manager = ProviderConfigManager(
        Path(codex_home),
        Path(state_directory),
        Path(asset),
        protector=IdentityProtector(),
    )
    original_prepare = provider_config._prepare_atomic_write

    def exit_after_prepare(path, value):
        temporary = original_prepare(path, value)
        if Path(path) == manager.config_path:
            os._exit(77)
        return temporary

    with mock.patch("provider_config._prepare_atomic_write", side_effect=exit_after_prepare):
        manager.activate_deepseek("sk-" + "orphan-provider", DEEPSEEK_MODEL_PRO)


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

    def test_supported_model_contract_uses_exact_official_slugs(self) -> None:
        self.assertEqual(
            SUPPORTED_DEEPSEEK_MODELS,
            ("deepseek-v4-flash", "deepseek-v4-pro"),
        )
        self.assertEqual(DEFAULT_DEEPSEEK_MODEL, "deepseek-v4-flash")
        self.assertEqual(DEEPSEEK_MODEL, "deepseek-v4-flash")
        self.assertNotIn("deepseek-4v-pro", SUPPORTED_DEEPSEEK_MODELS)

    def test_v1_managed_fingerprint_keys_remain_compatible(self) -> None:
        self.assertEqual(
            MANAGED_ROOT_KEYS,
            (
                "model",
                "model_provider",
                "preferred_auth_method",
                "forced_login_method",
                "model_reasoning_effort",
                "model_catalog_json",
                "profile",
                "profiles",
                "oss_provider",
                "openai_base_url",
                "model_context_window",
                "model_auto_compact_token_limit",
                "model_auto_compact_token_limit_scope",
                "base_instructions",
                "model_instructions_file",
                "compact_prompt",
                "experimental_compact_prompt_file",
                "service_tier",
                "model_verbosity",
                "model_reasoning_summary",
                "plan_mode_reasoning_effort",
                "experimental_use_unified_exec_tool",
            ),
        )

    def test_activate_writes_each_official_model(self) -> None:
        for model_slug in ("deepseek-v4-flash", "deepseek-v4-pro"):
            with self.subTest(model_slug=model_slug):
                self.write_config(
                    '# user settings\nmodel = "gpt-5"\nmodel_context_window = 200000\n\n'
                    '[mcp_servers.local]\ncommand = "tool.exe"\n'
                )
                self.models_path.write_text(
                    json.dumps({"models": [{"slug": "local-model"}]}),
                    encoding="utf-8",
                )

                result = self.manager.activate_deepseek("sk-test-model", model_slug)
                document = self.parse_config()

                self.assertTrue(result.changed)
                self.assertEqual(result.active_model, model_slug)
                self.assertEqual(str(document["model"]), model_slug)
                self.assertEqual(str(document["model_provider"]), "deepseek")
                self.assertEqual(str(document["preferred_auth_method"]), "apikey")
                self.assertEqual(str(document["forced_login_method"]), "api")
                self.assertEqual(str(document["model_reasoning_effort"]), "max")
                self.assertEqual(str(document["model_catalog_json"]), self.models_path.as_posix())
                self.assertEqual(
                    str(document["model_providers"]["deepseek"]["base_url"]),
                    "https://api.deepseek.com/",
                )
                self.assertEqual(
                    str(document["model_providers"]["deepseek"]["wire_api"]),
                    "responses",
                )
                self.assertEqual(
                    str(document["model_providers"]["deepseek"]["experimental_bearer_token"]),
                    "sk-test-model",
                )
                self.assertNotIn("model_context_window", document)
                self.assertEqual(str(document["mcp_servers"]["local"]["command"]), "tool.exe")
                self.assertEqual(self.models_path.read_bytes(), self.asset.read_bytes())
                status = self.manager.status()
                self.assertEqual(status.active_model, model_slug)
                self.assertFalse(status.conflict)

                self.manager.restore_openai()

    def test_unsupported_model_is_rejected_before_any_mutation(self) -> None:
        invalid_values = (None, "", "deepseek-4v-pro", "deepseek-v4-Pro", "deepseek-v4-pro ")
        for invalid_model in invalid_values:
            with self.subTest(model=invalid_model):
                self.write_config('model = "gpt-5"\n')
                self.models_path.write_bytes(b'{"models":[{"slug":"local"}]}\n')
                self.manager.state_directory.mkdir(parents=True, exist_ok=True)
                self.manager.state_path.write_bytes(b"existing-state")
                before = (
                    self.config_path.read_bytes(),
                    self.models_path.read_bytes(),
                    self.manager.state_path.read_bytes(),
                )

                with self.assertRaises(ProviderConfigError):
                    self.manager.activate_deepseek("sk-test-invalid", invalid_model)

                self.assertEqual(self.config_path.read_bytes(), before[0])
                self.assertEqual(self.models_path.read_bytes(), before[1])
                self.assertEqual(self.manager.state_path.read_bytes(), before[2])

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
        self.assertEqual(str(document["model_reasoning_effort"]), "max")
        document["model_reasoning_effort"] = "low"
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

    def test_models_catalog_requires_both_supported_models(self) -> None:
        for only_slug, missing_slug in (
            (DEEPSEEK_MODEL_FLASH, DEEPSEEK_MODEL_PRO),
            (DEEPSEEK_MODEL_PRO, DEEPSEEK_MODEL_FLASH),
        ):
            with self.subTest(missing_slug=missing_slug):
                payload = json.dumps({"models": [{"slug": only_slug}]}).encode("utf-8")
                with self.assertRaisesRegex(ProviderConfigError, missing_slug):
                    self.manager._validate_models(payload)

    def test_second_file_write_failure_rolls_back_models_and_state(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_config = self.config_path.read_bytes()
        original_models = b'{"models":[{"slug":"original"}]}\n'
        self.models_path.write_bytes(original_models)
        original_replace = provider_config._guarded_replace_optional
        failed = False

        def fail_config_once(path, expected, value, limit, label):
            nonlocal failed
            if Path(path) == self.config_path and not failed:
                failed = True
                raise OSError("simulated config failure")
            return original_replace(path, expected, value, limit, label)

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_config_once,
        ):
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
        original_replace = provider_config._guarded_replace_optional
        failed = False

        def fail_models_once(path, expected, value, limit, label):
            nonlocal failed
            if Path(path) == self.models_path and not failed:
                failed = True
                raise OSError("simulated models restore failure")
            return original_replace(path, expected, value, limit, label)

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_models_once,
        ):
            with self.assertRaises(provider_config.ProviderConfigError):
                self.manager.restore_openai()

        self.assertEqual(self.config_path.read_bytes(), deepseek_config)
        self.assertEqual(self.models_path.read_bytes(), deepseek_models)
        self.assertTrue(self.manager.state_path.exists())

    def test_model_switch_write_failure_restores_config_models_and_state(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-switch-fail", DEEPSEEK_MODEL_FLASH)
        before = (
            self.config_path.read_bytes(),
            self.models_path.read_bytes(),
            self.manager.state_path.read_bytes(),
        )
        original_replace = provider_config._guarded_replace_optional

        def fail_pro_config(path, expected, value, limit, label):
            if Path(path) == self.config_path and value is not None:
                document = tomlkit.parse(value.decode("utf-8"))
                if str(document.get("model")) == DEEPSEEK_MODEL_PRO:
                    raise OSError("simulated Pro config failure")
            return original_replace(path, expected, value, limit, label)

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_pro_config,
        ):
            with self.assertRaises(ProviderConfigError):
                self.manager.activate_deepseek(
                    "sk-switch-fail",
                    DEEPSEEK_MODEL_PRO,
                )

        self.assertEqual(self.config_path.read_bytes(), before[0])
        self.assertEqual(self.models_path.read_bytes(), before[1])
        self.assertEqual(self.manager.state_path.read_bytes(), before[2])
        self.assertEqual(self.manager.status().active_model, DEEPSEEK_MODEL_FLASH)

    def test_state_write_failure_does_not_touch_codex_files(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.models_path.write_bytes(b'{"models":[{"slug":"local"}]}\n')
        before_config = self.config_path.read_bytes()
        before_models = self.models_path.read_bytes()
        original_replace = provider_config._guarded_replace_optional

        def fail_state(path, expected, value, limit, label):
            if Path(path) == self.manager.state_path:
                raise OSError("simulated state failure")
            return original_replace(path, expected, value, limit, label)

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_state,
        ):
            with self.assertRaises(ProviderConfigError):
                self.manager.activate_deepseek("sk-test-state-fail", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), before_config)
        self.assertEqual(self.models_path.read_bytes(), before_models)
        self.assertFalse(self.manager.state_path.exists())

    def test_activation_cas_preserves_external_config_change(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_models = b'{"models":[{"slug":"local"}]}\n'
        self.models_path.write_bytes(original_models)
        external_config = b'model = "gpt-external-new"\n'
        original_replace = provider_config._guarded_replace_optional
        injected = False

        def inject_after_state(path, expected, value, limit, label):
            nonlocal injected
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path and not injected:
                injected = True
                self.config_path.write_bytes(external_config)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=inject_after_state,
        ):
            with self.assertRaises(ProviderConfigConflictError):
                self.manager.activate_deepseek("sk-test-cas", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), external_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())

    def test_external_change_during_atomic_preparation_is_not_overwritten(self) -> None:
        self.write_config('model = "gpt-5"\n')
        external_config = b'model = "gpt-external-during-prepare"\n'
        original_prepare = provider_config._prepare_atomic_write
        injected = False

        def inject_while_preparing(path, value):
            nonlocal injected
            temporary = original_prepare(path, value)
            if Path(path) == self.config_path and not injected:
                injected = True
                self.config_path.write_bytes(external_config)
            return temporary

        with mock.patch(
            "provider_config._prepare_atomic_write",
            side_effect=inject_while_preparing,
        ):
            with self.assertRaises(ProviderConfigConflictError):
                self.manager.activate_deepseek("sk-prepare-race", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), external_config)
        self.assertFalse(self.models_path.exists())
        self.assertFalse(self.manager.state_path.exists())

    def test_concurrent_delete_during_guarded_removal_is_reported(self) -> None:
        target = self.root / "guarded-removal.txt"
        target.write_bytes(b"expected")
        original_unlink = Path.unlink
        injected = False

        def delete_first(path, *args, **kwargs):
            nonlocal injected
            if Path(path) == target and not injected:
                injected = True
                original_unlink(path)
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=delete_first):
            with self.assertRaises(ProviderConfigConflictError):
                provider_config._guarded_replace_optional(
                    target,
                    b"expected",
                    None,
                    provider_config.MAX_CONFIG_BYTES,
                    target.name,
                )

        self.assertFalse(target.exists())

    def test_rollback_change_during_atomic_preparation_is_not_overwritten(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.models_path.write_bytes(b'{"models":[{"slug":"baseline-local"}]}\n')
        original_state = None
        external_models = b'{"models":[{"slug":"external-during-rollback"}]}\n'
        original_prepare = provider_config._prepare_atomic_write
        original_guarded = provider_config._guarded_replace_optional
        config_failure_seen = False
        injected = False

        def fail_config(path, expected, value, limit, label):
            nonlocal config_failure_seen
            if Path(path) == self.config_path and not config_failure_seen:
                config_failure_seen = True
                raise OSError("force rollback")
            return original_guarded(path, expected, value, limit, label)

        def inject_while_preparing(path, value):
            nonlocal injected
            temporary = original_prepare(path, value)
            if config_failure_seen and Path(path) == self.models_path and not injected:
                injected = True
                self.models_path.write_bytes(external_models)
            return temporary

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_config,
        ), mock.patch(
            "provider_config._prepare_atomic_write",
            side_effect=inject_while_preparing,
        ):
            with self.assertRaisesRegex(ProviderConfigError, "未能完整恢复"):
                self.manager.activate_deepseek("sk-rollback-race", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.models_path.read_bytes(), external_models)
        self.assertEqual(
            self.manager.state_path.read_bytes() if self.manager.state_path.exists() else None,
            original_state,
        )

    def test_activation_rechecks_noop_models_path_before_commit(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.models_path.write_bytes(self.asset.read_bytes())
        original_config = self.config_path.read_bytes()
        external_models = b'{"models":[{"slug":"external-noop-race"}]}\n'
        original_replace = provider_config._guarded_replace_optional
        injected = False

        def inject_after_state(path, expected, value, limit, label):
            nonlocal injected
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path and not injected:
                injected = True
                self.models_path.write_bytes(external_models)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=inject_after_state,
        ):
            with self.assertRaises(ProviderConfigConflictError):
                self.manager.activate_deepseek("sk-test-noop-race", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), external_models)
        self.assertFalse(self.manager.state_path.exists())

    def test_commit_final_verification_preserves_late_external_state(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_config = self.config_path.read_bytes()
        external_state = b"external-state-after-config-write"
        original_replace = provider_config._guarded_replace_optional
        injected = False

        def inject_after_config(path, expected, value, limit, label):
            nonlocal injected
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.config_path and not injected:
                injected = True
                self.manager.state_path.write_bytes(external_state)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=inject_after_config,
        ):
            with self.assertRaises(ProviderConfigError):
                self.manager.activate_deepseek("sk-test-late-race", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.manager.state_path.read_bytes(), external_state)
        self.assertFalse(self.models_path.exists())

    def test_restore_cas_preserves_external_models_change(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-restore-cas", DEEPSEEK_MODEL_PRO)
        deepseek_config = self.config_path.read_bytes()
        state_before = self.manager.state_path.read_bytes()
        external_models = b'{"models":[{"slug":"external-new"}]}\n'
        original_replace = provider_config._guarded_replace_optional
        injected = False

        def inject_after_config(path, expected, value, limit, label):
            nonlocal injected
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.config_path and not injected:
                injected = True
                self.models_path.write_bytes(external_models)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=inject_after_config,
        ):
            with self.assertRaises(ProviderConfigConflictError):
                self.manager.restore_openai()

        self.assertEqual(self.config_path.read_bytes(), deepseek_config)
        self.assertEqual(self.models_path.read_bytes(), external_models)
        self.assertEqual(self.manager.state_path.read_bytes(), state_before)

    def test_rollback_does_not_overwrite_newer_external_models(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.models_path.write_bytes(b'{"models":[{"slug":"local"}]}\n')
        external_models = b'{"models":[{"slug":"external-after-write"}]}\n'
        original_replace = provider_config._guarded_replace_optional

        def fail_config_after_external_models(path, expected, value, limit, label):
            if Path(path) == self.config_path:
                self.models_path.write_bytes(external_models)
                raise OSError("simulated config failure")
            return original_replace(path, expected, value, limit, label)

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_config_after_external_models,
        ):
            with self.assertRaises(ProviderConfigError):
                self.manager.activate_deepseek("sk-owned-rollback", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.models_path.read_bytes(), external_models)
        self.assertFalse(self.manager.state_path.exists())

    def test_api_key_update_does_not_replace_original_restore_baseline(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-old1")

        self.manager.activate_deepseek("sk-test-new2")
        self.manager.restore_openai()

        self.assertEqual(str(self.parse_config()["model"]), "gpt-5")

    def test_key_update_preserves_each_active_model(self) -> None:
        for model_slug in (DEEPSEEK_MODEL_FLASH, DEEPSEEK_MODEL_PRO):
            with self.subTest(model_slug=model_slug):
                self.write_config('model = "gpt-5"\n')
                self.manager.activate_deepseek("sk-test-old-key", model_slug)

                result = self.manager.activate_deepseek("sk-test-new-key", model_slug)

                self.assertTrue(result.changed)
                self.assertEqual(str(self.parse_config()["model"]), model_slug)
                self.assertEqual(self.manager.status().active_model, model_slug)
                self.manager.restore_openai()

    def test_same_model_and_key_reactivation_is_idempotent(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-same", DEEPSEEK_MODEL_PRO)
        before = (
            self.config_path.read_bytes(),
            self.models_path.read_bytes(),
            self.manager.state_path.read_bytes(),
        )

        result = self.manager.activate_deepseek("sk-test-same", DEEPSEEK_MODEL_PRO)

        self.assertFalse(result.changed)
        self.assertEqual(result.active_model, DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.config_path.read_bytes(), before[0])
        self.assertEqual(self.models_path.read_bytes(), before[1])
        self.assertEqual(self.manager.state_path.read_bytes(), before[2])

    def test_idempotent_reactivation_does_not_reencrypt_randomized_state(self) -> None:
        protector = RandomizedProtector()
        manager = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=protector,
        )
        self.write_config('model = "gpt-5"\n')
        manager.activate_deepseek("sk-idempotent", DEEPSEEK_MODEL_PRO)
        before = manager.state_path.read_bytes()

        result = manager.activate_deepseek("sk-idempotent", DEEPSEEK_MODEL_PRO)

        self.assertFalse(result.changed)
        # The semantic provider state is not re-encrypted; the second protect
        # call belongs to the durable transaction journal from the first write.
        self.assertEqual(protector.counter, 2)
        self.assertEqual(manager.state_path.read_bytes(), before)

    def test_switching_models_preserves_original_openai_baseline(self) -> None:
        for first_model, second_model in (
            (DEEPSEEK_MODEL_FLASH, DEEPSEEK_MODEL_PRO),
            (DEEPSEEK_MODEL_PRO, DEEPSEEK_MODEL_FLASH),
        ):
            with self.subTest(first=first_model, second=second_model):
                original_config = (
                    'model = "gpt-5.4"\nmodel_reasoning_effort = "xhigh"\n\n'
                    '[mcp_servers.local]\ncommand = "tool.exe"\n'
                ).encode("utf-8")
                original_models = b'{"models":[{"slug":"private"}]}\n'
                self.config_path.write_bytes(original_config)
                self.models_path.write_bytes(original_models)

                self.manager.activate_deepseek("sk-model-switch", first_model)
                result = self.manager.activate_deepseek(
                    "sk-model-switch",
                    second_model,
                )
                switched_state = self.manager._load_state(required=True)

                self.assertTrue(result.changed)
                self.assertEqual(str(self.parse_config()["model"]), second_model)
                self.assertEqual(
                    switched_state["baseline_config"],
                    self.manager._encode_optional(original_config),
                )
                self.assertEqual(
                    switched_state["baseline_models"],
                    self.manager._encode_optional(original_models),
                )

                self.manager.restore_openai()
                self.assertEqual(
                    tomlkit.parse(self.config_path.read_text(encoding="utf-8")).unwrap(),
                    tomlkit.parse(original_config.decode("utf-8")).unwrap(),
                )
                self.assertEqual(self.models_path.read_bytes(), original_models)

    def test_status_reports_unknown_and_missing_external_models_without_guessing(self) -> None:
        for model_line, expected_model in (
            ('model = "private-deepseek"\n', "private-deepseek"),
            ("", None),
        ):
            with self.subTest(expected_model=expected_model):
                self.write_config(model_line + 'model_provider = "deepseek"\n')

                status = self.manager.status()

                self.assertEqual(status.mode, "deepseek")
                self.assertEqual(status.active_model, expected_model)
                self.assertNotIn(status.active_model, SUPPORTED_DEEPSEEK_MODELS)

    def test_openai_status_does_not_expose_stale_deepseek_model(self) -> None:
        self.write_config('model = "gpt-5"\n')
        self.manager.activate_deepseek("sk-test-status", DEEPSEEK_MODEL_PRO)
        self.manager.restore_openai()

        status = self.manager.status()

        self.assertEqual(status.mode, "openai")
        self.assertIsNone(status.active_model)

    def test_version_one_flash_state_remains_loadable_and_restorable(self) -> None:
        original_config = b'model = "gpt-5"\n'
        self.config_path.write_bytes(original_config)
        self.manager.activate_deepseek("sk-test-legacy", DEEPSEEK_MODEL_FLASH)
        protected_state = self.manager.state_path.read_bytes()
        decoded_state = json.loads(IdentityProtector().unprotect(protected_state).decode("utf-8"))

        self.assertEqual(decoded_state["version"], 1)
        status = self.manager.status()
        self.assertEqual(status.active_model, DEEPSEEK_MODEL_FLASH)
        self.assertFalse(status.conflict)

        self.manager.restore_openai()
        self.assertEqual(str(self.parse_config()["model"]), "gpt-5")

    def test_fixed_v14_flash_state_fixture_remains_compatible(self) -> None:
        legacy_config = (
            (
                'model = "deepseek-v4-flash"\n'
                'model_provider = "deepseek"\n'
                'preferred_auth_method = "apikey"\n'
                'forced_login_method = "api"\n'
                'model_reasoning_effort = "high"\n'
                'model_catalog_json = "C:/Users/User/.codex/models.json"\n\n'
                "[model_providers.deepseek]\n"
                'name = "deepseek"\n'
                'base_url = "https://api.deepseek.com/"\n'
                'wire_api = "responses"\n'
                'experimental_bearer_token = "{}"\n'
            )
            .format("sk-legacy-" + "fixture-1234")
            .encode("utf-8")
        )
        baseline_config = b'model = "gpt-5"\n'
        models = self.asset.read_bytes()
        legacy_models_sha256 = "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"
        self.assertEqual(hashlib.sha256(models).hexdigest(), legacy_models_sha256)
        legacy_state = {
            "version": 1,
            "created_at": "2026-08-04T12:00:00+08:00",
            "baseline_config": base64.b64encode(baseline_config).decode("ascii"),
            "baseline_models": None,
            # Fixed output from the released v1.4.0 fingerprint implementation.
            "expected_fingerprint": (
                "fb0833e24db583d60bdaf3fd23473a0a9c3c264d1fc00563207c88fd8b9275de"
            ),
            "expected_models_sha256": legacy_models_sha256,
            "source": "codex-switcher",
        }
        self.config_path.write_bytes(legacy_config)
        self.models_path.write_bytes(models)
        self.manager.state_path.parent.mkdir(parents=True)
        serialized = json.dumps(
            legacy_state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.manager.state_path.write_bytes(IdentityProtector().protect(serialized))

        status = self.manager.status()

        self.assertTrue(status.managed)
        self.assertFalse(status.conflict)
        self.assertEqual(status.active_model, DEEPSEEK_MODEL_FLASH)
        self.manager.activate_deepseek("sk-upgraded-key", DEEPSEEK_MODEL_PRO)
        upgraded_state = self.manager._load_state(required=True)
        self.assertEqual(self.manager.status().active_model, DEEPSEEK_MODEL_PRO)
        self.assertEqual(str(self.parse_config()["model_reasoning_effort"]), "max")
        self.assertEqual(
            upgraded_state["baseline_config"],
            base64.b64encode(baseline_config).decode("ascii"),
        )
        self.assertIsNone(upgraded_state["baseline_models"])
        self.manager.restore_openai()
        self.assertEqual(
            tomlkit.parse(self.config_path.read_text(encoding="utf-8")).unwrap(),
            tomlkit.parse(baseline_config.decode("utf-8")).unwrap(),
        )
        self.assertFalse(self.models_path.exists())

    def test_official_backup_can_restore_external_flash_and_pro(self) -> None:
        for model_slug in (DEEPSEEK_MODEL_FLASH, DEEPSEEK_MODEL_PRO):
            with self.subTest(model_slug=model_slug):
                backup = self.codex_home / "backup-deepseek" / "config.toml"
                backup.parent.mkdir(exist_ok=True)
                backup.write_text('model = "gpt-5"\n', encoding="utf-8")
                self.write_config(
                    'model = "{0}"\nmodel_provider = "deepseek"\n'
                    'preferred_auth_method = "apikey"\nforced_login_method = "api"\n'
                    'model_reasoning_effort = "high"\n'
                    'model_catalog_json = "{1}"\n\n'
                    '[model_providers.deepseek]\nname = "deepseek"\n'
                    'base_url = "https://api.deepseek.com/"\nwire_api = "responses"\n'
                    'experimental_bearer_token = "sk-external-test"\n'.format(
                        model_slug,
                        self.models_path.as_posix(),
                    )
                )
                self.models_path.write_bytes(self.asset.read_bytes())

                status = self.manager.status()
                result = self.manager.restore_openai()

                self.assertEqual(status.active_model, model_slug)
                self.assertTrue(status.external_backup_available)
                self.assertTrue(result.changed)
                self.assertEqual(str(self.parse_config()["model"]), "gpt-5")
                self.assertFalse(self.models_path.exists())

    def test_external_restore_rechecks_official_backup_during_commit(self) -> None:
        backup = self.codex_home / "backup-deepseek" / "config.toml"
        backup.parent.mkdir(exist_ok=True)
        backup.write_text('model = "gpt-5"\n', encoding="utf-8")
        self.write_config(
            'model = "deepseek-v4-pro"\nmodel_provider = "deepseek"\n'
            'preferred_auth_method = "apikey"\nforced_login_method = "api"\n'
            'model_reasoning_effort = "high"\n'
            'model_catalog_json = "{}"\n'.format(self.models_path.as_posix())
        )
        self.models_path.write_bytes(self.asset.read_bytes())
        deepseek_config = self.config_path.read_bytes()
        updated_backup = b'model = "gpt-external-new"\n'
        original_replace = provider_config._guarded_replace_optional
        injected = False

        def inject_after_config(path, expected, value, limit, label):
            nonlocal injected
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.config_path and not injected:
                injected = True
                backup.write_bytes(updated_backup)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=inject_after_config,
        ):
            with self.assertRaises(ProviderConfigConflictError):
                self.manager.restore_openai()

        self.assertEqual(self.config_path.read_bytes(), deepseek_config)
        self.assertEqual(self.models_path.read_bytes(), self.asset.read_bytes())
        self.assertEqual(backup.read_bytes(), updated_backup)
        self.assertFalse(self.manager.state_path.exists())

    def test_external_configuration_without_backup_is_not_guessed(self) -> None:
        self.write_config('model_provider = "deepseek"\n')
        self.models_path.write_bytes(self.asset.read_bytes())

        with self.assertRaises(ProviderRestoreUnavailableError):
            self.manager.restore_openai()

    def test_reasoning_defaults_to_max_for_both_models_and_model_switches(self) -> None:
        self.write_config('model = "gpt-5"\n')
        for model_slug in (
            DEEPSEEK_MODEL_FLASH,
            DEEPSEEK_MODEL_PRO,
            DEEPSEEK_MODEL_FLASH,
        ):
            result = self.manager.activate_deepseek("sk-reasoning-max", model_slug)
            self.assertEqual(result.active_model, model_slug)
            self.assertEqual(str(self.parse_config()["model_reasoning_effort"]), "max")

    def test_managed_active_api_key_is_verified_without_exposing_it_in_status(self) -> None:
        self.write_config('model = "gpt-5"\n')
        managed_key = "sk-managed-" + "authority"
        self.manager.activate_deepseek(managed_key, DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.manager.managed_deepseek_api_key(), managed_key)
        self.assertNotIn(managed_key, repr(self.manager.status()))
        self.manager.restore_openai()
        self.assertIsNone(self.manager.managed_deepseek_api_key())

    def test_invalid_external_backup_is_not_reported_or_used(self) -> None:
        backup = self.codex_home / "backup-deepseek" / "config.toml"
        backup.parent.mkdir(exist_ok=True)
        deepseek_config = (
            'model = "deepseek-v4-pro"\nmodel_provider = "deepseek"\n'
            'model_reasoning_effort = "high"\n'
        ).encode("utf-8")
        self.config_path.write_bytes(deepseek_config)
        self.models_path.write_bytes(self.asset.read_bytes())
        for backup_bytes in (
            b'model = "deepseek-v4-flash"\nmodel_provider = "deepseek"\n',
            b'model = "unterminated\n',
        ):
            with self.subTest(backup=backup_bytes):
                backup.write_bytes(backup_bytes)
                before = (self.config_path.read_bytes(), self.models_path.read_bytes())
                self.assertFalse(self.manager.status().external_backup_available)
                with self.assertRaises(ProviderRestoreUnavailableError):
                    self.manager.restore_openai()
                self.assertEqual(self.config_path.read_bytes(), before[0])
                self.assertEqual(self.models_path.read_bytes(), before[1])
                self.assertFalse(self.manager.state_path.exists())

    def test_arbitrary_existing_models_bytes_are_restored_exactly(self) -> None:
        for original_models in (b"not-json\n", b"\xff\xfe", b"[]\n"):
            with self.subTest(original_models=original_models):
                self.write_config('model = "gpt-5"\n')
                self.models_path.write_bytes(original_models)
                self.manager.activate_deepseek("sk-raw-models", DEEPSEEK_MODEL_PRO)
                self.manager.restore_openai()
                self.assertEqual(self.models_path.read_bytes(), original_models)

    def test_replacement_over_limit_is_rejected_before_any_mutation(self) -> None:
        prefix = b'model = "gpt-5"\n# '
        original_config = (
            prefix + (b"x" * (provider_config.MAX_CONFIG_BYTES - len(prefix) - 2)) + b"\n"
        )
        self.assertEqual(len(original_config), provider_config.MAX_CONFIG_BYTES - 1)
        self.config_path.write_bytes(original_config)

        with self.assertRaisesRegex(ProviderConfigError, "config.toml.*过大"):
            self.manager.activate_deepseek("sk-config-limit", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertFalse(self.models_path.exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse(self.manager.transaction_path.exists())

    def test_protected_state_over_limit_is_rejected_before_any_mutation(self) -> None:
        class ExpandingProtector(IdentityProtector):
            def protect(self, value: bytes) -> bytes:
                return b"x" * (provider_config.MAX_STATE_BYTES + 1)

        manager = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=ExpandingProtector(),
        )
        self.write_config('model = "gpt-5"\n')
        original_config = self.config_path.read_bytes()

        with self.assertRaisesRegex(ProviderConfigError, "provider_state.dat.*过大"):
            manager.activate_deepseek("sk-state-limit", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertFalse(self.models_path.exists())
        self.assertFalse(manager.state_path.exists())
        self.assertFalse(manager.transaction_path.exists())

    def test_bounded_read_rejects_growth_after_stat(self) -> None:
        target = self.root / "growing.bin"
        target.write_bytes(b"x")
        real_open = Path.open

        class GrowingReader:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b"x" * 18

        def fake_open(path, *args, **kwargs):
            if Path(path) == target:
                return GrowingReader()
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", fake_open):
            with self.assertRaisesRegex(ProviderConfigError, "过大"):
                provider_config._read_optional_bytes(target, 16, "增长文件")

    def test_bounded_read_accepts_exact_limit_and_rejects_limit_plus_one(self) -> None:
        target = self.root / "bounded.bin"
        exact = b"x" * 16
        target.write_bytes(exact)

        self.assertEqual(
            provider_config._read_optional_bytes(target, 16, "bounded file"),
            exact,
        )

        target.write_bytes(exact + b"x")
        with self.assertRaises(ProviderConfigError):
            provider_config._read_optional_bytes(target, 16, "bounded file")

    def test_hard_exit_after_journal_before_first_target_clears_all_before_state(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b'{"models":[{"slug":"private"}]}\n'
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        original_replace = provider_config._guarded_replace_optional

        def exit_after_journal(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.transaction_path and value is not None:
                raise SystemExit(90)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_journal,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek(
                    "sk-crash-before-" + "first-target", DEEPSEEK_MODEL_PRO
                )

        self.assertTrue(self.manager.transaction_path.exists())
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())

        fresh = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        status = fresh.status()
        self.assertEqual(status.mode, "openai")
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(fresh.state_path.exists())
        self.assertFalse(fresh.transaction_path.exists())

    def test_activation_hard_exit_after_models_is_recovered_on_next_status(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b'{"models":[{"slug":"private"}]}\n'
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        original_replace = provider_config._guarded_replace_optional

        def hard_exit_after_models(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.models_path and value == self.asset.read_bytes():
                raise SystemExit(91)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=hard_exit_after_models,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-crash-activate", DEEPSEEK_MODEL_PRO)

        self.assertTrue(self.manager.transaction_path.exists())
        fresh = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        status = fresh.status()
        self.assertEqual(status.mode, "openai")
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(fresh.state_path.exists())
        self.assertFalse(fresh.transaction_path.exists())

    def test_disappearing_journal_rolls_back_owned_partial_writes(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b'{"models":[{"slug":"private"}]}\n'
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        original_check = self.manager._transaction_is_current
        calls = 0

        def remove_journal_before_config(expected):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.manager.transaction_path.unlink()
                return False
            return original_check(expected)

        with mock.patch.object(
            self.manager,
            "_transaction_is_current",
            side_effect=remove_journal_before_config,
        ):
            with self.assertRaises(ProviderTransactionRecoveryError):
                self.manager.activate_deepseek("sk-journal-" + "removed", DEEPSEEK_MODEL_PRO)

        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse(self.manager.transaction_path.exists())
        status = self.manager.status()
        self.assertEqual(status.mode, "openai")
        self.assertFalse(status.managed)

        self.manager.activate_deepseek("sk-journal-" + "retry", DEEPSEEK_MODEL_PRO)
        self.manager.restore_openai()
        self.assertEqual(self.models_path.read_bytes(), original_models)

    def test_restore_hard_exit_after_config_is_recovered_on_next_status(self) -> None:
        self.write_config('model = "gpt-original"\n')
        self.models_path.write_bytes(b'{"models":[{"slug":"private"}]}\n')
        self.manager.activate_deepseek("sk-crash-restore", DEEPSEEK_MODEL_PRO)
        deepseek = (
            self.config_path.read_bytes(),
            self.models_path.read_bytes(),
            self.manager.state_path.read_bytes(),
        )
        original_replace = provider_config._guarded_replace_optional

        def hard_exit_after_config(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.config_path and value != deepseek[0]:
                raise SystemExit(92)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=hard_exit_after_config,
        ):
            with self.assertRaises(SystemExit):
                self.manager.restore_openai()

        fresh = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        status = fresh.status()
        self.assertEqual(status.mode, "deepseek")
        self.assertEqual(status.active_model, DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.config_path.read_bytes(), deepseek[0])
        self.assertEqual(self.models_path.read_bytes(), deepseek[1])
        self.assertEqual(fresh.state_path.read_bytes(), deepseek[2])
        self.assertFalse(fresh.transaction_path.exists())

    def test_restore_hard_exit_after_models_recovers_complete_deepseek_state(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b'{"models":[{"slug":"private"}]}\n'
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        self.manager.activate_deepseek("sk-crash-restore-" + "models", DEEPSEEK_MODEL_PRO)
        deepseek = (
            self.config_path.read_bytes(),
            self.models_path.read_bytes(),
            self.manager.state_path.read_bytes(),
        )
        original_replace = provider_config._guarded_replace_optional

        def exit_after_models(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.models_path and value == original_models:
                raise SystemExit(101)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_models,
        ):
            with self.assertRaises(SystemExit):
                self.manager.restore_openai()

        self.assertTrue(self.manager.transaction_path.exists())
        fresh = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        status = fresh.status()
        self.assertEqual(status.mode, "deepseek")
        self.assertEqual(status.active_model, DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.config_path.read_bytes(), deepseek[0])
        self.assertEqual(self.models_path.read_bytes(), deepseek[1])
        self.assertEqual(fresh.state_path.read_bytes(), deepseek[2])
        self.assertFalse(fresh.transaction_path.exists())

    def test_model_and_key_update_hard_exit_recovery_matrix(self) -> None:
        self.write_config('model = "gpt-original"\n')
        self.manager.activate_deepseek("sk-old-managed", DEEPSEEK_MODEL_FLASH)
        flash_snapshot = (
            self.config_path.read_bytes(),
            self.models_path.read_bytes(),
            self.manager.state_path.read_bytes(),
        )
        original_replace = provider_config._guarded_replace_optional

        def exit_after_state(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path and value != flash_snapshot[2]:
                raise SystemExit(95)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_state,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-new-managed", DEEPSEEK_MODEL_PRO)

        fresh = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        self.assertEqual(fresh.status().active_model, DEEPSEEK_MODEL_FLASH)
        self.assertEqual(fresh.managed_deepseek_api_key(), "sk-old-managed")
        self.assertEqual(self.config_path.read_bytes(), flash_snapshot[0])
        self.assertEqual(self.models_path.read_bytes(), flash_snapshot[1])
        self.assertEqual(fresh.state_path.read_bytes(), flash_snapshot[2])

        def exit_after_config(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.config_path and value != flash_snapshot[0]:
                raise SystemExit(96)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_config,
        ):
            with self.assertRaises(SystemExit):
                fresh.activate_deepseek("sk-new-managed", DEEPSEEK_MODEL_PRO)

        finalized = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        self.assertEqual(finalized.status().active_model, DEEPSEEK_MODEL_PRO)
        self.assertEqual(finalized.managed_deepseek_api_key(), "sk-new-managed")
        self.assertEqual(str(self.parse_config()["model_reasoning_effort"]), "max")
        finalized.restore_openai()
        self.assertEqual(str(self.parse_config()["model"]), "gpt-original")

    def test_restore_completed_before_journal_cleanup_finalizes_openai(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b"private-model-catalog\n"
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        self.manager.activate_deepseek("sk-restore-final", DEEPSEEK_MODEL_PRO)
        original_replace = provider_config._guarded_replace_optional

        def exit_after_state_delete(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path and value is None:
                raise SystemExit(97)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_state_delete,
        ):
            with self.assertRaises(SystemExit):
                self.manager.restore_openai()

        self.assertTrue(self.manager.transaction_path.exists())
        self.assertEqual(self.manager.status().mode, "openai")
        self.assertEqual(
            tomlkit.parse(self.config_path.read_text(encoding="utf-8")).unwrap(),
            tomlkit.parse(original_config.decode("utf-8")).unwrap(),
        )
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse(self.manager.transaction_path.exists())

    def test_recovery_can_resume_after_another_hard_exit(self) -> None:
        self.write_config('model = "gpt-original"\n')
        self.models_path.write_bytes(b"original-models\n")
        original_replace = provider_config._guarded_replace_optional

        def exit_after_models(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.models_path and value == self.asset.read_bytes():
                raise SystemExit(98)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_models,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-recovery-repeat", DEEPSEEK_MODEL_PRO)

        crashed_once = False

        def exit_during_recovery(path, expected, value, limit, label):
            nonlocal crashed_once
            result = original_replace(path, expected, value, limit, label)
            if (
                Path(path) == self.models_path
                and value == b"original-models\n"
                and not crashed_once
            ):
                crashed_once = True
                raise SystemExit(99)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_during_recovery,
        ):
            with self.assertRaises(SystemExit):
                self.manager.status()

        self.assertTrue(self.manager.transaction_path.exists())
        self.assertEqual(self.manager.status().mode, "openai")
        self.assertEqual(self.models_path.read_bytes(), b"original-models\n")
        self.assertFalse(self.manager.transaction_path.exists())

    def test_transaction_journal_is_protected_and_corruption_fails_closed(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_replace = provider_config._guarded_replace_optional

        def exit_after_state(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path:
                raise SystemExit(100)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_state,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-secret-journal", DEEPSEEK_MODEL_PRO)

        protected = self.manager.transaction_path.read_bytes()
        self.assertNotIn(b"sk-secret-journal", protected)
        self.manager.transaction_path.write_bytes(b"corrupt-transaction")
        with self.assertRaises(ProviderTransactionRecoveryError):
            self.manager.status()
        self.assertEqual(self.manager.transaction_path.read_bytes(), b"corrupt-transaction")

    def test_transaction_decoder_rejects_incomplete_or_ambiguous_schema(self) -> None:
        changes = (
            (self.manager.state_path, None, b"state-after"),
            (self.models_path, b"models-before", b"models-after"),
            (self.config_path, b"config-before", b"config-after"),
        )
        protected = self.manager._encode_transaction("activate", changes)
        base_payload = json.loads(IdentityProtector().unprotect(protected).decode("utf-8"))

        mutations = {
            "extra top-level field": lambda value: value.update({"extra": True}),
            "missing guards": lambda value: value.pop("guards"),
            "unknown version": lambda value: value.update({"version": 99}),
            "unknown operation": lambda value: value.update({"operation": "migrate"}),
            "invalid created_at": lambda value: value.update({"created_at": "not-a-date"}),
            "state path contains nul": lambda value: value.update(
                {"state_path": "C:\\bad\x00path"}
            ),
            "foreign state path": lambda value: value.update(
                {"state_path": str(self.root / "other" / provider_config.STATE_FILE_NAME)}
            ),
            "missing state target": lambda value: value.__setitem__(
                "changes",
                [item for item in value["changes"] if item["target"] != "state"],
            ),
            "extra change field": lambda value: value["changes"][0].update({"unexpected": True}),
            "unknown target": lambda value: value["changes"][0].update({"target": "other"}),
            "invalid before type": lambda value: value["changes"][0].update({"before": 42}),
            "invalid before base64": lambda value: value["changes"][0].update({"before": "***"}),
            "invalid after digest": lambda value: value["changes"][0]["after"].update(
                {"sha256": "not-a-digest"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = json.loads(json.dumps(base_payload))
                mutate(payload)
                malformed = IdentityProtector().protect(
                    json.dumps(payload, separators=(",", ":")).encode("utf-8")
                )
                with self.assertRaises(ProviderTransactionRecoveryError):
                    self.manager._decode_transaction(malformed)

    def test_real_hard_exit_orphan_temp_is_removed_without_touching_near_misses(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b'{"models":[{"slug":"private"}]}\n'
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_exit_after_provider_config_temp_is_synced,
            args=(self.codex_home, self.state_directory, self.asset),
        )
        process.start()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("provider hard-exit probe did not terminate")
        self.assertEqual(process.exitcode, 77)

        owned = tuple(
            self.codex_home.glob(
                ".codex-account-switcher.config.toml.????????????????????????????????.tmp"
            )
        )
        self.assertEqual(len(owned), 1)
        secret = b"sk-" + b"orphan-provider"
        self.assertIn(secret, owned[0].read_bytes())
        near_short = self.codex_home / (".codex-account-switcher.config.toml." + "a" * 31 + ".tmp")
        near_other_target = self.codex_home / (
            ".codex-account-switcher.other.toml." + "b" * 32 + ".tmp"
        )
        near_old_namespace = self.codex_home / (".config.toml." + "c" * 32 + ".tmp")
        near_directory = self.codex_home / (
            ".codex-account-switcher.config.toml." + "d" * 32 + ".tmp"
        )
        for path in (near_short, near_other_target, near_old_namespace):
            path.write_bytes(b"keep")
        near_directory.mkdir()

        fresh = ProviderConfigManager(
            self.codex_home,
            self.state_directory,
            self.asset,
            protector=IdentityProtector(),
        )
        self.assertEqual(fresh.status().mode, "openai")

        self.assertFalse(owned[0].exists())
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(fresh.transaction_path.exists())
        for path in (near_short, near_other_target, near_old_namespace, near_directory):
            self.assertTrue(path.exists())
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes())

    def test_completed_targets_with_uncleared_journal_are_finalized(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_replace = provider_config._guarded_replace_optional

        def fail_journal_delete(path, expected, value, limit, label):
            if Path(path) == self.manager.transaction_path and value is None:
                raise SystemExit(93)
            return original_replace(path, expected, value, limit, label)

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=fail_journal_delete,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-finished", DEEPSEEK_MODEL_PRO)

        self.assertTrue(self.manager.transaction_path.exists())
        status = self.manager.status()
        self.assertEqual(status.mode, "deepseek")
        self.assertEqual(status.active_model, DEEPSEEK_MODEL_PRO)
        self.assertFalse(self.manager.transaction_path.exists())

    def test_external_change_after_crash_is_preserved_and_fails_closed(self) -> None:
        self.write_config('model = "gpt-5"\n')
        original_replace = provider_config._guarded_replace_optional

        def hard_exit_after_state(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path:
                raise SystemExit(94)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=hard_exit_after_state,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-external-crash", DEEPSEEK_MODEL_PRO)
        external_state = b"newer-external-state"
        self.manager.state_path.write_bytes(external_state)

        with self.assertRaises(ProviderTransactionRecoveryError):
            self.manager.status()

        self.assertEqual(self.manager.state_path.read_bytes(), external_state)
        self.assertTrue(self.manager.transaction_path.exists())

    def test_external_changed_config_or_models_fails_closed_for_all_public_operations(
        self,
    ) -> None:
        cases = (
            ("config", b'model = "gpt-external-after-crash"\n'),
            ("models", b'{"models":[{"slug":"external-after-crash"}]}\n'),
        )
        for target_name, external_value in cases:
            with self.subTest(target=target_name):
                case_root = self.root / target_name
                codex_home = case_root / ".codex"
                state_directory = case_root / "state"
                codex_home.mkdir(parents=True)
                manager = ProviderConfigManager(
                    codex_home,
                    state_directory,
                    self.asset,
                    protector=IdentityProtector(),
                )
                config_path = codex_home / "config.toml"
                models_path = codex_home / "models.json"
                original_config = b'model = "gpt-original"\n'
                original_models = b'{"models":[{"slug":"private"}]}\n'
                config_path.write_bytes(original_config)
                models_path.write_bytes(original_models)
                original_replace = provider_config._guarded_replace_optional

                def exit_after_state(path, expected, value, limit, label):
                    result = original_replace(path, expected, value, limit, label)
                    if Path(path) == manager.state_path and value is not None:
                        raise SystemExit(102)
                    return result

                with mock.patch(
                    "provider_config._guarded_replace_optional",
                    side_effect=exit_after_state,
                ):
                    with self.assertRaises(SystemExit):
                        manager.activate_deepseek(
                            "sk-external-" + "target-crash",
                            DEEPSEEK_MODEL_PRO,
                        )

                target_path = config_path if target_name == "config" else models_path
                target_path.write_bytes(external_value)
                journal = manager.transaction_path.read_bytes()

                operations = (
                    ("status", lambda candidate: candidate.status()),
                    (
                        "activate",
                        lambda candidate: candidate.activate_deepseek(
                            "sk-external-" + "target-retry",
                            DEEPSEEK_MODEL_FLASH,
                        ),
                    ),
                    ("restore", lambda candidate: candidate.restore_openai()),
                )
                for operation_name, operation in operations:
                    with self.subTest(target=target_name, operation=operation_name):
                        fresh = ProviderConfigManager(
                            codex_home,
                            state_directory,
                            self.asset,
                            protector=IdentityProtector(),
                        )
                        with self.assertRaises(ProviderTransactionRecoveryError):
                            operation(fresh)
                        self.assertEqual(target_path.read_bytes(), external_value)
                        self.assertEqual(manager.transaction_path.read_bytes(), journal)

                untouched_path = models_path if target_name == "config" else config_path
                untouched_value = original_models if target_name == "config" else original_config
                self.assertEqual(untouched_path.read_bytes(), untouched_value)
                self.assertFalse(manager.state_path.exists())

    def test_complete_journal_rejects_foreign_state_path_then_recovers_at_origin(self) -> None:
        original_config = b'model = "gpt-original"\n'
        original_models = b'{"models":[{"slug":"private"}]}\n'
        self.config_path.write_bytes(original_config)
        self.models_path.write_bytes(original_models)
        original_replace = provider_config._guarded_replace_optional

        def exit_after_state(path, expected, value, limit, label):
            result = original_replace(path, expected, value, limit, label)
            if Path(path) == self.manager.state_path and value is not None:
                raise SystemExit(103)
            return result

        with mock.patch(
            "provider_config._guarded_replace_optional",
            side_effect=exit_after_state,
        ):
            with self.assertRaises(SystemExit):
                self.manager.activate_deepseek("sk-portable-origin", DEEPSEEK_MODEL_PRO)

        journal = self.manager.transaction_path.read_bytes()
        origin_state = self.manager.state_path.read_bytes()
        foreign_state_directory = self.root / "copied-program-state"
        foreign = ProviderConfigManager(
            self.codex_home,
            foreign_state_directory,
            self.asset,
            protector=IdentityProtector(),
        )

        with self.assertRaises(ProviderTransactionRecoveryError):
            foreign.status()

        self.assertEqual(self.manager.transaction_path.read_bytes(), journal)
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertEqual(self.manager.state_path.read_bytes(), origin_state)
        self.assertFalse(foreign.state_path.exists())

        status = self.manager.status()
        self.assertEqual(status.mode, "openai")
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.models_path.read_bytes(), original_models)
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse(self.manager.transaction_path.exists())


if __name__ == "__main__":
    unittest.main()
