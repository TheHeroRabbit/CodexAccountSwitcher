import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

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

    def read(self):
        return self.value

    def write(self, value):
        self.value = value
        self.writes.append(value)

    def delete(self):
        self.value = None
        self.delete_calls += 1


class StubProviderManager:
    def __init__(self, mode="openai", events=None) -> None:
        self.mode = mode
        self.events = events if events is not None else []
        self.conflict = False
        self.activate_error = None
        self.restore_error = None
        self.last_key = None

    def status(self):
        return ProviderStatus(self.mode, self.mode == "deepseek", self.conflict, False)

    def activate_deepseek(self, api_key):
        self.events.append("activate")
        self.last_key = api_key
        if self.activate_error is not None:
            raise self.activate_error
        changed = self.mode != "deepseek"
        self.mode = "deepseek"
        return ProviderSwitchResult(changed, "deepseek")

    def restore_openai(self):
        self.events.append("restore")
        if self.restore_error is not None:
            raise self.restore_error
        changed = self.mode == "deepseek"
        self.mode = "openai"
        return ProviderSwitchResult(changed, "openai")


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

    def tearDown(self) -> None:
        app_module.app_directory = self.original_app_directory
        app_module.RemarkDialog = self.original_remark_dialog
        if hasattr(self, "root"):
            self.root.destroy()
        self.temporary_directory.cleanup()

    def write_current_auth(self, token: str) -> None:
        self.application.target_auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.application.target_auth_path.write_text(
            json.dumps({"token": token}),
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
        self.assertEqual(self.provider_manager.last_key, self.credential_store.value)
        self.assertEqual(self.codex_client.stop_calls, 1)
        self.assertEqual(self.codex_client.start_calls, 1)
        self.assertEqual(self.notices[-1][0], "已切换到 DeepSeek")

        self.application.toggle_provider()
        self.wait_for_restart()

        self.assertEqual(self.provider_manager.mode, "openai")
        self.assertEqual(self.codex_client.stop_calls, 2)
        self.assertEqual(self.codex_client.start_calls, 2)
        self.assertEqual(self.notices[-1][0], "已切回 OpenAI")

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

        self.assertIsNone(self.credential_store.value)
        self.assertEqual(self.credential_store.delete_calls, 1)
        self.assertEqual(self.notices[-1][0], "API Key 已移除")

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

        original_switch = self.application.store.switch_profile

        def tracked_switch(*args, **kwargs):
            events.append("switch")
            return original_switch(*args, **kwargs)

        self.application.codex_client = OrderedClient()
        self.application.store.switch_profile = tracked_switch

        self.application.switch_selected_profile()
        self.wait_for_restart()

        self.assertEqual(events, ["stop", "switch", "start"])

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
        self.assertTrue(self.application.provider_key_button.winfo_ismapped())
        self.assertTrue(self.application.provider_refresh_button.winfo_ismapped())
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
