import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from codex_client import CodexClientError, RestartResult, StopResult
from provider_config import ProviderConfigError, ProviderStatus, ProviderSwitchResult


class StubRemarkDialog:
    results = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.result = self.results.pop(0)


class StubCodexClient:
    def __init__(self, result=None, stop_error=None, start_error=None) -> None:
        self.result = result or RestartResult(True, False, 2400)
        self.stop_error = stop_error
        self.start_error = start_error
        self.stop_calls = 0
        self.start_calls = 0

    @property
    def calls(self):
        return self.start_calls

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        return StopResult(self.result.was_running, self.result.forced_close)

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return self.result.launched_pid


class StubCredentialStore:
    def __init__(self, value="sk-saved-test-1234") -> None:
        self.value = value
        self.writes = []
        self.delete_calls = 0
        self.read_error = None
        self.write_error = None

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.value

    def write(self, value):
        if self.write_error is not None:
            raise self.write_error
        self.value = value
        self.writes.append(value)

    def delete(self):
        self.value = None
        self.delete_calls += 1


class StubProviderManager:
    def __init__(self, mode="openai", events=None, active_model=None) -> None:
        self.mode = mode
        self.events = events if events is not None else []
        self.conflict = False
        self.activate_error = None
        self.restore_error = None
        self.last_key = None
        self.active_model = active_model
        self.last_model = None
        self.managed = None
        self.external_backup_available = False
        self.managed_key = None

    def status(self):
        active_model = self.active_model
        if self.mode == "deepseek" and active_model is None:
            active_model = app_module.DEEPSEEK_MODEL_FLASH
        return ProviderStatus(
            self.mode,
            (
                self.mode == "deepseek"
                if self.managed is None
                else self.managed and self.mode == "deepseek"
            ),
            self.conflict,
            self.external_backup_available,
            active_model,
        )

    def activate_deepseek(self, api_key, model_slug=app_module.DEFAULT_DEEPSEEK_MODEL):
        self.events.append("activate")
        self.last_key = api_key
        self.last_model = model_slug
        if self.activate_error is not None:
            raise self.activate_error
        changed = self.mode != "deepseek" or self.active_model != model_slug
        self.mode = "deepseek"
        self.managed = True
        self.active_model = model_slug
        return ProviderSwitchResult(changed, "deepseek", model_slug)

    def restore_openai(self):
        self.events.append("restore")
        if self.restore_error is not None:
            raise self.restore_error
        changed = self.mode == "deepseek"
        self.mode = "openai"
        self.managed = False
        self.active_model = None
        return ProviderSwitchResult(changed, "openai", None)

    def managed_deepseek_api_key(self):
        return self.managed_key


class AccountSwitcherActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary_directory.name)
        self.original_app_directory = app_module.app_directory
        self.original_remark_dialog = app_module.RemarkDialog
        app_module.app_directory = lambda: self.root_path / "app"
        app_module.RemarkDialog = StubRemarkDialog

        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.temporary_directory.cleanup()
            self.skipTest("Tk is not available: {}".format(exc))
        self.root.withdraw()
        self.codex_client = StubCodexClient()
        self.provider_manager = StubProviderManager()
        self.credential_store = StubCredentialStore()
        self.application = app_module.AccountSwitcherApp(
            self.root,
            self.codex_client,
            provider_manager=self.provider_manager,
            credential_store=self.credential_store,
            codex_home=self.root_path / ".codex",
        )
        self.notices = []
        self.application.show_notice = lambda title, message, kind="info": self.notices.append(
            (title, message, kind)
        )
        self.confirms = []
        self.application.show_confirm = self.accept_confirm
        StubRemarkDialog.results = []
        # Drain the ttk theme-change notification queued while the interface is
        # constructed so it cannot leak into the next short-lived test root.
        self.root.update_idletasks()

    def tearDown(self) -> None:
        app_module.app_directory = self.original_app_directory
        app_module.RemarkDialog = self.original_remark_dialog
        if hasattr(self, "root"):
            self.root.update_idletasks()
            self.root.destroy()
        self.temporary_directory.cleanup()

    def write_current_auth(self, token: str) -> None:
        self.application.target_auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.application.target_auth_path.write_text(
            json.dumps({"token": token}),
            encoding="utf-8",
        )

    @staticmethod
    def managed_auth(account_id: str, generation: str):
        return {
            "auth_mode": "chatgpt",
            "last_refresh": "2026-08-0{}T00:00:00Z".format(generation),
            "OPENAI_API_KEY": None,
            "tokens": {
                "account_id": account_id,
                "access_token": "access-{}".format(generation),
                "id_token": "identity-{}".format(generation),
                "refresh_token": "refresh-{}".format(generation),
            },
        }

    def write_current_managed_auth(self, account_id: str, generation: str) -> None:
        self.application.target_auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.application.target_auth_path.write_text(
            json.dumps(self.managed_auth(account_id, generation)),
            encoding="utf-8",
        )

    def accept_confirm(self, *args, **kwargs) -> bool:
        self.confirms.append((args, kwargs))
        return True

    def wait_for_restart(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while self.application.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.application.busy, "Codex restart worker did not finish")

    def test_full_account_management_flow(self) -> None:
        self.write_current_auth("first")
        StubRemarkDialog.results.append("工作账号")

        self.application.add_from_current()

        self.assertEqual(len(self.application.profiles), 1)
        profile = self.application.profiles[0]
        self.assertEqual(profile.remark, "工作账号")
        self.assertTrue(self.application.store.profile_path(profile).is_file())
        displayed_values = self.application.tree.item(profile.profile_id, "values")
        self.assertTrue(displayed_values[0].startswith(app_module.TABLE_CELL_INSET))
        self.assertTrue(displayed_values[2].startswith(app_module.TABLE_CELL_INSET))

        self.application.add_from_current()
        self.assertEqual(len(self.application.profiles), 1)
        self.assertEqual(self.notices[-1][0], "账号已存在")

        StubRemarkDialog.results.append("工作账号（新）")
        self.application.rename_selected_profile()
        renamed = self.application.profiles[0]
        self.assertEqual(renamed.remark, "工作账号（新）")

        self.write_current_auth("refreshed")
        self.application.update_selected_profile()
        updated = self.application.profiles[0]
        self.assertEqual(
            self.application.store.profile_path(updated).read_bytes(),
            self.application.target_auth_path.read_bytes(),
        )

        self.write_current_auth("different-current")
        self.application.switch_selected_profile()
        self.wait_for_restart()
        switch_confirm = next(item for item in self.confirms if item[0][0] == "切换账号")
        self.assertIn("正在运行的任务可能会中断", switch_confirm[0][1])
        self.assertEqual(
            self.application.target_auth_path.read_bytes(),
            self.application.store.profile_path(updated).read_bytes(),
        )

        active_bytes = self.application.target_auth_path.read_bytes()
        self.application.remove_selected_profile()
        self.assertEqual(self.application.profiles, [])
        self.assertEqual(self.application.target_auth_path.read_bytes(), active_bytes)
        self.assertEqual(self.notices[-1][0], "账号已移除")

        self.assertEqual(self.codex_client.calls, 1)

    def test_provider_switches_both_directions_and_restarts_codex(self) -> None:
        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.mode, "deepseek")
        self.assertEqual(self.provider_manager.last_model, app_module.DEEPSEEK_MODEL_FLASH)
        self.assertEqual(self.provider_manager.last_key, self.credential_store.value)
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "已切换到 DeepSeek V4 Flash")

        self.application.restore_openai_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.mode, "openai")
        self.assertEqual(self.codex_client.stop_calls, 2)
        self.assertEqual(self.codex_client.start_calls, 2)
        self.assertEqual(self.notices[-1][0], "已切回 OpenAI")

    def test_selected_pro_is_passed_to_provider_and_named_in_messages(self) -> None:
        self.application._select_deepseek_model(app_module.DEEPSEEK_MODEL_PRO)

        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.last_model, "deepseek-v4-pro")
        self.assertEqual(self.provider_manager.active_model, "deepseek-v4-pro")
        self.assertIn("V4 Pro", self.confirms[-1][0][1])
        self.assertEqual(self.notices[-1][0], "已切换到 DeepSeek V4 Pro")
        self.assertEqual(
            self.application.provider_active_model_value.cget("text"),
            "V4 Pro",
        )

    def test_openai_status_refresh_preserves_user_model_selection(self) -> None:
        self.application._select_deepseek_model(app_module.DEEPSEEK_MODEL_PRO)

        self.application.refresh_provider_status(show_error=False)

        self.assertEqual(
            self.application.selected_deepseek_model,
            app_module.DEEPSEEK_MODEL_PRO,
        )
        self.assertIn("V4 Pro", self.application.provider_action_button.cget("text"))

    def test_deepseek_model_switch_does_not_restore_openai(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_FLASH
        self.application.refresh_provider_status(show_error=False)
        self.application._select_deepseek_model(app_module.DEEPSEEK_MODEL_PRO)

        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.events, ["activate"])
        self.assertEqual(self.provider_manager.active_model, app_module.DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)

    def test_model_and_restore_buttons_are_wired_to_their_exact_actions(self) -> None:
        pro_button = self.application.provider_model_buttons[app_module.DEEPSEEK_MODEL_PRO]
        flash_button = self.application.provider_model_buttons[app_module.DEEPSEEK_MODEL_FLASH]
        pro_button.invoke()
        self.assertEqual(
            self.application.selected_deepseek_model,
            app_module.DEEPSEEK_MODEL_PRO,
        )
        flash_button.invoke()
        self.assertEqual(
            self.application.selected_deepseek_model,
            app_module.DEEPSEEK_MODEL_FLASH,
        )
        pro_button.invoke()
        self.application.provider_action_button.invoke()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.events, ["activate"])
        self.assertEqual(self.provider_manager.last_model, app_module.DEEPSEEK_MODEL_PRO)

        self.application.provider_openai_button.invoke()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.events, ["activate", "restore"])
        self.assertEqual(self.provider_manager.mode, "openai")

    def test_api_key_update_preserves_locked_active_model(self) -> None:
        for active_model, pending_model in (
            (app_module.DEEPSEEK_MODEL_FLASH, app_module.DEEPSEEK_MODEL_PRO),
            (app_module.DEEPSEEK_MODEL_PRO, app_module.DEEPSEEK_MODEL_FLASH),
        ):
            with self.subTest(active_model=active_model):
                self.provider_manager.mode = "deepseek"
                self.provider_manager.managed = True
                self.provider_manager.active_model = active_model
                self.application.selected_deepseek_model = pending_model
                self.credential_store.value = "sk-before-update"

                self.application._begin_provider_switch(
                    "deepseek",
                    "sk-after-update",
                    "update_key",
                    target_model=pending_model,
                    progress_button=self.application.provider_key_button,
                )
                self.wait_for_restart()

                self.assertEqual(self.provider_manager.last_model, active_model)
                self.assertEqual(self.provider_manager.active_model, active_model)
                self.assertEqual(self.credential_store.value, "sk-after-update")
                self.assertIn(
                    app_module.DEEPSEEK_MODEL_LABELS[active_model],
                    self.notices[-1][1],
                )

    def test_key_cas_runs_before_managed_credential_reconciliation(self) -> None:
        active_key = "sk-active-" + "authority"
        newer_key = "sk-other-window-" + "value"
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_PRO
        self.provider_manager.managed_key = active_key
        self.credential_store.value = newer_key

        self.application._begin_provider_switch(
            "deepseek",
            "sk-dialog-update",
            "update_key",
            target_model=None,
            expected_key=active_key,
        )
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, newer_key)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.provider_manager.events, [])
        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertIn("其他窗口发生变化", self.notices[-1][1])

    def test_managed_model_switch_uses_active_config_key_as_authority(self) -> None:
        active_key = "sk-active-" + "authority"
        stale_saved_key = "sk-stale-saved-" + "value"
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_FLASH
        self.provider_manager.managed_key = active_key
        self.credential_store.value = stale_saved_key

        self.application._begin_provider_switch(
            "deepseek",
            stale_saved_key,
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
            expected_key=stale_saved_key,
        )
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.active_model, app_module.DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.provider_manager.last_key, active_key)
        self.assertEqual(self.credential_store.value, active_key)
        self.assertEqual(self.credential_store.writes, [active_key])
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)

    def test_provider_failure_preserves_newer_external_credential(self) -> None:
        original_key = self.credential_store.value
        replacement_key = "sk-requested-" + "replacement"
        newer_key = "sk-other-window-" + "newer"

        def fail_after_external_credential_write(_api_key, _model_slug):
            self.credential_store.value = newer_key
            raise ProviderConfigError("模拟配置失败")

        self.provider_manager.activate_deepseek = fail_after_external_credential_write
        self.application._begin_provider_switch(
            "deepseek",
            replacement_key,
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
            expected_key=original_key,
        )
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, newer_key)
        self.assertEqual(self.credential_store.writes, [replacement_key])
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertIn("已保留较新的凭据", self.notices[-1][1])

    def test_invalid_target_mode_fails_before_stopping_codex(self) -> None:
        self.application._begin_provider_switch("invalid", None, "switch")
        self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.provider_manager.events, [])
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("不支持的目标模型服务", self.notices[-1][1])

    def test_restore_rechecks_locked_status_and_skips_an_external_noop(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_PRO
        original_status = self.provider_manager.status

        def status_changed_while_waiting():
            if threading.current_thread().name == "ProviderSwitchWorker":
                return ProviderStatus("openai", False, False, False, None)
            return original_status()

        self.provider_manager.status = status_changed_while_waiting
        self.application.refresh_provider_status(show_error=False)
        self.assertEqual(self.application.provider_status.mode, "deepseek")

        self.application._begin_provider_switch("openai", None, "switch")
        self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.provider_manager.events, [])
        self.assertEqual(self.notices[-1][0], "无需切换")

    def test_locked_status_conflict_aborts_before_stop_or_credential_write(self) -> None:
        original_status = self.provider_manager.status

        def status_changed_while_waiting():
            if threading.current_thread().name == "ProviderSwitchWorker":
                return ProviderStatus(
                    "deepseek",
                    True,
                    True,
                    False,
                    app_module.DEEPSEEK_MODEL_FLASH,
                )
            return original_status()

        self.provider_manager.status = status_changed_while_waiting
        self.application.refresh_provider_status(show_error=False)

        self.application._begin_provider_switch(
            "deepseek",
            "sk-after-race",
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
        )
        self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.provider_manager.events, [])
        self.assertIn("外部修改", self.notices[-1][1])

    def test_invalid_model_fails_before_stopping_or_writing_credential(self) -> None:
        original_key = self.credential_store.value

        self.application._begin_provider_switch(
            "deepseek",
            "sk-invalid-model",
            "switch",
            target_model="deepseek-4v-pro",
        )
        self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.credential_store.value, original_key)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.provider_manager.events, [])
        self.assertIn("不受支持", self.notices[-1][1])

    def test_unknown_active_model_disables_model_overwrite(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = False
        self.provider_manager.external_backup_available = True
        self.provider_manager.active_model = "private-deepseek"

        self.application.refresh_provider_status(show_error=False)

        self.assertEqual(self.application.provider_badge.cget("text"), "模型未知")
        self.assertEqual(str(self.application.provider_action_button.cget("state")), "disabled")
        self.assertEqual(str(self.application.provider_key_button.cget("state")), "disabled")
        self.assertEqual(str(self.application.provider_openai_button.cget("state")), "normal")
        for button in self.application.provider_model_buttons.values():
            self.assertEqual(str(button.cget("state")), "disabled")

    def test_managed_unknown_model_still_names_safe_openai_recovery(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.external_backup_available = False
        self.provider_manager.active_model = "future-deepseek-model"

        self.application.refresh_provider_status(show_error=False)

        self.assertIn("只能安全切回 OpenAI", self.application.provider_hint.cget("text"))
        self.assertEqual(str(self.application.provider_action_button.cget("state")), "disabled")
        self.assertEqual(str(self.application.provider_openai_button.cget("state")), "normal")

    def test_provider_restart_failure_keeps_selected_model_and_names_it(self) -> None:
        self.application.codex_client = StubCodexClient(
            start_error=CodexClientError("模拟启动失败")
        )
        self.application._select_deepseek_model(app_module.DEEPSEEK_MODEL_PRO)

        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.active_model, app_module.DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.notices[-1][0], "配置已更新")
        self.assertIn("DeepSeek V4 Pro", self.notices[-1][1])
        self.assertIn("请手动启动 Codex", self.notices[-1][1])

    def test_provider_switch_uses_the_same_cross_process_codex_lock(self) -> None:
        self.application.store.lock_timeout = 0.1

        with self.application.store.codex_operation_guard(self.application.target_auth_path):
            self.application.toggle_provider()
            self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.provider_manager.mode, "openai")
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("Codex 操作锁", self.notices[-1][1])

    def test_provider_failure_does_not_start_a_client_that_was_not_running(self) -> None:
        self.application.codex_client = StubCodexClient(result=RestartResult(False, False, 2400))
        self.provider_manager.activate_error = ProviderConfigError("模拟配置失败")

        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.application.codex_client.stop_calls, 1)
        self.assertEqual(self.application.codex_client.start_calls, 0)
        self.assertEqual(self.notices[-1][0], "切换失败")

    def test_provider_guard_exit_failure_reports_committed_warning_once(self) -> None:
        original_guard = self.application.store.codex_operation_guard

        class ExitFailureGuard:
            def __enter__(_self):
                _self.inner = original_guard(self.application.target_auth_path)
                return _self.inner.__enter__()

            def __exit__(_self, exc_type, exc, traceback):
                _self.inner.__exit__(exc_type, exc, traceback)
                raise app_module.AccountStoreError("模拟锁释放失败")

        self.application.store.codex_operation_guard = lambda _path: ExitFailureGuard()
        self.application._select_deepseek_model(app_module.DEEPSEEK_MODEL_PRO)
        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.mode, "deepseek")
        self.assertEqual(self.provider_manager.active_model, app_module.DEEPSEEK_MODEL_PRO)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "已切换到 DeepSeek V4 Pro")
        self.assertEqual(self.notices[-1][2], "warning")
        self.assertIn("配置已提交", self.notices[-1][1])
        self.assertTrue(self.application.provider_queue.empty())

    def test_account_switch_from_deepseek_restores_openai_before_start(self) -> None:
        events = []
        self.provider_manager.mode = "deepseek"
        self.provider_manager.events = events
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("工作账号")
        self.application.add_from_current()
        self.write_current_auth("current")

        class OrderedClient:
            def stop(_self):
                events.append("stop")
                return StopResult(True, False)

            def start(_self):
                events.append("start")
                return 2400

        original_switch = self.application.store.switch_profile

        def tracked_switch(*args, **kwargs):
            events.append("auth")
            return original_switch(*args, **kwargs)

        self.application.codex_client = OrderedClient()
        self.application.store.switch_profile = tracked_switch
        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(events, ["stop", "auth", "restore", "start"])
        self.assertEqual(self.provider_manager.mode, "openai")
        self.assertIn("已切回 OpenAI", self.notices[-1][1])

    def test_account_switch_aborts_if_provider_changes_after_confirmation(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("工作账号")
        self.application.add_from_current()
        self.write_current_auth("current")
        before = self.application.target_auth_path.read_bytes()
        original_status = self.provider_manager.status

        def status_changed_while_waiting():
            if threading.current_thread().name == "CodexRestartWorker":
                return ProviderStatus(
                    "deepseek",
                    True,
                    False,
                    False,
                    app_module.DEEPSEEK_MODEL_PRO,
                )
            return original_status()

        self.provider_manager.status = status_changed_while_waiting

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), before)
        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.provider_manager.events, [])
        self.assertIn("未经确认", self.notices[-1][1])

    def test_account_switch_rechecks_recovery_baseline_before_stop(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_FLASH
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("工作账号")
        self.application.add_from_current()
        self.write_current_auth("current")
        before = self.application.target_auth_path.read_bytes()
        original_status = self.provider_manager.status

        def baseline_removed_while_waiting():
            if threading.current_thread().name == "CodexRestartWorker":
                return ProviderStatus(
                    "deepseek",
                    False,
                    False,
                    False,
                    app_module.DEEPSEEK_MODEL_FLASH,
                )
            return original_status()

        self.provider_manager.status = baseline_removed_while_waiting

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), before)
        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.provider_manager.events, [])
        self.assertIn("OpenAI 配置备份", self.notices[-1][1])

    def test_provider_failure_restores_previous_credential_and_restarts_client(self) -> None:
        self.provider_manager.activate_error = ProviderConfigError("模拟配置失败")

        self.application._begin_provider_switch(
            "deepseek",
            "sk-new-test-5678",
            "switch",
        )
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, "sk-saved-test-1234")
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("模拟配置失败", self.notices[-1][1])

    def test_provider_stop_failure_leaves_key_and_model_untouched(self) -> None:
        self.application.codex_client = StubCodexClient(stop_error=CodexClientError("模拟关闭失败"))
        original_key = self.credential_store.value

        self.application._begin_provider_switch(
            "deepseek",
            "sk-new-after-stop",
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
        )
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, original_key)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.provider_manager.events, [])
        self.assertEqual(self.provider_manager.mode, "openai")
        self.assertEqual(self.application.codex_client.start_calls, 0)
        self.assertIn("模拟关闭失败", self.notices[-1][1])

    def test_credential_write_failure_does_not_activate_provider(self) -> None:
        self.credential_store.write_error = app_module.CredentialStoreError("模拟凭据写入失败")
        original_key = self.credential_store.value

        self.application._begin_provider_switch(
            "deepseek",
            "sk-new-write-fail",
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
        )
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.events, [])
        self.assertEqual(self.provider_manager.mode, "openai")
        self.assertEqual(self.credential_store.value, original_key)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertIn("模拟凭据写入失败", self.notices[-1][1])

    def test_managed_key_reconcile_failure_is_reported_before_stop(self) -> None:
        active_key = "sk-active-" + "authority"
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_FLASH
        self.provider_manager.managed_key = active_key
        self.credential_store.value = "sk-stale-test"
        self.credential_store.write_error = app_module.CredentialStoreError("模拟活动凭据对账失败")

        self.application._begin_provider_switch(
            "deepseek",
            "sk-next-test",
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
        )
        self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.provider_manager.events, [])
        self.assertIn("模拟活动凭据对账失败", self.notices[-1][1])
        self.assertNotIn(active_key, self.notices[-1][1])

    def test_canonical_home_migrates_v14_credential_through_app_wiring(self) -> None:
        legacy = StubCredentialStore("sk-legacy-test")
        marker = StubCredentialStore(None)
        self.credential_store.value = None
        canonical_home = (self.root_path / ".codex").resolve()
        self.application.codex_home = canonical_home
        self.application.target_auth_path = canonical_home / "auth.json"

        with mock.patch.object(app_module.Path, "home", return_value=self.root_path), mock.patch(
            "app.WindowsCredentialStore", side_effect=(legacy, marker)
        ) as store_factory:
            self.application._migrate_and_reconcile_credentials()

        self.assertEqual(
            store_factory.call_args_list,
            [
                mock.call(app_module.DEFAULT_CREDENTIAL_TARGET),
                mock.call(app_module.legacy_migration_marker_target_for_codex_home(canonical_home)),
            ],
        )
        self.assertEqual(self.credential_store.value, "sk-legacy-test")
        self.assertEqual(legacy.value, "sk-legacy-test")
        self.assertEqual(marker.value, "sk-migrated-v15")

    def test_custom_home_reconciles_managed_key_without_reading_global_legacy(self) -> None:
        active_key = "sk-managed-" + "config"
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_PRO
        self.provider_manager.managed_key = active_key
        self.credential_store.value = "sk-after-hard-exit"

        with mock.patch(
            "app.WindowsCredentialStore",
            side_effect=AssertionError("custom homes must not read the global legacy target"),
        ) as legacy_factory:
            self.application._migrate_and_reconcile_credentials()

        legacy_factory.assert_not_called()
        self.assertEqual(self.credential_store.value, active_key)

    def test_transient_credential_migration_failure_clears_after_refresh(self) -> None:
        attempts = []
        self.application.credential_migration_error = "首次迁移失败"

        def retry_migration():
            attempts.append("retry")

        self.application._migrate_and_reconcile_credentials = retry_migration
        self.application.refresh_provider_status(show_error=False)

        self.assertEqual(attempts, ["retry"])
        self.assertEqual(self.application.credential_migration_error, "")
        self.assertEqual(self.application.credential_status_error, "")
        self.assertEqual(str(self.application.provider_action_button.cget("state")), "normal")

    def test_persistent_credential_migration_failure_remains_visible(self) -> None:
        failure = "旧版 API Key 迁移暂未完成"
        self.application.credential_migration_error = failure

        def fail_migration():
            raise app_module.CredentialStoreError(failure)

        self.application._migrate_and_reconcile_credentials = fail_migration
        self.application.refresh_provider_status(show_error=False)

        self.assertEqual(self.application.credential_migration_error, failure)
        self.assertEqual(self.application.credential_status_error, failure)
        self.assertIn(failure, self.application.provider_hint.cget("text"))
        self.assertEqual(str(self.application.provider_action_button.cget("state")), "disabled")

    def test_direct_openai_restore_failure_keeps_deepseek_state(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_PRO
        self.provider_manager.restore_error = ProviderConfigError("模拟直接恢复失败")
        original_key = self.credential_store.value
        self.application.refresh_provider_status(show_error=False)

        self.application.provider_openai_button.invoke()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.events, ["restore"])
        self.assertEqual(self.provider_manager.mode, "deepseek")
        self.assertEqual(
            self.provider_manager.active_model,
            app_module.DEEPSEEK_MODEL_PRO,
        )
        self.assertEqual(self.credential_store.value, original_key)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertIn("模拟直接恢复失败", self.notices[-1][1])

    def test_unreadable_previous_key_aborts_before_stopping_codex(self) -> None:
        self.credential_store.read_error = app_module.CredentialStoreError("模拟凭据读取失败")

        self.application._begin_provider_switch(
            "deepseek",
            "sk-new-read-fail",
            "switch",
            target_model=app_module.DEEPSEEK_MODEL_PRO,
        )
        self.wait_for_restart()

        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.provider_manager.events, [])
        self.assertIn("关闭 Codex 前取消", self.notices[-1][1])

    def test_saved_api_key_can_be_removed_while_openai_is_active(self) -> None:
        original_dialog = app_module.ApiKeyDialog

        class DeleteDialog:
            def __init__(self, *_args, **_kwargs):
                self.result = None
                self.deleted = True

        app_module.ApiKeyDialog = DeleteDialog
        try:
            self.application.configure_deepseek_key()
        finally:
            app_module.ApiKeyDialog = original_dialog
        self.wait_for_restart()

        self.assertIsNone(self.credential_store.value)
        self.assertEqual(self.credential_store.delete_calls, 1)
        self.assertEqual(self.notices[-1][0], "API Key 已移除")

    def test_key_delete_aborts_if_another_instance_activates_deepseek(self) -> None:
        original_dialog = app_module.ApiKeyDialog
        application = self.application

        class RacingDeleteDialog:
            def __init__(self, *_args, **_kwargs):
                application.provider_manager.mode = "deepseek"
                application.provider_manager.active_model = app_module.DEEPSEEK_MODEL_PRO
                self.result = None
                self.deleted = True

        app_module.ApiKeyDialog = RacingDeleteDialog
        try:
            self.application.configure_deepseek_key()
        finally:
            app_module.ApiKeyDialog = original_dialog
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, "sk-saved-test-1234")
        self.assertEqual(self.credential_store.delete_calls, 0)
        self.assertEqual(self.notices[-1][0], "移除失败")
        self.assertIn("活动配置不一致", self.notices[-1][1])

    def test_key_save_aborts_if_another_instance_activates_deepseek(self) -> None:
        original_dialog = app_module.ApiKeyDialog
        application = self.application

        class RacingSaveDialog:
            def __init__(self, *_args, **_kwargs):
                application.provider_manager.mode = "deepseek"
                application.provider_manager.active_model = app_module.DEEPSEEK_MODEL_FLASH
                self.result = "sk-racing-value"
                self.deleted = False

        app_module.ApiKeyDialog = RacingSaveDialog
        try:
            self.application.configure_deepseek_key()
        finally:
            app_module.ApiKeyDialog = original_dialog
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, "sk-saved-test-1234")
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.notices[-1][0], "保存失败")
        self.assertIn("活动配置不一致", self.notices[-1][1])

    def test_key_delete_preserves_a_newer_credential_from_another_window(self) -> None:
        original_dialog = app_module.ApiKeyDialog
        credential_store = self.credential_store

        class RacingDeleteDialog:
            def __init__(self, *_args, **_kwargs):
                credential_store.value = "sk-other-window"
                self.result = None
                self.deleted = True

        app_module.ApiKeyDialog = RacingDeleteDialog
        try:
            self.application.configure_deepseek_key()
        finally:
            app_module.ApiKeyDialog = original_dialog
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, "sk-other-window")
        self.assertEqual(self.credential_store.delete_calls, 0)
        self.assertEqual(self.notices[-1][0], "移除失败")
        self.assertIn("较新的凭据", self.notices[-1][1])

    def test_key_save_preserves_a_newer_credential_from_another_window(self) -> None:
        original_dialog = app_module.ApiKeyDialog
        credential_store = self.credential_store

        class RacingSaveDialog:
            def __init__(self, *_args, **_kwargs):
                credential_store.value = "sk-other-window"
                self.result = "sk-dialog-value"
                self.deleted = False

        app_module.ApiKeyDialog = RacingSaveDialog
        try:
            self.application.configure_deepseek_key()
        finally:
            app_module.ApiKeyDialog = original_dialog
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, "sk-other-window")
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.notices[-1][0], "保存失败")
        self.assertIn("较新的凭据", self.notices[-1][1])

    def test_saved_key_mutation_uses_the_shared_codex_lock(self) -> None:
        self.application.store.lock_timeout = 0.1
        original_key = self.credential_store.value

        with self.application.store.codex_operation_guard(self.application.target_auth_path):
            self.application._begin_saved_key_mutation(
                "sk-blocked-value",
                original_key,
            )
            self.wait_for_restart()

        self.assertEqual(self.credential_store.value, original_key)
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.credential_store.delete_calls, 0)
        self.assertEqual(self.notices[-1][0], "保存失败")
        self.assertIn("Codex 操作锁", self.notices[-1][1])

    def test_active_deepseek_key_update_preserves_newer_external_credential(self) -> None:
        original_dialog = app_module.ApiKeyDialog
        credential_store = self.credential_store
        self.provider_manager.mode = "deepseek"
        self.provider_manager.managed = True
        self.provider_manager.active_model = app_module.DEEPSEEK_MODEL_PRO
        self.application.refresh_provider_status(show_error=False)

        class RacingUpdateDialog:
            def __init__(self, *_args, **_kwargs):
                credential_store.value = "sk-other-window"
                self.result = "sk-dialog-value"
                self.deleted = False

        app_module.ApiKeyDialog = RacingUpdateDialog
        try:
            self.application.configure_deepseek_key()
        finally:
            app_module.ApiKeyDialog = original_dialog
        self.wait_for_restart()

        self.assertEqual(self.credential_store.value, "sk-other-window")
        self.assertEqual(self.credential_store.writes, [])
        self.assertEqual(self.provider_manager.events, [])
        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertIn("较新的凭据", self.notices[-1][1])

    def test_openai_restore_failure_rolls_back_account_change(self) -> None:
        self.provider_manager.mode = "deepseek"
        self.provider_manager.restore_error = ProviderConfigError("模拟恢复失败")
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("工作账号")
        self.application.add_from_current()
        self.write_current_auth("current")
        before = self.application.target_auth_path.read_bytes()

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), before)
        self.assertEqual(self.provider_manager.mode, "deepseek")
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("模拟恢复失败", self.notices[-1][1])

    def test_cancelled_switch_does_not_change_auth_or_restart(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        self.write_current_auth("current")
        before = self.application.target_auth_path.read_bytes()
        self.application.show_confirm = lambda *_args, **_kwargs: False

        self.application.switch_selected_profile()

        self.assertEqual(self.application.target_auth_path.read_bytes(), before)
        self.assertEqual(self.codex_client.calls, 0)

    def test_switch_lock_timeout_happens_before_codex_is_stopped(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        before = self.application.target_auth_path.read_bytes()
        self.application.store.lock_timeout = 0.1
        status_threads = []
        original_status = self.provider_manager.status

        def tracked_status():
            status_threads.append(threading.current_thread().name)
            return original_status()

        self.provider_manager.status = tracked_status

        with self.application.store.account_switch_guard(self.application.target_auth_path):
            self.application.switch_selected_profile()
            self.wait_for_restart()

        self.assertNotIn("CodexRestartWorker", status_threads)
        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.application.target_auth_path.read_bytes(), before)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("Codex 操作锁", self.notices[-1][1])

    def test_account_failure_does_not_start_a_client_that_was_not_running(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        self.application.codex_client = StubCodexClient(result=RestartResult(False, False, 2400))
        self.application.store.sync_current_profile = lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(app_module.AccountStoreError("模拟同步失败"))

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.codex_client.stop_calls, 1)
        self.assertEqual(self.application.codex_client.start_calls, 0)
        self.assertEqual(self.notices[-1][0], "切换失败")

    def test_account_guard_exit_failure_emits_only_one_terminal_outcome(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()
        self.write_current_auth("target")
        StubRemarkDialog.results.append("账号 B")
        self.application.add_from_current()
        target_profile = next(item for item in self.application.profiles if item.remark == "账号 A")
        self.application.refresh_profiles(target_profile.profile_id)
        expected_auth = self.application.store.profile_path(target_profile).read_bytes()
        original_guard = self.application.store.account_switch_guard
        guard_calls = 0

        class ExitFailureGuard:
            def __enter__(_self):
                _self.inner = original_guard(self.application.target_auth_path)
                return _self.inner.__enter__()

            def __exit__(_self, exc_type, exc, traceback):
                _self.inner.__exit__(exc_type, exc, traceback)
                raise app_module.AccountStoreError("模拟锁释放失败")

        def outer_exit_failure_guard(_path):
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 1:
                return ExitFailureGuard()
            return original_guard(self.application.target_auth_path)

        self.application.store.account_switch_guard = outer_exit_failure_guard
        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), expected_auth)
        self.assertEqual(self.notices[-1][0], "已切换账号")
        self.assertEqual(self.notices[-1][2], "warning")
        self.assertIn("模拟锁释放失败", self.notices[-1][1])
        terminal_items = []
        while not self.application.restart_queue.empty():
            item = self.application.restart_queue.get_nowait()
            if item.get("status") != "progress":
                terminal_items.append(item)
        self.assertEqual(terminal_items, [])
        self.assertEqual(self.application.codex_client.start_calls, 1)

    def test_unexpected_provider_status_failure_finishes_without_stopping_codex(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()

        original_status = self.provider_manager.status
        status_calls = 0

        def fail_worker_status():
            nonlocal status_calls
            status_calls += 1
            if status_calls == 1:
                return original_status()
            raise RuntimeError("simulated unexpected status failure")

        self.provider_manager.status = fail_worker_status
        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertFalse(self.application.busy)
        self.assertEqual(self.codex_client.stop_calls, 0)
        self.assertEqual(self.codex_client.start_calls, 0)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("系统未能检测当前模型服务", self.notices[-1][1])
        self.assertFalse(self.application.busy)

    def test_restart_failure_keeps_switched_auth_and_gives_manual_fallback(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        profile = self.application.profiles[0]
        saved = self.application.store.profile_path(profile).read_bytes()
        self.write_current_auth("current")
        self.application.codex_client = StubCodexClient(
            start_error=CodexClientError("未检测到 Microsoft Store 版 Codex。")
        )

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), saved)
        self.assertEqual(self.notices[-1][0], "账号已切换")
        self.assertEqual(self.notices[-1][2], "warning")
        self.assertIn("请手动启动 Codex", self.notices[-1][1])
        self.assertIn("Microsoft Store", self.notices[-1][1])

    def test_stopped_client_success_uses_started_wording(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        self.application.codex_client = StubCodexClient(result=RestartResult(False, False, 2400))

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.notices[-1][0], "Codex 已启动")
        self.assertIn("已是当前账号", self.notices[-1][1])

    def test_client_stops_before_auth_is_replaced_and_then_starts(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        self.write_current_auth("current")
        events = []

        class OrderedClient:
            def stop(self):
                events.append("stop")
                return StopResult(True, False)

            def start(self):
                events.append("start")
                return 2400

        original_sync = self.application.store.sync_current_profile
        original_switch = self.application.store.switch_profile

        def tracked_sync(*args, **kwargs):
            events.append("sync")
            return original_sync(*args, **kwargs)

        def tracked_switch(*args, **kwargs):
            events.append("switch")
            return original_switch(*args, **kwargs)

        self.application.codex_client = OrderedClient()
        self.application.store.sync_current_profile = tracked_sync
        self.application.store.switch_profile = tracked_switch

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(events, ["stop", "sync", "switch", "start"])

    def test_switch_saves_refreshed_current_profile_before_replacing_auth(self) -> None:
        self.write_current_managed_auth("account-a", "1")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()
        account_a = self.application.profiles[0]
        account_a_original = self.application.store.profile_path(account_a).read_bytes()

        self.write_current_managed_auth("account-b", "1")
        StubRemarkDialog.results.append("账号 B")
        self.application.add_from_current()
        account_b = next(item for item in self.application.profiles if item.remark == "账号 B")
        account_b_bytes = self.application.store.profile_path(account_b).read_bytes()

        self.write_current_managed_auth("account-a", "2")
        refreshed_a_bytes = self.application.target_auth_path.read_bytes()
        self.application.refresh_profiles(account_b.profile_id)

        self.application.switch_selected_profile()
        self.wait_for_restart()

        refreshed_a = next(item for item in self.application.profiles if item.remark == "账号 A")
        self.assertEqual(
            self.application.store.profile_path(refreshed_a).read_bytes(),
            refreshed_a_bytes,
        )
        self.assertEqual(self.application.target_auth_path.read_bytes(), account_b_bytes)
        update_backups = list(self.application.store.update_backup_directory.glob("*.json"))
        self.assertEqual(len(update_backups), 1)
        self.assertEqual(update_backups[0].read_bytes(), account_a_original)
        self.assertIn("已自动保存“账号 A”", self.notices[-1][1])

    def test_failed_target_switch_refreshes_successfully_synced_current_profile(self) -> None:
        self.write_current_managed_auth("account-a", "1")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()

        self.write_current_managed_auth("account-b", "1")
        StubRemarkDialog.results.append("账号 B")
        self.application.add_from_current()
        account_b = next(item for item in self.application.profiles if item.remark == "账号 B")

        self.write_current_managed_auth("account-a", "2")
        refreshed_a_bytes = self.application.target_auth_path.read_bytes()
        self.application.store.profile_path(account_b).write_text(
            json.dumps({"token": "externally-modified"}),
            encoding="utf-8",
        )
        self.application.refresh_profiles(account_b.profile_id)

        self.application.switch_selected_profile()
        self.wait_for_restart()

        refreshed_a = next(item for item in self.application.profiles if item.remark == "账号 A")
        self.assertEqual(
            self.application.store.profile_path(refreshed_a).read_bytes(),
            refreshed_a_bytes,
        )
        self.assertEqual(
            refreshed_a.sha256,
            app_module.validate_auth_file(self.application.store.profile_path(refreshed_a)),
        )
        self.assertEqual(self.application.target_auth_path.read_bytes(), refreshed_a_bytes)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("已自动保存“账号 A”", self.notices[-1][1])
        self.assertIn("目标账号切换未完成", self.notices[-1][1])

    def test_switching_same_profile_keeps_refreshed_current_auth(self) -> None:
        self.write_current_managed_auth("account-a", "1")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()
        account_a = self.application.profiles[0]
        self.write_current_managed_auth("account-a", "2")
        refreshed_bytes = self.application.target_auth_path.read_bytes()
        self.application.refresh_profiles(account_a.profile_id)

        self.application.switch_selected_profile()
        self.wait_for_restart()

        refreshed_a = self.application.profiles[0]
        self.assertEqual(self.application.target_auth_path.read_bytes(), refreshed_bytes)
        self.assertEqual(
            self.application.store.profile_path(refreshed_a).read_bytes(),
            refreshed_bytes,
        )
        self.assertEqual(list(self.application.store.backup_directory.glob("*.json")), [])
        self.assertIn("已自动保存“账号 A”", self.notices[-1][1])

    def test_deepseek_restore_failure_keeps_synced_account_and_rolls_back_target(self) -> None:
        self.write_current_managed_auth("account-a", "1")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()

        self.write_current_managed_auth("account-b", "1")
        StubRemarkDialog.results.append("账号 B")
        self.application.add_from_current()
        account_b = next(item for item in self.application.profiles if item.remark == "账号 B")
        account_b_bytes = self.application.store.profile_path(account_b).read_bytes()

        self.write_current_managed_auth("account-a", "2")
        refreshed_a_bytes = self.application.target_auth_path.read_bytes()
        self.provider_manager.mode = "deepseek"
        self.provider_manager.restore_error = ProviderConfigError("模拟恢复失败")
        self.application.refresh_provider_status(show_error=False)
        self.application.refresh_profiles(account_b.profile_id)

        self.application.switch_selected_profile()
        self.wait_for_restart()

        refreshed_a = next(item for item in self.application.profiles if item.remark == "账号 A")
        self.assertEqual(
            self.application.store.profile_path(refreshed_a).read_bytes(),
            refreshed_a_bytes,
        )
        self.assertEqual(
            self.application.store.profile_path(account_b).read_bytes(),
            account_b_bytes,
        )
        self.assertEqual(self.application.target_auth_path.read_bytes(), refreshed_a_bytes)
        self.assertEqual(self.provider_manager.mode, "deepseek")
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("模拟恢复失败", self.notices[-1][1])
        self.assertEqual(
            refreshed_a.sha256,
            app_module.validate_auth_file(self.application.store.profile_path(refreshed_a)),
        )
        self.assertIn("已自动保存“账号 A”", self.notices[-1][1])

    def test_restart_failure_keeps_target_and_prior_account_sync(self) -> None:
        self.write_current_managed_auth("account-a", "1")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()

        self.write_current_managed_auth("account-b", "1")
        StubRemarkDialog.results.append("账号 B")
        self.application.add_from_current()
        account_b = next(item for item in self.application.profiles if item.remark == "账号 B")
        account_b_bytes = self.application.store.profile_path(account_b).read_bytes()

        self.write_current_managed_auth("account-a", "2")
        refreshed_a_bytes = self.application.target_auth_path.read_bytes()
        self.application.refresh_profiles(account_b.profile_id)
        self.application.codex_client = StubCodexClient(
            start_error=CodexClientError("模拟启动失败")
        )

        self.application.switch_selected_profile()
        self.wait_for_restart()

        refreshed_a = next(item for item in self.application.profiles if item.remark == "账号 A")
        self.assertEqual(
            self.application.store.profile_path(refreshed_a).read_bytes(),
            refreshed_a_bytes,
        )
        self.assertEqual(self.application.target_auth_path.read_bytes(), account_b_bytes)
        self.assertEqual(self.notices[-1][0], "账号已切换")
        self.assertIn("请手动启动 Codex", self.notices[-1][1])

    def test_ambiguous_current_identity_stops_switch_and_restarts_original(self) -> None:
        first_source = self.root_path / "first.json"
        second_source = self.root_path / "second.json"
        first_source.write_text(
            json.dumps(self.managed_auth("account-a", "1")),
            encoding="utf-8",
        )
        second_source.write_text(
            json.dumps(self.managed_auth("account-a", "2")),
            encoding="utf-8",
        )
        first = self.application.store.import_profile(first_source, "账号 A 旧副本").profile
        legacy_source = self.root_path / "legacy.json"
        legacy_source.write_text(json.dumps({"token": "legacy"}), encoding="utf-8")
        second = self.application.store.import_profile(
            legacy_source,
            "账号 A 新副本",
        ).profile
        second_path = self.application.store.profile_path(second)
        second_path.write_bytes(second_source.read_bytes())
        manifest = json.loads(self.application.store.manifest_path.read_text(encoding="utf-8"))
        for item in manifest["profiles"]:
            if item["id"] == second.profile_id:
                item["sha256"] = app_module.validate_auth_file(second_path)
        self.application.store.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        self.write_current_managed_auth("account-a", "3")
        current_bytes = self.application.target_auth_path.read_bytes()
        self.application.refresh_profiles(first.profile_id)

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), current_bytes)
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertIn("多个账号副本", self.notices[-1][1])

    def test_manual_update_rejects_different_managed_account(self) -> None:
        self.write_current_managed_auth("account-a", "1")
        StubRemarkDialog.results.append("账号 A")
        self.application.add_from_current()
        account_a = self.application.profiles[0]
        stored_bytes = self.application.store.profile_path(account_a).read_bytes()
        self.write_current_managed_auth("account-b", "2")

        self.application.update_selected_profile()

        self.assertEqual(self.application.store.profile_path(account_a).read_bytes(), stored_bytes)
        self.assertEqual(self.notices[-1][0], "更新失败")
        self.assertIn("不是同一 ChatGPT 账号", self.notices[-1][1])

    def test_switch_error_restarts_client_with_original_auth(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        profile = self.application.profiles[0]
        self.application.store.profile_path(profile).write_text(
            json.dumps({"token": "tampered"}),
            encoding="utf-8",
        )
        self.write_current_auth("current")
        before = self.application.target_auth_path.read_bytes()

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(self.application.target_auth_path.read_bytes(), before)
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "切换失败")
        self.assertEqual(self.notices[-1][2], "error")

    def test_restart_runs_in_background_and_exposes_progress(self) -> None:
        self.write_current_auth("saved")
        StubRemarkDialog.results.append("测试账号")
        self.application.add_from_current()
        release = threading.Event()

        class SlowCodexClient:
            def stop(self):
                release.wait(1.0)
                return StopResult(True, False)

            def start(self):
                return 2400

        self.application.codex_client = SlowCodexClient()

        self.application.switch_selected_profile()
        self.root.update()

        self.assertTrue(self.application.busy)
        self.assertIn(
            self.application.switch_button.cget("text"),
            ("正在准备…", "正在关闭…"),
        )
        self.assertEqual(self.application.detail_badge.cget("text"), "处理中")
        self.assertEqual(
            self.application.switch_button.cget("background"),
            app_module.COLORS["primary_soft"],
        )
        self.assertEqual(
            self.application.switch_button.cget("disabledforeground"),
            app_module.COLORS["primary"],
        )
        self.assertTrue(
            any(
                value in self.application.update_hint.cget("text")
                for value in ("正在准备", "正在关闭")
            )
        )
        self.application._request_close()
        self.assertEqual(self.root.winfo_exists(), 1)

        release.set()
        self.wait_for_restart()
        self.assertEqual(
            self.application.switch_button.cget("text"),
            app_module.SWITCH_BUTTON_TEXT,
        )

    def test_control_geometry_uses_balanced_dimensions(self) -> None:
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.geometry("1080x720+0+0")
        self.root.update()

        self.assertEqual(
            self.application.add_current_button.winfo_width(),
            self.application.add_file_button.winfo_width(),
        )
        self.assertEqual(
            self.application.add_current_button.winfo_height(),
            self.application.add_file_button.winfo_height(),
        )
        self.assertEqual(
            self.application.rename_button.winfo_width(),
            self.application.remove_button.winfo_width(),
        )
        self.assertEqual(
            self.application.rename_button.winfo_height(),
            self.application.remove_button.winfo_height(),
        )
        self.assertEqual(
            self.application.switch_button.winfo_height(),
            self.application.update_button.winfo_height(),
        )
        self.assertLessEqual(self.application.switch_button.winfo_height(), 42)
        self.assertLessEqual(self.application.rename_button.winfo_height(), 35)
        self.assertLess(
            self.application.switch_button.winfo_width()
            / self.application.switch_button.winfo_height(),
            6.8,
        )

        table_left_inset = (
            self.application.list_shell.winfo_rootx() - self.application.list_card.winfo_rootx()
        )
        self.assertGreaterEqual(table_left_inset, 20)
        self.assertLessEqual(table_left_inset, 22)
        self.assertEqual(
            self.application.list_card.winfo_rooty(),
            self.application.detail_card.winfo_rooty(),
        )
        self.assertEqual(
            self.application.list_card.winfo_height(),
            self.application.detail_card.winfo_height(),
        )

    def test_minimum_window_keeps_actions_inside_detail_card(self) -> None:
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.geometry("900x600+0+0")
        self.root.update()

        self.assertTrue(self.application.switch_button.winfo_ismapped())
        self.assertTrue(self.application.update_button.winfo_ismapped())
        self.assertTrue(self.application.maintenance_frame.winfo_ismapped())
        self.assertTrue(self.application.remove_button.winfo_ismapped())
        visible_column_width = sum(
            self.application.tree.column(column, "width")
            for column in ("remark", "state", "updated")
        )
        self.assertLessEqual(visible_column_width, self.application.tree.winfo_width())
        action_bottom = (
            self.application.remove_button.winfo_rooty()
            + self.application.remove_button.winfo_height()
        )
        detail_bottom = (
            self.application.detail_card.winfo_rooty() + self.application.detail_card.winfo_height()
        )
        self.assertLessEqual(action_bottom + 12, detail_bottom)

    def test_deepseek_page_fits_minimum_window(self) -> None:
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.geometry("900x600+0+0")
        self.application._show_page("deepseek")
        self.root.update()

        self.assertTrue(self.application.provider_action_button.winfo_ismapped())
        self.assertTrue(self.application.provider_openai_button.winfo_ismapped())
        self.assertTrue(self.application.provider_key_button.winfo_ismapped())
        self.assertTrue(self.application.provider_refresh_button.winfo_ismapped())
        for button in self.application.provider_model_buttons.values():
            self.assertTrue(button.winfo_ismapped())
        action_bottom = (
            self.application.provider_key_button.winfo_rooty()
            + self.application.provider_key_button.winfo_height()
        )
        page_bottom = (
            self.application.deepseek_page.winfo_rooty()
            + self.application.deepseek_page.winfo_height()
        )
        self.assertLessEqual(action_bottom + 16, page_bottom)
        self.assertLessEqual(
            self.application.deepseek_tab_button.winfo_rootx()
            + self.application.deepseek_tab_button.winfo_width(),
            self.root.winfo_rootx() + self.root.winfo_width(),
        )


if __name__ == "__main__":
    unittest.main()
