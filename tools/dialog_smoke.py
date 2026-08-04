"""Capture deterministic custom-dialog screenshots for visual review."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import ImageGrab

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app as app_module


def capture_dialog(root, output_path: Path) -> None:
    root.update()
    dialogs = [
        child
        for child in root.winfo_children()
        if isinstance(child, app_module.tk.Toplevel) and child.winfo_ismapped()
    ]
    if not dialogs:
        raise RuntimeError("Dialog window was not created.")
    dialog = dialogs[-1]
    left = dialog.winfo_rootx()
    top = dialog.winfo_rooty()
    right = left + dialog.winfo_width()
    bottom = top + dialog.winfo_height()
    ImageGrab.grab((left, top, right, bottom), all_screens=True).save(output_path)
    dialog.destroy()


def schedule_capture(root, output_path: Path) -> None:
    root.after(450, lambda: capture_dialog(root, output_path))


def main() -> None:
    output_directory = PROJECT_ROOT / "artifacts" / "dialogs"
    output_directory.mkdir(parents=True, exist_ok=True)
    root = app_module.tk.Tk()
    root.title("Dialog smoke host")
    root.geometry("900x600")
    root.configure(bg=app_module.COLORS["window"])
    root.update_idletasks()
    font_family, _mono_font = app_module.choose_ui_font(root)

    notice_path = output_directory / "notice.png"
    schedule_capture(root, notice_path)
    app_module.NoticeDialog(
        root,
        "已切换到 DeepSeek",
        "Codex 已重新启动。",
        font_family,
        "success",
    )

    confirm_path = output_directory / "confirm.png"
    schedule_capture(root, confirm_path)
    app_module.ConfirmDialog(
        root,
        "切换到 DeepSeek",
        "将切换到 DeepSeek V4 Flash，并重启 Codex。\n正在运行的任务可能会中断。",
        font_family,
        "切换并重启",
    )

    warning_path = output_directory / "warning.png"
    schedule_capture(root, warning_path)
    app_module.NoticeDialog(
        root,
        "配置已更新",
        "已切换到 DeepSeek，但 Codex 未能自动启动。\n"
        "请手动启动 Codex。\n\n"
        "原因：未检测到 Microsoft Store 版 Codex。",
        font_family,
        "warning",
    )

    remark_path = output_directory / "remark.png"
    schedule_capture(root, remark_path)
    app_module.RemarkDialog(
        root,
        font_family,
        "修改备注",
        "备注仅用于本机显示，不会修改认证内容。",
        "保存",
        "工作账号",
    )

    api_key_path = output_directory / "api-key.png"
    schedule_capture(root, api_key_path)
    app_module.ApiKeyDialog(
        root,
        font_family,
        "sk-demo-1234",
        allow_delete=True,
    )

    root.destroy()
    print(notice_path)
    print(confirm_path)
    print(warning_path)
    print(remark_path)
    print(api_key_path)


if __name__ == "__main__":
    main()
