"""Capture deterministic application screenshots for visual review."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from PIL import ImageGrab

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app as app_module


class SmokeProviderManager:
    def __init__(self, mode="openai", active_model=None) -> None:
        self.mode = mode
        self.active_model = active_model

    def status(self):
        return app_module.ProviderStatus(
            self.mode,
            self.mode == "deepseek",
            False,
            False,
            self.active_model if self.mode == "deepseek" else None,
        )


class SmokeCredentialStore:
    def read(self):
        return "sk-demo-1234"


def write_auth(path: Path, token: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": token}), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--page", choices=("accounts", "deepseek"), default="accounts")
    parser.add_argument(
        "--model",
        choices=(app_module.DEEPSEEK_MODEL_FLASH, app_module.DEEPSEEK_MODEL_PRO),
        default=app_module.DEEPSEEK_MODEL_FLASH,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "ui-smoke.png",
    )
    arguments = parser.parse_args()
    if (arguments.width is None) != (arguments.height is None):
        raise SystemExit("--width and --height must be used together.")
    output_path = arguments.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        app_root = temporary_root / "app"
        app_module.app_directory = lambda: app_root

        root = app_module.tk.Tk()
        provider_manager = SmokeProviderManager(
            "deepseek" if arguments.page == "deepseek" else "openai",
            arguments.model if arguments.page == "deepseek" else None,
        )
        application = app_module.AccountSwitcherApp(
            root,
            provider_manager=provider_manager,
            credential_store=SmokeCredentialStore(),
            codex_home=Path(r"C:\Users\User\.codex"),
        )
        if arguments.width is not None and arguments.height is not None:
            root.geometry("{}x{}".format(arguments.width, arguments.height))
        application.target_auth_path = temporary_root / ".codex" / "auth.json"

        work = application.store.import_profile(
            write_auth(temporary_root / "work.json", "work-token"),
            "工作账号",
        ).profile
        personal = application.store.import_profile(
            write_auth(temporary_root / "personal.json", "personal-token"),
            "个人账号",
        ).profile
        missing = application.store.import_profile(
            write_auth(temporary_root / "expired.json", "expired-token"),
            "旧测试账号",
        ).profile
        application.store.switch_profile(work.profile_id, application.target_auth_path)
        application.store.profile_path(missing).unlink()
        application.refresh_profiles(personal.profile_id)
        application._show_page(arguments.page)
        if arguments.progress:
            if arguments.page == "deepseek":
                application._set_provider_progress("正在启动…", "正在启动 Codex…")
            else:
                application._set_restart_progress("正在启动…", "正在启动 Codex…")
        root.update_idletasks()

        def capture() -> None:
            root.update()
            left = root.winfo_rootx()
            top = root.winfo_rooty()
            right = left + root.winfo_width()
            bottom = top + root.winfo_height()
            ImageGrab.grab((left, top, right, bottom), all_screens=True).save(output_path)
            root.destroy()

        root.after(700, capture)
        root.mainloop()
    print(output_path)


if __name__ == "__main__":
    main()
