"""Tkinter desktop application for switching Codex accounts and providers."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont
from typing import Dict, List, Optional, Tuple

from account_store import (
    MAX_REMARK_LENGTH,
    AccountProfile,
    AccountStoreError,
    InvalidAuthFileError,
    ProfileStore,
    validate_auth_file,
)
from codex_client import CodexClientError, RestartResult, StoreCodexClient
from credential_store import (
    CredentialStoreError,
    InvalidApiKeyError,
    WindowsCredentialStore,
    masked_api_key,
    normalize_api_key,
)
from provider_config import (
    ProviderConfigError,
    ProviderConfigManager,
    ProviderStatus,
    resolve_codex_home,
)

APP_TITLE = "Codex 切换器"
APP_VERSION = "1.3.0"
SWITCH_BUTTON_TEXT = "切换账号并重启"
PROVIDER_BUTTON_OPENAI = "切回 OpenAI 并重启"
PROVIDER_BUTTON_DEEPSEEK = "切换到 DeepSeek 并重启"

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 20
SPACE_2XL = 24
TABLE_CELL_INSET = "\u2002"
TABLE_REMARK_MIN_WIDTH = 200
TABLE_STATE_WIDTH = 84
TABLE_UPDATED_WIDTH = 160

COLORS: Dict[str, str] = {
    "window": "#F4F7FB",
    "surface": "#FFFFFF",
    "surface_alt": "#F8FAFD",
    "border": "#DDE5EF",
    "border_strong": "#C9D5E4",
    "text": "#172033",
    "muted": "#64748B",
    "muted_light": "#8A98AA",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "primary_pressed": "#1E40AF",
    "primary_soft": "#EAF1FF",
    "primary_soft_hover": "#DCE8FF",
    "teal": "#0F9F9A",
    "teal_soft": "#E7F8F6",
    "success": "#11845B",
    "success_soft": "#EAF8F2",
    "warning": "#B54708",
    "warning_soft": "#FFF4E5",
    "danger": "#C4322B",
    "danger_hover": "#A92720",
    "danger_soft": "#FDEDEC",
    "danger_soft_hover": "#FADBD8",
    "disabled": "#EEF2F7",
    "disabled_text": "#A7B2C1",
    "selection": "#E9F1FF",
    "row_selected": "#F0F5FF",
    "row_success": "#F1FAF7",
    "row_danger": "#FFF3F2",
}


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def enable_light_title_bar(window: tk.Misc) -> None:
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        window_handle = ctypes.windll.user32.GetParent(window.winfo_id())
        use_dark = ctypes.c_int(0)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                window_handle,
                attribute,
                ctypes.byref(use_dark),
                ctypes.sizeof(use_dark),
            )
            if result == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative_path: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


def choose_ui_font(root: tk.Misc) -> Tuple[str, str]:
    available = set(tkfont.families(root))
    body_candidates = (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Segoe UI Variable Text",
        "Segoe UI",
        "Noto Sans CJK SC",
        "Arial",
    )
    mono_candidates = ("Cascadia Mono", "Consolas", "Courier New")
    body = next((name for name in body_candidates if name in available), "Arial")
    mono = next((name for name in mono_candidates if name in available), "Courier New")
    return body, mono


def center_window(window: tk.Toplevel, parent: tk.Misc, width: int, height: int) -> None:
    parent.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
    y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
    x = max(8, min(x, max(8, screen_width - width - 8)))
    y = max(8, min(y, max(8, screen_height - height - 48)))
    window.geometry("{}x{}+{}+{}".format(width, height, x, y))


def bounded_notice_height(requested_height: int, screen_height: int) -> int:
    minimum_height = 265
    available_height = max(minimum_height, screen_height - 96)
    return max(minimum_height, min(requested_height, available_height))


def bounded_confirm_height(requested_height: int, screen_height: int) -> int:
    minimum_height = 225
    available_height = max(minimum_height, screen_height - 96)
    return max(minimum_height, min(requested_height, available_height))


def preferred_window_size(screen_width: int, screen_height: int) -> Tuple[int, int]:
    available_width = max(760, screen_width - 64)
    available_height = max(560, screen_height - 96)
    return min(1080, available_width), min(720, available_height)


def format_table_cell(value: str) -> str:
    return TABLE_CELL_INSET + value


def table_column_widths(available_width: int) -> Tuple[int, int, int]:
    usable_width = max(0, available_width - 2)
    remark_width = max(
        TABLE_REMARK_MIN_WIDTH,
        usable_width - TABLE_STATE_WIDTH - TABLE_UPDATED_WIDTH,
    )
    return remark_width, TABLE_STATE_WIDTH, TABLE_UPDATED_WIDTH


def card_frame(master: tk.Misc) -> tk.Frame:
    return tk.Frame(
        master,
        bg=COLORS["surface"],
        highlightbackground=COLORS["border"],
        highlightthickness=1,
    )


class FlatButton(tk.Button):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command,
        font_family: str,
        variant: str = "primary",
        compact: bool = False,
        width: Optional[int] = None,
    ) -> None:
        palettes = {
            "primary": (
                COLORS["primary"],
                "#FFFFFF",
                COLORS["primary_hover"],
                COLORS["primary_pressed"],
                COLORS["primary"],
            ),
            "secondary": (
                COLORS["primary_soft"],
                COLORS["primary"],
                COLORS["primary_soft_hover"],
                "#CADBFF",
                COLORS["primary_soft"],
            ),
            "ghost": (
                COLORS["surface_alt"],
                COLORS["text"],
                COLORS["disabled"],
                COLORS["selection"],
                COLORS["border_strong"],
            ),
            "danger": (
                COLORS["danger_soft"],
                COLORS["danger"],
                COLORS["danger_soft_hover"],
                "#F6C9C5",
                COLORS["danger_soft"],
            ),
        }
        background, foreground, hover, pressed, border = palettes.get(
            variant,
            palettes["ghost"],
        )
        self._normal_background = background
        self._normal_foreground = foreground
        self._hover_background = hover
        self._pressed_background = pressed
        self._border_color = border

        super().__init__(
            master,
            text=text,
            command=command,
            font=(font_family, 9 if compact else 10, "bold"),
            bg=background,
            fg=foreground,
            activebackground=hover,
            activeforeground=foreground,
            disabledforeground=COLORS["disabled_text"],
            relief="flat",
            overrelief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=COLORS["primary"],
            cursor="hand2",
            padx=SPACE_MD if compact else SPACE_LG,
            pady=5 if compact else 6,
            takefocus=True,
            width=width,
        )
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")

    def _is_enabled(self) -> bool:
        return str(self.cget("state")) != "disabled"

    def _on_enter(self, _event) -> None:
        if self._is_enabled():
            self.configure(bg=self._hover_background)

    def _on_leave(self, _event) -> None:
        if self._is_enabled():
            self.configure(bg=self._normal_background)

    def _on_press(self, _event) -> None:
        if self._is_enabled():
            self.configure(bg=self._pressed_background)

    def _on_release(self, event) -> None:
        if self._is_enabled():
            inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
            self.configure(bg=self._hover_background if inside else self._normal_background)

    def _on_focus_in(self, _event) -> None:
        if self._is_enabled():
            self.configure(highlightbackground=COLORS["primary"])

    def _on_focus_out(self, _event) -> None:
        self.configure(highlightbackground=self._border_color)

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.configure(
                state="normal",
                bg=self._normal_background,
                fg=self._normal_foreground,
                cursor="hand2",
                highlightbackground=self._border_color,
            )
        else:
            self.configure(
                state="disabled",
                bg=COLORS["disabled"],
                disabledforeground=COLORS["disabled_text"],
                cursor="arrow",
                highlightbackground=COLORS["disabled"],
            )

    def set_progress(self, text: str) -> None:
        self.configure(
            text=text,
            state="disabled",
            bg=COLORS["primary_soft"],
            disabledforeground=COLORS["primary"],
            cursor="arrow",
            highlightbackground=COLORS["primary_soft"],
        )

    def set_selected(self, selected: bool) -> None:
        if selected:
            self._normal_background = COLORS["primary_soft"]
            self._normal_foreground = COLORS["primary"]
            self._hover_background = COLORS["primary_soft_hover"]
            self._pressed_background = "#CADBFF"
            self._border_color = COLORS["primary_soft"]
        else:
            self._normal_background = COLORS["surface_alt"]
            self._normal_foreground = COLORS["text"]
            self._hover_background = COLORS["disabled"]
            self._pressed_background = COLORS["selection"]
            self._border_color = COLORS["border_strong"]
        if self._is_enabled():
            self.configure(
                bg=self._normal_background,
                fg=self._normal_foreground,
                activebackground=self._hover_background,
                activeforeground=self._normal_foreground,
                highlightbackground=self._border_color,
            )


class NoticeDialog:
    ICONS = {
        "success": ("✓", COLORS["success"], COLORS["success_soft"]),
        "warning": ("!", COLORS["warning"], COLORS["warning_soft"]),
        "error": ("×", COLORS["danger"], COLORS["danger_soft"]),
        "info": ("i", COLORS["primary"], COLORS["primary_soft"]),
    }

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        message: str,
        font_family: str,
        kind: str = "info",
        button_text: str = "确定",
    ) -> None:
        icon_text, accent, icon_background = self.ICONS.get(kind, self.ICONS["info"])
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.configure(bg=COLORS["window"])
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        card = card_frame(self.window)
        card.pack(fill="both", expand=True, padx=12, pady=12)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(1, weight=1)

        tk.Label(
            card,
            text=icon_text,
            font=(font_family, 18, "bold"),
            fg=accent,
            bg=icon_background,
            width=2,
            height=1,
        ).grid(row=0, column=0, rowspan=2, sticky="n", padx=(22, 14), pady=(24, 0))

        tk.Label(
            card,
            text=title,
            font=(font_family, 14, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", padx=(0, 24), pady=(23, 6))

        tk.Label(
            card,
            text=message,
            font=(font_family, 10),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            justify="left",
            anchor="nw",
            wraplength=390,
        ).grid(row=1, column=1, sticky="nsew", padx=(0, 24))

        button = FlatButton(
            card,
            text=button_text,
            command=self.close,
            font_family=font_family,
            variant="primary",
            compact=True,
        )
        button.grid(row=2, column=1, sticky="e", padx=(0, 24), pady=(18, 22))

        self.window.update_idletasks()
        dialog_height = bounded_notice_height(
            self.window.winfo_reqheight(),
            self.window.winfo_screenheight(),
        )
        center_window(self.window, parent, 510, dialog_height)
        enable_light_title_bar(self.window)
        self.window.bind("<Escape>", lambda _event: self.close())
        self.window.bind("<Return>", lambda _event: self.close())
        self.window.grab_set()
        button.focus_set()
        self.window.wait_window()

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class ConfirmDialog:
    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        message: str,
        font_family: str,
        confirm_text: str,
        destructive: bool = False,
    ) -> None:
        self.result = False
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.configure(bg=COLORS["window"])
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        card = card_frame(self.window)
        card.pack(fill="both", expand=True, padx=12, pady=12)

        tk.Label(
            card,
            text=title,
            font=(font_family, 14, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x", padx=24, pady=(23, 7))

        tk.Label(
            card,
            text=message,
            font=(font_family, 10),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=430,
        ).pack(fill="x", padx=24)

        actions = tk.Frame(card, bg=COLORS["surface"])
        actions.pack(side="bottom", fill="x", padx=24, pady=(20, 22))
        confirm_button = FlatButton(
            actions,
            text=confirm_text,
            command=self.confirm,
            font_family=font_family,
            variant="danger" if destructive else "primary",
            compact=True,
            width=7,
        )
        confirm_button.pack(side="right")
        FlatButton(
            actions,
            text="取消",
            command=self.cancel,
            font_family=font_family,
            variant="ghost",
            compact=True,
            width=7,
        ).pack(side="right", padx=(0, SPACE_SM))

        self.window.update_idletasks()
        dialog_height = bounded_confirm_height(
            self.window.winfo_reqheight(),
            self.window.winfo_screenheight(),
        )
        center_window(self.window, parent, 510, dialog_height)
        enable_light_title_bar(self.window)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.grab_set()
        confirm_button.focus_set()
        self.window.wait_window()

    def confirm(self) -> None:
        self.result = True
        self.close()

    def cancel(self) -> None:
        self.result = False
        self.close()

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class RemarkDialog:
    def __init__(
        self,
        parent: tk.Misc,
        font_family: str,
        title: str,
        description: str,
        confirm_text: str,
        initial_value: str = "",
    ) -> None:
        self.result: Optional[str] = None
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.configure(bg=COLORS["window"])
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        card = card_frame(self.window)
        card.pack(fill="both", expand=True, padx=12, pady=12)

        tk.Label(
            card,
            text=title,
            font=(font_family, 15, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x", padx=24, pady=(23, 5))

        tk.Label(
            card,
            text=description,
            font=(font_family, 9),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=430,
        ).pack(fill="x", padx=24, pady=(0, 14))

        self.entry_shell = tk.Frame(
            card,
            bg=COLORS["surface_alt"],
            highlightbackground=COLORS["border_strong"],
            highlightthickness=1,
        )
        self.entry_shell.pack(fill="x", padx=24)

        self.value = tk.StringVar(value=initial_value[:MAX_REMARK_LENGTH])
        self.entry = tk.Entry(
            self.entry_shell,
            textvariable=self.value,
            font=(font_family, 11),
            bg=COLORS["surface_alt"],
            fg=COLORS["text"],
            insertbackground=COLORS["primary"],
            selectbackground=COLORS["selection"],
            selectforeground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.entry.pack(fill="x", padx=12, pady=10)

        self.counter = tk.Label(
            card,
            text="{} / {}".format(len(self.value.get()), MAX_REMARK_LENGTH),
            font=(font_family, 8),
            fg=COLORS["muted_light"],
            bg=COLORS["surface"],
            anchor="e",
        )
        self.counter.pack(fill="x", padx=25, pady=(5, 0))

        actions = tk.Frame(card, bg=COLORS["surface"])
        actions.pack(fill="x", padx=24, pady=(17, 22))
        self.save_button = FlatButton(
            actions,
            text=confirm_text,
            command=self.save,
            font_family=font_family,
            variant="primary",
            compact=True,
            width=7,
        )
        self.save_button.pack(side="right")
        FlatButton(
            actions,
            text="取消",
            command=self.cancel,
            font_family=font_family,
            variant="ghost",
            compact=True,
            width=7,
        ).pack(side="right", padx=(0, SPACE_SM))

        self.value.trace_add("write", self._on_change)
        self._on_change()
        self.entry.bind("<Return>", lambda _event: self.save())
        self.entry.bind(
            "<FocusIn>",
            lambda _event: self.entry_shell.configure(highlightbackground=COLORS["primary"]),
        )
        self.entry.bind(
            "<FocusOut>",
            lambda _event: self.entry_shell.configure(highlightbackground=COLORS["border_strong"]),
        )
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.update_idletasks()
        dialog_height = bounded_notice_height(
            self.window.winfo_reqheight(),
            self.window.winfo_screenheight(),
        )
        center_window(self.window, parent, 510, dialog_height)
        enable_light_title_bar(self.window)
        self.window.grab_set()
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        self.window.wait_window()

    def _on_change(self, *_args) -> None:
        raw = self.value.get()
        if len(raw) > MAX_REMARK_LENGTH:
            self.value.set(raw[:MAX_REMARK_LENGTH])
            return
        self.counter.configure(text="{} / {}".format(len(raw), MAX_REMARK_LENGTH))
        self.save_button.set_enabled(bool(raw.strip()))

    def save(self) -> None:
        value = " ".join(self.value.get().split())
        if not value:
            return
        self.result = value
        self.close()

    def cancel(self) -> None:
        self.result = None
        self.close()

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class ApiKeyDialog:
    def __init__(
        self,
        parent: tk.Misc,
        font_family: str,
        initial_value: str = "",
        allow_delete: bool = False,
    ) -> None:
        self.result: Optional[str] = None
        self.deleted = False
        self.visible = False
        self.window = tk.Toplevel(parent)
        self.window.title("设置 DeepSeek API Key")
        self.window.configure(bg=COLORS["window"])
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        card = card_frame(self.window)
        card.pack(fill="both", expand=True, padx=12, pady=12)

        tk.Label(
            card,
            text="设置 DeepSeek API Key",
            font=(font_family, 15, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x", padx=24, pady=(23, 5))
        tk.Label(
            card,
            text="API Key 将保存在 Windows 凭据管理器。",
            font=(font_family, 9),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x", padx=24, pady=(0, 14))

        field_row = tk.Frame(card, bg=COLORS["surface"])
        field_row.pack(fill="x", padx=24)
        field_row.grid_columnconfigure(0, weight=1)
        self.entry_shell = tk.Frame(
            field_row,
            bg=COLORS["surface_alt"],
            highlightbackground=COLORS["border_strong"],
            highlightthickness=1,
        )
        self.entry_shell.grid(row=0, column=0, sticky="ew", padx=(0, SPACE_SM))
        self.value = tk.StringVar(value=initial_value)
        self.entry = tk.Entry(
            self.entry_shell,
            textvariable=self.value,
            show="•",
            font=(font_family, 11),
            bg=COLORS["surface_alt"],
            fg=COLORS["text"],
            insertbackground=COLORS["primary"],
            selectbackground=COLORS["selection"],
            selectforeground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.entry.pack(fill="x", padx=12, pady=10)
        self.toggle_button = FlatButton(
            field_row,
            text="显示",
            command=self.toggle_visibility,
            font_family=font_family,
            variant="ghost",
            compact=True,
            width=5,
        )
        self.toggle_button.grid(row=0, column=1, sticky="e")

        self.error_label = tk.Label(
            card,
            text="",
            font=(font_family, 8),
            fg=COLORS["danger"],
            bg=COLORS["surface"],
            anchor="w",
        )
        self.error_label.pack(fill="x", padx=25, pady=(6, 0))

        tk.Label(
            card,
            text="使用 DeepSeek 时，Codex 会按官方要求在本机配置中读取此密钥。",
            font=(font_family, 8),
            fg=COLORS["muted_light"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=450,
        ).pack(fill="x", padx=24, pady=(8, 0))

        actions = tk.Frame(card, bg=COLORS["surface"])
        actions.pack(fill="x", padx=24, pady=(17, 22))
        if allow_delete:
            FlatButton(
                actions,
                text="移除",
                command=self.remove,
                font_family=font_family,
                variant="danger",
                compact=True,
                width=7,
            ).pack(side="left")
        save_button = FlatButton(
            actions,
            text="保存",
            command=self.save,
            font_family=font_family,
            variant="primary",
            compact=True,
            width=7,
        )
        save_button.pack(side="right")
        FlatButton(
            actions,
            text="取消",
            command=self.cancel,
            font_family=font_family,
            variant="ghost",
            compact=True,
            width=7,
        ).pack(side="right", padx=(0, SPACE_SM))

        self.value.trace_add("write", lambda *_args: self.error_label.configure(text=""))
        self.entry.bind(
            "<FocusIn>",
            lambda _event: self.entry_shell.configure(highlightbackground=COLORS["primary"]),
        )
        self.entry.bind(
            "<FocusOut>",
            lambda _event: self.entry_shell.configure(highlightbackground=COLORS["border_strong"]),
        )
        self.entry.bind("<Return>", lambda _event: self.save())
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.update_idletasks()
        center_window(
            self.window,
            parent,
            540,
            bounded_notice_height(self.window.winfo_reqheight(), self.window.winfo_screenheight()),
        )
        enable_light_title_bar(self.window)
        self.window.grab_set()
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        self.window.wait_window()

    def toggle_visibility(self) -> None:
        self.visible = not self.visible
        self.entry.configure(show="" if self.visible else "•")
        self.toggle_button.configure(text="隐藏" if self.visible else "显示")
        self.entry.focus_set()

    def save(self) -> None:
        try:
            self.result = normalize_api_key(self.value.get())
        except InvalidApiKeyError as exc:
            self.result = None
            self.error_label.configure(text=str(exc))
            self.entry.focus_set()
            return
        self.close()

    def remove(self) -> None:
        self.deleted = True
        self.result = None
        self.close()

    def cancel(self) -> None:
        self.result = None
        self.close()

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class AccountSwitcherApp:
    def __init__(
        self,
        root: tk.Tk,
        codex_client=None,
        provider_manager=None,
        credential_store=None,
        codex_home: Optional[Path] = None,
    ) -> None:
        self.root = root
        self.font_family, self.mono_font = choose_ui_font(root)
        self.base_directory = app_directory()
        self.store = ProfileStore(self.base_directory)
        self.codex_home = Path(codex_home or resolve_codex_home()).resolve()
        self.target_auth_path = self.codex_home / "auth.json"
        self.profiles: List[AccountProfile] = []
        self.active_profile_id: Optional[str] = None
        self.provider_status = ProviderStatus("openai", False, False, False)
        self.provider_status_error = ""
        self.credential_status_error = ""
        self.busy = False
        self.busy_message = ""
        self.restart_in_progress = False
        self.restart_button_text = ""
        self.provider_in_progress = False
        self.provider_button_text = ""
        self.codex_client = codex_client
        self.restart_queue = queue.Queue()
        self.restart_poll_id = None
        self.provider_queue = queue.Queue()
        self.provider_poll_id = None
        self.header_icon: Optional[tk.PhotoImage] = None
        self.window_icon: Optional[tk.PhotoImage] = None
        self.current_page = "accounts"

        self.provider_manager = provider_manager
        self.credential_store = credential_store
        self.provider_initialization_error = ""
        self.credential_initialization_error = ""
        if self.provider_manager is None:
            try:
                self.provider_manager = ProviderConfigManager(
                    self.codex_home,
                    self.store.storage_directory,
                    resource_path("assets/deepseek_models.json"),
                )
            except (ProviderConfigError, CredentialStoreError, OSError) as exc:
                self.provider_initialization_error = str(exc)
        if self.credential_store is None:
            try:
                self.credential_store = WindowsCredentialStore()
            except (CredentialStoreError, OSError) as exc:
                self.credential_initialization_error = str(exc)

        self._configure_window()
        self._configure_styles()
        self._build_interface()
        self.root.protocol("WM_DELETE_WINDOW", self._request_close)
        self.refresh_profiles()
        self.refresh_provider_status(show_error=False)
        self._show_page("deepseek" if self.provider_status.mode == "deepseek" else "accounts")
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.configure(bg=COLORS["window"])
        self.root.option_add("*tearOff", False)

        icon_path = resource_path("assets/app_icon.png")
        header_path = resource_path("assets/app_icon_64.png")
        try:
            self.window_icon = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self.window_icon)
        except tk.TclError:
            self.window_icon = None
        try:
            self.header_icon = tk.PhotoImage(file=str(header_path))
        except tk.TclError:
            self.header_icon = None

        self.root.update_idletasks()
        width, height = preferred_window_size(
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )
        self.root.minsize(min(900, width), min(600, height))
        x = max(8, (self.root.winfo_screenwidth() - width) // 2)
        y = max(8, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry("{}x{}+{}+{}".format(width, height, x, y))
        enable_light_title_bar(self.root)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Profiles.Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            rowheight=44,
            borderwidth=0,
            relief="flat",
            font=(self.font_family, 10),
        )
        style.layout("Profiles.Treeview", [("Profiles.Treeview.treearea", {"sticky": "nswe"})])
        style.map(
            "Profiles.Treeview",
            background=[("selected", COLORS["row_selected"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "Profiles.Treeview.Heading",
            background=COLORS["surface_alt"],
            foreground=COLORS["muted"],
            borderwidth=0,
            relief="flat",
            padding=(10, 9),
            font=(self.font_family, 9, "bold"),
        )
        style.map(
            "Profiles.Treeview.Heading",
            background=[("active", COLORS["surface_alt"])],
            foreground=[("active", COLORS["text"])],
        )
        style.configure(
            "App.Vertical.TScrollbar",
            background=COLORS["border_strong"],
            troughcolor=COLORS["surface_alt"],
            bordercolor=COLORS["surface_alt"],
            arrowcolor=COLORS["muted"],
            relief="flat",
            width=12,
        )

    def _build_interface(self) -> None:
        outer = tk.Frame(self.root, bg=COLORS["window"])
        outer.pack(
            fill="both",
            expand=True,
            padx=SPACE_2XL,
            pady=(SPACE_XL, SPACE_LG),
        )
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=COLORS["window"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_LG))
        header.grid_columnconfigure(1, weight=1)

        if self.header_icon is not None:
            tk.Label(
                header,
                image=self.header_icon,
                bg=COLORS["window"],
                borderwidth=0,
            ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, SPACE_LG))
        else:
            tk.Label(
                header,
                text="↔",
                font=(self.font_family, 22, "bold"),
                fg=COLORS["primary"],
                bg=COLORS["primary_soft"],
                width=2,
            ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, SPACE_LG))

        tk.Label(
            header,
            text=APP_TITLE,
            font=(self.font_family, 20, "bold"),
            fg=COLORS["text"],
            bg=COLORS["window"],
            anchor="w",
        ).grid(row=0, column=1, sticky="sw")
        tk.Label(
            header,
            text="账号与模型服务",
            font=(self.font_family, 9),
            fg=COLORS["muted"],
            bg=COLORS["window"],
            anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(2, 0))

        page_switcher = tk.Frame(header, bg=COLORS["surface_alt"])
        page_switcher.grid(row=0, column=2, rowspan=2, sticky="e", padx=(SPACE_MD, SPACE_LG))
        self.accounts_tab_button = FlatButton(
            page_switcher,
            text="OpenAI 账号",
            command=lambda: self._show_page("accounts"),
            font_family=self.font_family,
            variant="secondary",
            compact=True,
        )
        self.accounts_tab_button.pack(side="left")
        self.deepseek_tab_button = FlatButton(
            page_switcher,
            text="DeepSeek",
            command=lambda: self._show_page("deepseek"),
            font_family=self.font_family,
            variant="ghost",
            compact=True,
        )
        self.deepseek_tab_button.pack(side="left", padx=(SPACE_XS, 0))

        tk.Label(
            header,
            text="v{}".format(APP_VERSION),
            font=(self.font_family, 8, "bold"),
            fg=COLORS["primary"],
            bg=COLORS["primary_soft"],
            padx=10,
            pady=5,
        ).grid(row=0, column=3, rowspan=2, sticky="e")

        import_card = card_frame(outer)
        self.import_card = import_card
        import_card.grid(row=1, column=0, sticky="ew", pady=(0, SPACE_LG))
        import_card.grid_columnconfigure(1, weight=1)

        tk.Frame(import_card, width=4, bg=COLORS["primary"]).grid(
            row=0,
            column=0,
            rowspan=2,
            sticky="nsw",
        )
        tk.Label(
            import_card,
            text="添加账号",
            font=(self.font_family, 12, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).grid(
            row=0,
            column=1,
            sticky="sw",
            padx=(SPACE_XL, SPACE_MD),
            pady=(SPACE_LG, 2),
        )
        tk.Label(
            import_card,
            text="保存认证副本，凭据仅保留在本机。",
            font=(self.font_family, 9),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            anchor="w",
        ).grid(
            row=1,
            column=1,
            sticky="nw",
            padx=(SPACE_XL, SPACE_MD),
            pady=(0, SPACE_LG),
        )

        import_actions = tk.Frame(import_card, bg=COLORS["surface"])
        import_actions.grid(
            row=0,
            column=2,
            rowspan=2,
            sticky="e",
            padx=SPACE_LG,
            pady=SPACE_MD,
        )
        import_actions.grid_columnconfigure(0, weight=1, uniform="import_action")
        import_actions.grid_columnconfigure(1, weight=1, uniform="import_action")
        self.add_current_button = FlatButton(
            import_actions,
            text="从当前 Codex 添加",
            command=self.add_from_current,
            font_family=self.font_family,
            variant="primary",
            compact=True,
        )
        self.add_current_button.grid(row=0, column=0, sticky="ew", padx=(0, SPACE_XS))
        self.add_file_button = FlatButton(
            import_actions,
            text="选择 auth.json",
            command=self.add_from_file,
            font_family=self.font_family,
            variant="secondary",
            compact=True,
        )
        self.add_file_button.grid(row=0, column=1, sticky="ew", padx=(SPACE_XS, 0))

        content = tk.Frame(outer, bg=COLORS["window"])
        content.grid(row=2, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=3, minsize=520)
        content.grid_columnconfigure(1, weight=0, minsize=314)
        content.grid_rowconfigure(0, weight=1)

        list_card = card_frame(content)
        self.list_card = list_card
        list_card.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE_LG))
        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_rowconfigure(1, weight=1)

        list_header = tk.Frame(list_card, bg=COLORS["surface"])
        self.list_header = list_header
        list_header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACE_XL,
            pady=(SPACE_MD, SPACE_SM),
        )
        list_header.grid_columnconfigure(1, weight=1)
        tk.Label(
            list_header,
            text="账号列表",
            font=(self.font_family, 12, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
        ).grid(row=0, column=0, sticky="w")
        self.count_label = tk.Label(
            list_header,
            text="0 个",
            font=(self.font_family, 8, "bold"),
            fg=COLORS["primary"],
            bg=COLORS["primary_soft"],
            padx=8,
            pady=3,
        )
        self.count_label.grid(row=0, column=1, sticky="w", padx=(9, 0))
        self.refresh_button = FlatButton(
            list_header,
            text="刷新列表",
            command=self.refresh_profiles,
            font_family=self.font_family,
            variant="secondary",
            compact=True,
        )
        self.refresh_button.grid(row=0, column=2, sticky="e")

        list_shell = tk.Frame(
            list_card,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self.list_shell = list_shell
        list_shell.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=SPACE_XL,
            pady=(0, SPACE_XL),
        )
        list_shell.grid_columnconfigure(0, weight=1)
        list_shell.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            list_shell,
            columns=("remark", "state", "updated"),
            show="headings",
            style="Profiles.Treeview",
            selectmode="browse",
        )
        self.tree.heading("remark", text="账号备注", anchor="w")
        self.tree.heading("state", text="状态", anchor="center")
        self.tree.heading("updated", text="最近更新", anchor="w")
        self.tree.column(
            "remark",
            minwidth=TABLE_REMARK_MIN_WIDTH,
            width=310,
            anchor="w",
            stretch=False,
        )
        self.tree.column(
            "state",
            minwidth=80,
            width=TABLE_STATE_WIDTH,
            anchor="center",
            stretch=False,
        )
        self.tree.column(
            "updated",
            minwidth=150,
            width=TABLE_UPDATED_WIDTH,
            anchor="w",
            stretch=False,
        )
        self.tree.tag_configure(
            "active",
            background=COLORS["row_success"],
            foreground=COLORS["success"],
        )
        self.tree.tag_configure(
            "missing",
            background=COLORS["row_danger"],
            foreground=COLORS["danger"],
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_selection)
        self.tree.bind("<Configure>", self._on_tree_resize, add="+")
        self.tree.bind("<Return>", lambda _event: self.switch_selected_profile())

        self.scrollbar = ttk.Scrollbar(
            list_shell,
            orient="vertical",
            command=self.tree.yview,
            style="App.Vertical.TScrollbar",
        )
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self.scrollbar.set)

        self.empty_panel = tk.Frame(list_shell, bg=COLORS["surface"])
        self.empty_panel.grid(row=0, column=0, sticky="nsew")
        self.empty_panel.grid_columnconfigure(0, weight=1)
        self.empty_panel.grid_rowconfigure(0, weight=1)
        empty_content = tk.Frame(self.empty_panel, bg=COLORS["surface"])
        empty_content.grid(row=0, column=0)
        tk.Label(
            empty_content,
            text="＋",
            font=(self.font_family, 22, "bold"),
            fg=COLORS["primary"],
            bg=COLORS["primary_soft"],
            width=2,
        ).pack()
        tk.Label(
            empty_content,
            text="尚未添加账号",
            font=(self.font_family, 11, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
        ).pack(pady=(9, 3))
        tk.Label(
            empty_content,
            text="可从当前 Codex 添加，或选择 auth.json。",
            font=(self.font_family, 9),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
        ).pack()

        detail_card = card_frame(content)
        self.detail_card = detail_card
        detail_card.grid(row=0, column=1, sticky="nsew")

        detail_body = tk.Frame(detail_card, bg=COLORS["surface"])
        detail_body.pack(
            fill="both",
            expand=True,
            padx=SPACE_XL,
            pady=SPACE_LG,
        )

        detail_top = tk.Frame(detail_body, bg=COLORS["surface"])
        detail_top.pack(fill="x")
        tk.Label(
            detail_top,
            text="账号操作",
            font=(self.font_family, 12, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
        ).pack(side="left")
        self.detail_badge = tk.Label(
            detail_top,
            text="未选择",
            font=(self.font_family, 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["surface_alt"],
            padx=8,
            pady=3,
        )
        self.detail_badge.pack(side="right")

        self.detail_title = tk.Label(
            detail_body,
            text="请选择账号",
            font=(self.font_family, 15, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=270,
        )
        self.detail_title.pack(fill="x", pady=(SPACE_MD, SPACE_SM))

        metadata = tk.Frame(detail_body, bg=COLORS["surface_alt"])
        metadata.pack(fill="x")
        metadata.grid_columnconfigure(1, weight=1)

        tk.Label(
            metadata,
            text="状态",
            font=(self.font_family, 8),
            fg=COLORS["muted"],
            bg=COLORS["surface_alt"],
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(SPACE_MD, SPACE_SM),
            pady=(SPACE_SM, SPACE_XS),
        )
        self.detail_status = tk.Label(
            metadata,
            text="—",
            font=(self.font_family, 9, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface_alt"],
            anchor="e",
        )
        self.detail_status.grid(
            row=0,
            column=1,
            sticky="e",
            padx=(SPACE_SM, SPACE_MD),
            pady=(SPACE_SM, SPACE_XS),
        )

        tk.Label(
            metadata,
            text="最近更新",
            font=(self.font_family, 8),
            fg=COLORS["muted"],
            bg=COLORS["surface_alt"],
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(SPACE_MD, SPACE_SM),
            pady=(SPACE_XS, SPACE_SM),
        )
        self.detail_updated = tk.Label(
            metadata,
            text="—",
            font=(self.font_family, 8),
            fg=COLORS["text"],
            bg=COLORS["surface_alt"],
            anchor="e",
        )
        self.detail_updated.grid(
            row=1,
            column=1,
            sticky="e",
            padx=(SPACE_SM, SPACE_MD),
            pady=(SPACE_XS, SPACE_SM),
        )

        self.update_hint = tk.Label(
            detail_body,
            text="选择账号后可切换、更新或管理。",
            font=(self.font_family, 8),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=270,
        )
        self.update_hint.pack(fill="x", pady=SPACE_SM)

        action_group = tk.Frame(detail_body, bg=COLORS["surface"])
        self.action_group = action_group
        action_group.pack(fill="x", padx=SPACE_SM)

        self.switch_button = FlatButton(
            action_group,
            text=SWITCH_BUTTON_TEXT,
            command=self.switch_selected_profile,
            font_family=self.font_family,
            variant="primary",
        )
        self.switch_button.pack(fill="x")

        self.update_button = FlatButton(
            action_group,
            text="从当前 Codex 更新",
            command=self.update_selected_profile,
            font_family=self.font_family,
            variant="secondary",
        )
        self.update_button.pack(fill="x", pady=(SPACE_SM, 0))

        maintenance = tk.Frame(action_group, bg=COLORS["surface"])
        self.maintenance_frame = maintenance
        maintenance.pack(fill="x", pady=(SPACE_SM, 0))
        maintenance.grid_columnconfigure(0, weight=1, uniform="maintenance_action")
        maintenance.grid_columnconfigure(1, weight=1, uniform="maintenance_action")
        self.rename_button = FlatButton(
            maintenance,
            text="修改备注",
            command=self.rename_selected_profile,
            font_family=self.font_family,
            variant="ghost",
            compact=True,
        )
        self.rename_button.grid(row=0, column=0, sticky="ew", padx=(0, SPACE_XS))
        self.remove_button = FlatButton(
            maintenance,
            text="移除",
            command=self.remove_selected_profile,
            font_family=self.font_family,
            variant="danger",
            compact=True,
        )
        self.remove_button.grid(row=0, column=1, sticky="ew", padx=(SPACE_XS, 0))

        for button in (
            self.switch_button,
            self.update_button,
            self.rename_button,
            self.remove_button,
        ):
            button.set_enabled(False)

        self.account_content = content
        self.deepseek_page = tk.Frame(outer, bg=COLORS["window"])
        self.deepseek_page.grid(
            row=1,
            column=0,
            rowspan=2,
            sticky="nsew",
        )
        self.deepseek_page.grid_columnconfigure(0, weight=3, minsize=510)
        self.deepseek_page.grid_columnconfigure(1, weight=2, minsize=280)
        self.deepseek_page.grid_rowconfigure(0, weight=1)

        provider_card = card_frame(self.deepseek_page)
        provider_card.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, SPACE_LG),
        )
        provider_card.grid_columnconfigure(1, weight=1)
        provider_card.grid_rowconfigure(0, weight=1)
        tk.Frame(provider_card, width=4, bg=COLORS["teal"]).grid(
            row=0,
            column=0,
            sticky="nsw",
        )

        provider_body = tk.Frame(provider_card, bg=COLORS["surface"])
        provider_body.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=SPACE_2XL,
            pady=SPACE_XL,
        )

        provider_top = tk.Frame(provider_body, bg=COLORS["surface"])
        provider_top.pack(fill="x")
        tk.Label(
            provider_top,
            text="模型服务",
            font=(self.font_family, 10, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
        ).pack(side="left")
        self.provider_badge = tk.Label(
            provider_top,
            text="正在检测",
            font=(self.font_family, 8, "bold"),
            fg=COLORS["muted"],
            bg=COLORS["surface_alt"],
            padx=9,
            pady=4,
        )
        self.provider_badge.pack(side="right")

        tk.Label(
            provider_body,
            text="DeepSeek V4 Flash",
            font=(self.font_family, 20, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x", pady=(SPACE_MD, 4))
        tk.Label(
            provider_body,
            text="通过 DeepSeek Responses API 使用 Codex。",
            font=(self.font_family, 9),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x")

        provider_metadata = tk.Frame(provider_body, bg=COLORS["surface_alt"])
        provider_metadata.pack(fill="x", pady=(SPACE_XL, SPACE_MD))
        provider_metadata.grid_columnconfigure(1, weight=1)

        for row, label_text in enumerate(("当前模式", "API Key", "配置状态")):
            tk.Label(
                provider_metadata,
                text=label_text,
                font=(self.font_family, 8),
                fg=COLORS["muted"],
                bg=COLORS["surface_alt"],
                anchor="w",
            ).grid(
                row=row,
                column=0,
                sticky="w",
                padx=(SPACE_MD, SPACE_SM),
                pady=(SPACE_SM if row == 0 else SPACE_XS, SPACE_SM if row == 2 else SPACE_XS),
            )

        self.provider_mode_value = tk.Label(
            provider_metadata,
            text="OpenAI",
            font=(self.font_family, 9, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface_alt"],
            anchor="e",
        )
        self.provider_mode_value.grid(
            row=0,
            column=1,
            sticky="e",
            padx=(SPACE_SM, SPACE_MD),
            pady=(SPACE_SM, SPACE_XS),
        )
        self.provider_key_value = tk.Label(
            provider_metadata,
            text="未保存",
            font=(self.font_family, 9, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface_alt"],
            anchor="e",
        )
        self.provider_key_value.grid(
            row=1,
            column=1,
            sticky="e",
            padx=(SPACE_SM, SPACE_MD),
            pady=SPACE_XS,
        )
        self.provider_config_value = tk.Label(
            provider_metadata,
            text="正在检测",
            font=(self.font_family, 9, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface_alt"],
            anchor="e",
        )
        self.provider_config_value.grid(
            row=2,
            column=1,
            sticky="e",
            padx=(SPACE_SM, SPACE_MD),
            pady=(SPACE_XS, SPACE_SM),
        )

        self.provider_hint = tk.Label(
            provider_body,
            text="切换后将自动重启 Codex。",
            font=(self.font_family, 8),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=470,
        )
        self.provider_hint.pack(fill="x", pady=(0, SPACE_MD))

        provider_actions = tk.Frame(provider_body, bg=COLORS["surface"])
        provider_actions.pack(fill="x", side="bottom")
        self.provider_action_button = FlatButton(
            provider_actions,
            text=PROVIDER_BUTTON_DEEPSEEK,
            command=self.toggle_provider,
            font_family=self.font_family,
            variant="primary",
        )
        self.provider_action_button.pack(fill="x")
        self.provider_key_button = FlatButton(
            provider_actions,
            text="设置 API Key",
            command=self.configure_deepseek_key,
            font_family=self.font_family,
            variant="secondary",
        )
        self.provider_key_button.pack(fill="x", pady=(SPACE_SM, 0))

        guide_card = card_frame(self.deepseek_page)
        guide_card.grid(row=0, column=1, sticky="nsew")
        guide_body = tk.Frame(guide_card, bg=COLORS["surface"])
        guide_body.pack(fill="both", expand=True, padx=SPACE_XL, pady=SPACE_XL)
        tk.Label(
            guide_body,
            text="切换流程",
            font=(self.font_family, 12, "bold"),
            fg=COLORS["text"],
            bg=COLORS["surface"],
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            guide_body,
            text="切换期间，正在运行的任务可能会中断。",
            font=(self.font_family, 8),
            fg=COLORS["muted"],
            bg=COLORS["surface"],
            justify="left",
            anchor="w",
            wraplength=260,
        ).pack(fill="x", pady=(4, SPACE_LG))

        for number, description in (
            ("1", "关闭 Microsoft Store 版 Codex"),
            ("2", "安全更新模型服务配置"),
            ("3", "重新启动并确认客户端状态"),
        ):
            row = tk.Frame(guide_body, bg=COLORS["surface"])
            row.pack(fill="x", pady=(0, SPACE_MD))
            tk.Label(
                row,
                text=number,
                font=(self.font_family, 9, "bold"),
                fg=COLORS["teal"],
                bg=COLORS["teal_soft"],
                width=2,
                pady=3,
            ).pack(side="left")
            tk.Label(
                row,
                text=description,
                font=(self.font_family, 9),
                fg=COLORS["text"],
                bg=COLORS["surface"],
                anchor="w",
                wraplength=220,
            ).pack(side="left", fill="x", expand=True, padx=(SPACE_SM, 0))

        support_note = tk.Frame(guide_body, bg=COLORS["primary_soft"])
        support_note.pack(fill="x", side="bottom", pady=(SPACE_MD, 0))
        tk.Label(
            support_note,
            text="当前支持",
            font=(self.font_family, 8, "bold"),
            fg=COLORS["primary"],
            bg=COLORS["primary_soft"],
        ).pack(anchor="w", padx=SPACE_MD, pady=(SPACE_SM, 2))
        tk.Label(
            support_note,
            text="DeepSeek V4 Flash",
            font=(self.font_family, 9, "bold"),
            fg=COLORS["text"],
            bg=COLORS["primary_soft"],
        ).pack(anchor="w", padx=SPACE_MD, pady=(0, SPACE_SM))

        self.provider_refresh_button = FlatButton(
            guide_body,
            text="重新检测",
            command=lambda: self.refresh_provider_status(show_error=True),
            font_family=self.font_family,
            variant="ghost",
            compact=True,
        )
        self.provider_refresh_button.pack(side="bottom", fill="x", pady=(SPACE_LG, 0))
        self.provider_action_button.set_enabled(False)
        self.deepseek_page.grid_remove()

        footer = tk.Frame(outer, bg=COLORS["window"])
        footer.grid(row=3, column=0, sticky="ew", pady=(11, 0))
        footer.grid_columnconfigure(1, weight=1)
        tk.Label(
            footer,
            text="配置目录",
            font=(self.font_family, 8, "bold"),
            fg=COLORS["primary"],
            bg=COLORS["window"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        tk.Label(
            footer,
            text=str(self.codex_home),
            font=(self.mono_font, 8),
            fg=COLORS["muted"],
            bg=COLORS["window"],
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            footer,
            text="本机配置含敏感凭据，请勿分享。",
            font=(self.font_family, 8),
            fg=COLORS["muted_light"],
            bg=COLORS["window"],
            anchor="e",
        ).grid(row=0, column=2, sticky="e")

        self.root.bind("<F5>", lambda _event: self.refresh_all())

    def _show_page(self, page: str) -> None:
        if page not in ("accounts", "deepseek"):
            return
        self.current_page = page
        if page == "accounts":
            self.deepseek_page.grid_remove()
            self.import_card.grid()
            self.account_content.grid()
        else:
            self.import_card.grid_remove()
            self.account_content.grid_remove()
            self.deepseek_page.grid()
            self.refresh_provider_status(show_error=False)
        self.accounts_tab_button.set_selected(page == "accounts")
        self.deepseek_tab_button.set_selected(page == "deepseek")
        self.accounts_tab_button.set_enabled(not self.busy)
        self.deepseek_tab_button.set_enabled(not self.busy)

    def refresh_all(self) -> None:
        if self.busy:
            return
        self.refresh_profiles()
        self.refresh_provider_status(show_error=False)

    def _read_api_key(self) -> Optional[str]:
        if self.credential_store is None:
            raise CredentialStoreError(
                self.credential_initialization_error or "Windows 凭据管理器不可用。"
            )
        return self.credential_store.read()

    def refresh_provider_status(self, show_error: bool = False) -> None:
        if self.provider_manager is None:
            self.provider_status_error = (
                self.provider_initialization_error or "DeepSeek 配置组件不可用。"
            )
            self._render_provider_status(None)
            if show_error:
                self.show_notice("无法检测配置", self.provider_status_error, "error")
            return
        try:
            status = self.provider_manager.status()
        except (ProviderConfigError, OSError) as exc:
            self.provider_status_error = str(exc)
            self._render_provider_status(None)
            if show_error:
                self.show_notice("无法检测配置", self.provider_status_error, "error")
            return

        api_key = None
        self.credential_status_error = self.credential_initialization_error
        if self.credential_store is not None:
            try:
                api_key = self._read_api_key()
            except CredentialStoreError as exc:
                self.credential_status_error = str(exc)

        self.provider_status = status
        self.provider_status_error = ""
        self._render_provider_status(api_key)
        self._render_selection()

    def _render_provider_status(self, api_key: Optional[str]) -> None:
        if self.provider_status_error:
            self.provider_badge.configure(
                text="配置异常",
                fg=COLORS["danger"],
                bg=COLORS["danger_soft"],
            )
            self.provider_mode_value.configure(text="—", fg=COLORS["text"])
            self.provider_key_value.configure(text="—", fg=COLORS["text"])
            self.provider_config_value.configure(
                text="需要处理",
                fg=COLORS["danger"],
            )
            self.provider_hint.configure(
                text=self.provider_status_error,
                fg=COLORS["danger"],
            )
            self.provider_action_button.set_enabled(False)
            self.provider_key_button.set_enabled(False)
            return

        is_deepseek = self.provider_status.mode == "deepseek"
        self.provider_mode_value.configure(
            text="DeepSeek" if is_deepseek else "OpenAI",
            fg=COLORS["teal"] if is_deepseek else COLORS["primary"],
        )
        if self.credential_status_error:
            self.provider_key_value.configure(
                text="需要更新",
                fg=COLORS["danger"],
            )
        else:
            self.provider_key_value.configure(
                text=masked_api_key(api_key),
                fg=COLORS["success"] if api_key else COLORS["muted"],
            )

        if self.provider_status.conflict:
            badge_text = "配置已变更"
            badge_fg = COLORS["danger"]
            badge_bg = COLORS["danger_soft"]
            config_text = "需要确认"
            hint = "检测到外部修改。为避免覆盖，请先恢复或重新配置。"
            action_enabled = False
        elif is_deepseek and not self.provider_status.managed:
            badge_text = "当前使用"
            badge_fg = COLORS["teal"]
            badge_bg = COLORS["teal_soft"]
            if self.provider_status.external_backup_available:
                config_text = "可安全恢复"
                hint = "检测到官方备份，可安全切回 OpenAI。"
                action_enabled = True
            else:
                config_text = "外部配置"
                hint = "未找到原始 OpenAI 配置，暂不能安全切回。"
                action_enabled = False
        elif is_deepseek:
            badge_text = "当前使用"
            badge_fg = COLORS["teal"]
            badge_bg = COLORS["teal_soft"]
            config_text = "已接管"
            hint = "切回时将恢复原有 OpenAI 设置。"
            action_enabled = True
        else:
            badge_text = "可用"
            badge_fg = COLORS["primary"]
            badge_bg = COLORS["primary_soft"]
            config_text = "就绪"
            hint = "切换后将自动重启 Codex。"
            action_enabled = True

        if not is_deepseek and (self.credential_status_error or self.credential_store is None):
            hint = "请先重新设置 DeepSeek API Key。"
            action_enabled = False

        self.provider_badge.configure(
            text=badge_text,
            fg=badge_fg,
            bg=badge_bg,
        )
        self.provider_config_value.configure(text=config_text, fg=badge_fg)
        self.provider_hint.configure(
            text=hint, fg=badge_fg if not action_enabled else COLORS["muted"]
        )
        self.provider_action_button.configure(
            text=PROVIDER_BUTTON_OPENAI if is_deepseek else PROVIDER_BUTTON_DEEPSEEK
        )
        self.provider_key_button.configure(
            text=(
                "重新设置 API Key"
                if self.credential_status_error
                else "更新 API Key"
                if api_key
                else "设置 API Key"
            )
        )
        enabled = not self.busy
        provider_can_be_managed = not (
            is_deepseek
            and not self.provider_status.managed
            and not self.provider_status.external_backup_available
        )
        self.provider_action_button.set_enabled(enabled and action_enabled)
        self.provider_key_button.set_enabled(
            enabled
            and self.credential_store is not None
            and provider_can_be_managed
            and not self.provider_status.conflict
        )
        self.provider_refresh_button.set_enabled(enabled)
        if self.provider_in_progress:
            self.provider_action_button.set_progress(self.provider_button_text)

    def configure_deepseek_key(self) -> None:
        if self.busy:
            return
        if self.credential_store is None:
            self.show_notice(
                "凭据管理器不可用",
                self.credential_initialization_error or "无法访问 Windows 凭据管理器。",
                "error",
            )
            return
        try:
            current_key = self._read_api_key()
        except CredentialStoreError:
            current_key = None

        dialog = ApiKeyDialog(
            self.root,
            self.font_family,
            current_key or "",
            allow_delete=(current_key is not None and self.provider_status.mode != "deepseek"),
        )
        if dialog.deleted:
            if not self.show_confirm(
                "移除 API Key",
                "将从 Windows 凭据管理器移除 DeepSeek API Key。",
                "确认移除",
                destructive=True,
            ):
                return
            self._set_busy(True)
            try:
                self.credential_store.delete()
            except CredentialStoreError as exc:
                error_message = str(exc)
            else:
                error_message = ""
            finally:
                self._set_busy(False)
            self.refresh_provider_status(show_error=False)
            if error_message:
                self.show_notice("移除失败", error_message, "error")
            else:
                self.show_notice("API Key 已移除", "已删除本机保存的 DeepSeek API Key。", "success")
            return
        if dialog.result is None or dialog.result == current_key:
            return

        if self.provider_status.mode == "deepseek":
            if not self.show_confirm(
                "更新 API Key",
                "将更新 DeepSeek API Key，并重启 Codex。\n正在运行的任务可能会中断。",
                "更新并重启",
            ):
                return
            self._begin_provider_switch("deepseek", dialog.result, "update_key")
            return

        self._set_busy(True)
        try:
            self.credential_store.write(dialog.result)
        except CredentialStoreError as exc:
            error_message = str(exc)
        else:
            error_message = ""
        finally:
            self._set_busy(False)
        self.refresh_provider_status(show_error=False)
        if error_message:
            self.show_notice("保存失败", error_message, "error")
        else:
            self.show_notice("API Key 已保存", "DeepSeek API Key 已安全保存。", "success")

    def toggle_provider(self) -> None:
        if self.busy:
            return
        self.refresh_provider_status(show_error=False)
        if self.provider_status_error:
            self.show_notice("无法切换", self.provider_status_error, "error")
            return
        if self.provider_status.conflict:
            self.show_notice(
                "配置已变更",
                "检测到 Codex 配置已在外部修改。为避免覆盖，本次操作已取消。",
                "warning",
            )
            return

        if self.provider_status.mode == "deepseek":
            if not self.show_confirm(
                "切回 OpenAI",
                "将恢复 OpenAI 配置，并重启 Codex。\n正在运行的任务可能会中断。",
                "切回并重启",
            ):
                return
            self._begin_provider_switch("openai", None, "switch")
            return

        try:
            api_key = self._read_api_key()
        except CredentialStoreError as exc:
            self.show_notice("无法读取 API Key", str(exc), "error")
            return
        if api_key is None:
            dialog = ApiKeyDialog(self.root, self.font_family)
            if dialog.result is None:
                return
            api_key = dialog.result
        if not self.show_confirm(
            "切换到 DeepSeek",
            "将切换到 DeepSeek V4 Flash，并重启 Codex。\n正在运行的任务可能会中断。",
            "切换并重启",
        ):
            return
        self._begin_provider_switch("deepseek", api_key, "switch")

    def _set_provider_progress(self, button_text: str, message: str) -> None:
        self.provider_in_progress = True
        self.provider_button_text = button_text
        self._set_busy(True, message)
        self.provider_action_button.set_progress(button_text)
        self.provider_hint.configure(text=message, fg=COLORS["primary"])

    def _begin_provider_switch(
        self,
        target_mode: str,
        api_key: Optional[str],
        action: str,
    ) -> None:
        self._set_provider_progress("正在准备…", "正在准备切换…")
        worker = threading.Thread(
            target=self._provider_switch_worker,
            args=(target_mode, api_key, action),
            name="ProviderSwitchWorker",
            daemon=False,
        )
        worker.start()
        self._schedule_provider_poll()

    def _provider_switch_worker(
        self,
        target_mode: str,
        api_key: Optional[str],
        action: str,
    ) -> None:
        client = self.codex_client
        if client is None:
            client = StoreCodexClient()
        previous_key: Optional[str] = None
        previous_key_unreadable = False
        credential_changed = False
        stopped = False
        try:
            try:
                previous_key = self._read_api_key()
            except CredentialStoreError:
                previous_key = None
                previous_key_unreadable = True
            self.provider_queue.put(
                {"status": "progress", "message": "正在关闭 Codex…", "button": "正在关闭…"}
            )
            stop_result = client.stop()
            stopped = True

            self.provider_queue.put(
                {"status": "progress", "message": "正在更新模型服务…", "button": "正在切换…"}
            )
            if target_mode == "deepseek":
                if api_key is None:
                    raise CredentialStoreError("未找到可用的 DeepSeek API Key。")
                normalized_key = normalize_api_key(api_key)
                if normalized_key != previous_key:
                    self.credential_store.write(normalized_key)
                    credential_changed = True
                switch_result = self.provider_manager.activate_deepseek(normalized_key)
            else:
                switch_result = self.provider_manager.restore_openai()
        except (ProviderConfigError, CredentialStoreError, InvalidApiKeyError) as exc:
            recovery_notes = []
            if credential_changed:
                try:
                    if previous_key is None or previous_key_unreadable:
                        self.credential_store.delete()
                    else:
                        self.credential_store.write(previous_key)
                except CredentialStoreError:
                    recovery_notes.append("API Key 未能恢复。")
            if stopped:
                try:
                    client.start()
                except Exception:
                    recovery_notes.append("请手动启动 Codex。")
            message = str(exc)
            if recovery_notes:
                message += "\n" + " ".join(recovery_notes)
            self.provider_queue.put({"status": "provider_error", "message": message})
            return
        except Exception:
            if credential_changed:
                try:
                    if previous_key is None or previous_key_unreadable:
                        self.credential_store.delete()
                    else:
                        self.credential_store.write(previous_key)
                except CredentialStoreError:
                    pass
            if stopped:
                try:
                    client.start()
                except Exception:
                    pass
            self.provider_queue.put(
                {"status": "provider_error", "message": "系统未能完成模型服务切换。"}
            )
            return

        self.provider_queue.put(
            {"status": "progress", "message": "正在启动 Codex…", "button": "正在启动…"}
        )
        try:
            launched_pid = client.start()
        except CodexClientError as exc:
            self.provider_queue.put(
                {
                    "status": "provider_restart_error",
                    "target_mode": target_mode,
                    "action": action,
                    "message": str(exc) or "系统未能启动 Codex。",
                }
            )
            return
        except Exception:
            self.provider_queue.put(
                {
                    "status": "provider_restart_error",
                    "target_mode": target_mode,
                    "action": action,
                    "message": "系统未能启动 Codex。",
                }
            )
            return
        self.provider_queue.put(
            {
                "status": "provider_success",
                "target_mode": target_mode,
                "action": action,
                "changed": switch_result.changed,
                "result": RestartResult(
                    was_running=stop_result.was_running,
                    forced_close=stop_result.forced_close,
                    launched_pid=launched_pid,
                ),
            }
        )

    def _schedule_provider_poll(self) -> None:
        if self.provider_poll_id is None:
            self.provider_poll_id = self.root.after(100, self._poll_provider_result)

    def _poll_provider_result(self) -> None:
        self.provider_poll_id = None
        try:
            outcome = self.provider_queue.get_nowait()
        except queue.Empty:
            if self.busy:
                self._schedule_provider_poll()
            return

        if outcome.get("status") == "progress":
            self._set_provider_progress(outcome["button"], outcome["message"])
            self._schedule_provider_poll()
            return

        self.provider_in_progress = False
        self.provider_button_text = ""
        self._set_busy(False)
        self.refresh_provider_status(show_error=False)
        self.refresh_profiles()

        status = outcome.get("status")
        if status == "provider_error":
            self.show_notice("切换失败", outcome["message"], "error")
            return
        target_mode = outcome.get("target_mode", "openai")
        target_name = "DeepSeek" if target_mode == "deepseek" else "OpenAI"
        if status == "provider_restart_error":
            self.show_notice(
                "配置已更新",
                "已切换到 {}，但 Codex 未能自动启动。\n请手动启动 Codex。\n\n原因：{}".format(
                    target_name,
                    outcome["message"],
                ),
                "warning",
            )
            return

        restart_result = outcome.get("result")
        action = outcome.get("action")
        if not isinstance(restart_result, RestartResult):
            self.show_notice(
                "配置已更新",
                "模型服务已更新，但未能确认 Codex 状态。",
                "warning",
            )
            return
        client_action = "已重新启动" if restart_result.was_running else "已启动"
        if action == "update_key":
            title = "API Key 已更新"
            message = "DeepSeek API Key 已更新，Codex {}。".format(client_action)
        elif target_mode == "deepseek":
            title = "已切换到 DeepSeek"
            message = "Codex {}。".format(client_action)
        else:
            title = "已切回 OpenAI"
            message = "Codex {}。".format(client_action)
        self.show_notice(title, message, "success")

    def show_notice(self, title: str, message: str, kind: str = "info") -> None:
        NoticeDialog(self.root, title, message, self.font_family, kind)

    def show_confirm(
        self,
        title: str,
        message: str,
        confirm_text: str,
        destructive: bool = False,
    ) -> bool:
        dialog = ConfirmDialog(
            self.root,
            title,
            message,
            self.font_family,
            confirm_text,
            destructive,
        )
        return dialog.result

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        self.busy_message = message if busy else ""
        self.root.configure(cursor="watch" if busy else "")
        self.add_file_button.set_enabled(not busy)
        self.add_current_button.set_enabled(not busy)
        self.refresh_button.set_enabled(not busy)
        self.accounts_tab_button.set_enabled(not busy)
        self.deepseek_tab_button.set_enabled(not busy)
        self.provider_action_button.set_enabled(not busy)
        self.provider_key_button.set_enabled(not busy)
        self.provider_refresh_button.set_enabled(not busy)
        self._render_selection()
        if not busy and not self.provider_in_progress:
            self.refresh_provider_status(show_error=False)
        self.root.update_idletasks()

    def _set_restart_progress(self, button_text: str, message: str) -> None:
        self.restart_in_progress = True
        self.restart_button_text = button_text
        self._set_busy(True, message)
        self.switch_button.set_progress(button_text)

    def _on_root_destroy(self, event) -> None:
        if event.widget is not self.root:
            return
        for attribute in ("restart_poll_id", "provider_poll_id"):
            poll_id = getattr(self, attribute)
            if poll_id is None:
                continue
            try:
                self.root.after_cancel(poll_id)
            except tk.TclError:
                pass
            setattr(self, attribute, None)

    def _request_close(self) -> None:
        if self.busy:
            self.root.bell()
            return
        self.root.destroy()

    def _format_time(self, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return value[:16].replace("T", " ")

    def _selected_profile(self) -> Optional[AccountProfile]:
        selection = self.tree.selection()
        if not selection:
            return None
        profile_id = selection[0]
        return next(
            (item for item in self.profiles if item.profile_id == profile_id),
            None,
        )

    def refresh_profiles(self, select_profile_id: Optional[str] = None) -> None:
        if self.busy:
            return
        previous_selection = select_profile_id
        if previous_selection is None and self.tree.selection():
            previous_selection = self.tree.selection()[0]

        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            self.profiles = self.store.load_profiles()
            self.active_profile_id = self.store.active_profile_id(self.target_auth_path)
        except AccountStoreError as exc:
            self.profiles = []
            self.active_profile_id = None
            self.empty_panel.grid()
            self.scrollbar.grid_remove()
            self.count_label.configure(text="0 个")
            self._render_selection()
            self.show_notice("无法读取账号列表", str(exc), "error")
            return

        sorted_profiles = sorted(
            self.profiles,
            key=lambda item: item.added_at,
            reverse=True,
        )
        for profile in sorted_profiles:
            available = self.store.profile_is_available(profile)
            if not available:
                state = "文件缺失"
                tags = ("missing",)
            elif profile.profile_id == self.active_profile_id:
                state = "当前"
                tags = ("active",)
            else:
                state = "可用"
                tags = ()
            self.tree.insert(
                "",
                "end",
                iid=profile.profile_id,
                values=(
                    format_table_cell(profile.remark),
                    state,
                    format_table_cell(self._format_time(profile.updated_at)),
                ),
                tags=tags,
            )

        self.count_label.configure(text="{} 个".format(len(self.profiles)))
        if self.profiles:
            self.empty_panel.grid_remove()
            if len(self.profiles) > 5:
                self.scrollbar.grid()
            else:
                self.scrollbar.grid_remove()
        else:
            self.empty_panel.grid()
            self.scrollbar.grid_remove()

        target_selection = previous_selection or self.active_profile_id
        if target_selection and self.tree.exists(target_selection):
            self.tree.selection_set(target_selection)
            self.tree.focus(target_selection)
            self.tree.see(target_selection)
        self._render_selection()

        if self.store.recovered_from_backup:
            self.show_notice(
                "账号列表已恢复",
                "主列表无法读取，已使用本地备份。后续保存操作会重建主列表。",
                "warning",
            )

    def _on_selection(self, _event=None) -> None:
        self._render_selection()

    def _on_tree_resize(self, event) -> None:
        remark_width, state_width, updated_width = table_column_widths(event.width)
        self.tree.column("remark", width=remark_width)
        self.tree.column("state", width=state_width)
        self.tree.column("updated", width=updated_width)

    def _render_selection(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.detail_badge.configure(
                text="未选择",
                fg=COLORS["muted"],
                bg=COLORS["surface_alt"],
            )
            self.detail_title.configure(text="请选择账号")
            self.detail_status.configure(text="—", fg=COLORS["text"])
            self.detail_updated.configure(text="—")
            self.update_hint.configure(
                text="选择账号后可切换、更新或管理。",
                fg=COLORS["muted"],
            )
            self.switch_button.configure(text=SWITCH_BUTTON_TEXT)
            for button in (
                self.switch_button,
                self.update_button,
                self.rename_button,
                self.remove_button,
            ):
                button.set_enabled(False)
            if self.restart_in_progress:
                self.switch_button.set_progress(self.restart_button_text)
            return

        available = self.store.profile_is_available(profile)
        deepseek_active = self.provider_status.mode == "deepseek"
        is_active = (
            profile.profile_id == self.active_profile_id and available and not deepseek_active
        )
        if not available:
            badge_text = "文件缺失"
            badge_fg = COLORS["danger"]
            badge_bg = COLORS["danger_soft"]
            status_text = "认证副本缺失"
            hint_text = "可用当前 Codex 认证恢复此账号。"
            hint_color = COLORS["danger"]
        elif is_active:
            badge_text = "当前"
            badge_fg = COLORS["success"]
            badge_bg = COLORS["success_soft"]
            status_text = "当前使用"
            hint_text = "可重新启动 Codex，或更新此认证副本。"
            hint_color = COLORS["muted"]
        else:
            badge_text = "待使用" if deepseek_active else "可用"
            badge_fg = COLORS["primary"]
            badge_bg = COLORS["primary_soft"]
            status_text = "OpenAI 账号" if deepseek_active else "可切换"
            hint_text = (
                "切换时将恢复 OpenAI，并重启 Codex。"
                if deepseek_active
                else "切换后将自动重启 Codex。"
            )
            hint_color = COLORS["muted"]

        self.detail_badge.configure(text=badge_text, fg=badge_fg, bg=badge_bg)
        self.detail_title.configure(text=profile.remark)
        self.detail_status.configure(text=status_text, fg=badge_fg)
        self.detail_updated.configure(text=self._format_time(profile.updated_at))
        self.update_hint.configure(text=hint_text, fg=hint_color)
        self.switch_button.configure(
            text="切回并使用此账号" if deepseek_active else SWITCH_BUTTON_TEXT
        )

        if self.busy and self.busy_message:
            self.detail_badge.configure(
                text="处理中",
                fg=COLORS["primary"],
                bg=COLORS["primary_soft"],
            )
            self.update_hint.configure(
                text=self.busy_message,
                fg=COLORS["primary"],
            )

        enabled = not self.busy
        self.switch_button.set_enabled(enabled and available)
        self.update_button.set_enabled(enabled and self.target_auth_path.is_file())
        self.rename_button.set_enabled(enabled)
        self.remove_button.set_enabled(enabled)
        if self.restart_in_progress:
            self.switch_button.set_progress(self.restart_button_text)

    def _begin_import(self, source_path: Path, source_label: str) -> None:
        if self.busy:
            return
        try:
            source_hash = validate_auth_file(source_path)
        except InvalidAuthFileError as exc:
            self.show_notice("无法添加账号", str(exc), "error")
            return

        matching = next(
            (
                profile
                for profile in self.profiles
                if profile.sha256 == source_hash and self.store.profile_is_available(profile)
            ),
            None,
        )
        if matching is not None:
            self.refresh_profiles(matching.profile_id)
            self.show_notice(
                "账号已存在",
                "相同的认证文件已保存为“{}”。".format(matching.remark),
                "info",
            )
            return

        remark_dialog = RemarkDialog(
            self.root,
            self.font_family,
            "添加账号",
            "请输入便于识别的账号备注。\n来源：{}".format(source_label),
            "添加",
        )
        if remark_dialog.result is None:
            return

        self._set_busy(True)
        try:
            result = self.store.import_profile(source_path, remark_dialog.result)
        except AccountStoreError as exc:
            error_message = str(exc)
            result = None
        else:
            error_message = None
        finally:
            self._set_busy(False)

        if error_message is not None:
            self.show_notice("添加失败", error_message, "error")
            return
        if result is None:
            return

        self.refresh_profiles(result.profile.profile_id)
        if result.created:
            self.show_notice(
                "账号已添加",
                "已保存“{}”。".format(result.profile.remark),
                "success",
            )
        else:
            self.show_notice(
                "账号已存在",
                "相同的认证文件已保存为“{}”。".format(result.profile.remark),
                "info",
            )

    def add_from_current(self) -> None:
        self._begin_import(self.target_auth_path, "当前 Codex")

    def add_from_file(self) -> None:
        if self.busy:
            return
        initial_directory = Path.home() / ".codex"
        if not initial_directory.is_dir():
            initial_directory = Path.home()

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择 Codex 认证文件",
            initialdir=str(initial_directory),
            initialfile="auth.json",
            filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
        )
        if selected:
            self._begin_import(Path(selected), Path(selected).name)

    def switch_selected_profile(self) -> None:
        if self.busy:
            return
        profile = self._selected_profile()
        if profile is None:
            self.show_notice("未选择账号", "请先选择一个账号。", "warning")
            return

        self.refresh_provider_status(show_error=False)
        if self.provider_status_error:
            self.show_notice("无法切换", self.provider_status_error, "error")
            return
        if self.provider_status.conflict:
            self.show_notice(
                "配置已变更",
                "检测到 Codex 配置已在外部修改。为避免覆盖，本次操作已取消。",
                "warning",
            )
            return
        if (
            self.provider_status.mode == "deepseek"
            and not self.provider_status.managed
            and not self.provider_status.external_backup_available
        ):
            self.show_notice(
                "无法切回 OpenAI",
                "未找到原始 OpenAI 配置。请先使用原配置工具完成恢复。",
                "warning",
            )
            return
        deepseek_active = self.provider_status.mode == "deepseek"
        confirm_title = "切回 OpenAI 并切换账号" if deepseek_active else "切换账号"
        confirm_message = (
            "将切回 OpenAI，切换到“{}”，并重启 Codex。\n正在运行的任务可能会中断。"
            if deepseek_active
            else "将切换到“{}”，并重启 Codex。\n正在运行的任务可能会中断。"
        ).format(profile.remark)

        if not self.show_confirm(
            confirm_title,
            confirm_message,
            "切换并重启",
        ):
            return

        self._set_restart_progress("正在准备…", "正在准备切换…")
        worker = threading.Thread(
            target=self._restart_codex_worker,
            args=(profile.profile_id, profile.remark),
            name="CodexRestartWorker",
            daemon=False,
        )
        worker.start()
        self._schedule_restart_poll()

    def _restart_codex_worker(self, profile_id: str, remark: str) -> None:
        client = self.codex_client
        if client is None:
            client = StoreCodexClient()
        try:
            provider_status = self.provider_manager.status()
            if provider_status.conflict:
                raise ProviderConfigError("Codex 配置已在外部修改。为避免覆盖，本次操作已取消。")
        except (ProviderConfigError, AttributeError) as exc:
            self.restart_queue.put(
                {
                    "status": "switch_error",
                    "remark": remark,
                    "message": str(exc) or "无法检测当前模型服务。",
                    "client_stopped": False,
                }
            )
            return
        provider_was_deepseek = provider_status.mode == "deepseek"

        try:
            self.restart_queue.put(
                {"status": "progress", "message": "正在关闭 Codex…", "button": "正在关闭…"}
            )
            stop_result = client.stop()
        except CodexClientError as exc:
            self.restart_queue.put(
                {
                    "status": "switch_error",
                    "remark": remark,
                    "message": str(exc),
                    "client_stopped": False,
                }
            )
            return
        except Exception:
            self.restart_queue.put(
                {
                    "status": "switch_error",
                    "remark": remark,
                    "message": "系统未能关闭 Codex。",
                    "client_stopped": False,
                }
            )
            return

        self.restart_queue.put(
            {"status": "progress", "message": "正在更新认证文件…", "button": "正在切换…"}
        )
        try:
            switch_result = self.store.switch_profile(profile_id, self.target_auth_path)
        except AccountStoreError as exc:
            recovery_error = ""
            try:
                client.start()
            except Exception:
                recovery_error = "\nCodex 未能重新启动，请手动启动。"
            self.restart_queue.put(
                {
                    "status": "switch_error",
                    "remark": remark,
                    "message": str(exc) + recovery_error,
                    "client_stopped": True,
                }
            )
            return
        except Exception:
            recovery_error = ""
            try:
                client.start()
            except Exception:
                recovery_error = "\nCodex 未能重新启动，请手动启动。"
            self.restart_queue.put(
                {
                    "status": "switch_error",
                    "remark": remark,
                    "message": "系统未能更新认证文件。" + recovery_error,
                    "client_stopped": True,
                }
            )
            return

        if provider_was_deepseek:
            self.restart_queue.put(
                {
                    "status": "progress",
                    "message": "正在恢复 OpenAI 配置…",
                    "button": "正在切回…",
                }
            )
            try:
                self.provider_manager.restore_openai()
            except ProviderConfigError as exc:
                recovery_notes = []
                try:
                    self.store.rollback_switch(switch_result)
                except AccountStoreError:
                    recovery_notes.append("原认证文件未能恢复。")
                try:
                    client.start()
                except Exception:
                    recovery_notes.append("请手动启动 Codex。")
                message = str(exc)
                if recovery_notes:
                    message += "\n" + " ".join(recovery_notes)
                self.restart_queue.put(
                    {
                        "status": "switch_error",
                        "remark": remark,
                        "message": message,
                        "client_stopped": True,
                    }
                )
                return
            except Exception:
                recovery_notes = []
                try:
                    self.store.rollback_switch(switch_result)
                except AccountStoreError:
                    recovery_notes.append("原认证文件未能恢复。")
                try:
                    client.start()
                except Exception:
                    recovery_notes.append("请手动启动 Codex。")
                message = "系统未能恢复 OpenAI 配置。"
                if recovery_notes:
                    message += "\n" + " ".join(recovery_notes)
                self.restart_queue.put(
                    {
                        "status": "switch_error",
                        "remark": remark,
                        "message": message,
                        "client_stopped": True,
                    }
                )
                return

        self.restart_queue.put(
            {"status": "progress", "message": "正在启动 Codex…", "button": "正在启动…"}
        )
        try:
            launched_pid = client.start()
        except CodexClientError as exc:
            self.restart_queue.put(
                {
                    "status": "restart_error",
                    "profile_id": profile_id,
                    "remark": remark,
                    "changed": switch_result.changed or provider_was_deepseek,
                    "provider_restored": provider_was_deepseek,
                    "message": str(exc),
                }
            )
            return
        except Exception:
            self.restart_queue.put(
                {
                    "status": "restart_error",
                    "profile_id": profile_id,
                    "remark": remark,
                    "changed": switch_result.changed or provider_was_deepseek,
                    "provider_restored": provider_was_deepseek,
                    "message": "系统未能完成 Codex 启动。",
                }
            )
            return

        self.restart_queue.put(
            {
                "status": "success",
                "profile_id": profile_id,
                "remark": remark,
                "changed": switch_result.changed or provider_was_deepseek,
                "provider_restored": provider_was_deepseek,
                "result": RestartResult(
                    was_running=stop_result.was_running,
                    forced_close=stop_result.forced_close,
                    launched_pid=launched_pid,
                ),
            }
        )

    def _schedule_restart_poll(self) -> None:
        if self.restart_poll_id is None:
            self.restart_poll_id = self.root.after(100, self._poll_restart_result)

    def _poll_restart_result(self) -> None:
        self.restart_poll_id = None
        try:
            outcome = self.restart_queue.get_nowait()
        except queue.Empty:
            if self.busy:
                self._schedule_restart_poll()
            return

        status = outcome.get("status")
        if status == "progress":
            self._set_restart_progress(outcome["button"], outcome["message"])
            self._schedule_restart_poll()
            return

        profile_id = outcome.get("profile_id")
        remark = outcome.get("remark", "所选账号")
        changed = outcome.get("changed", False)
        provider_restored = outcome.get("provider_restored", False)
        self.restart_in_progress = False
        self.restart_button_text = ""
        self.switch_button.configure(text=SWITCH_BUTTON_TEXT)
        self._set_busy(False)
        if profile_id:
            self.refresh_profiles(profile_id)

        if status == "switch_error":
            self.show_notice("切换失败", outcome["message"], "error")
            return

        if status == "restart_error":
            if changed:
                title = "账号已切换"
                message = (
                    "账号与模型服务已更新，但 Codex 未能自动重启。\n"
                    if provider_restored
                    else "认证文件已更新，但 Codex 未能自动重启。\n"
                ) + ("请手动启动 Codex。\n\n原因：{}").format(outcome["message"])
            else:
                title = "Codex 未能启动"
                message = ("“{}”已是当前账号。请手动启动 Codex。\n\n原因：{}").format(
                    remark, outcome["message"]
                )
            self.show_notice(
                title,
                message,
                "warning",
            )
            return

        restart_result = outcome.get("result")
        if not isinstance(restart_result, RestartResult):
            self.show_notice(
                "账号已切换",
                "认证文件已更新，但未能确认 Codex 状态。\n请手动检查客户端。",
                "warning",
            )
            return

        action = "已重新启动" if restart_result.was_running else "已启动"
        if changed:
            title = "已切换账号"
            if provider_restored:
                message = "已切回 OpenAI 并切换到“{}”，Codex {}。".format(
                    remark,
                    action,
                )
            else:
                message = "已切换到“{}”，Codex {}。".format(remark, action)
        else:
            title = "Codex {}".format(action)
            message = "“{}”已是当前账号。".format(remark)
        self.show_notice(title, message, "success")

    def update_selected_profile(self) -> None:
        if self.busy:
            return
        profile = self._selected_profile()
        if profile is None:
            self.show_notice("未选择账号", "请先选择一个账号。", "warning")
            return

        try:
            current_hash = validate_auth_file(self.target_auth_path)
        except InvalidAuthFileError as exc:
            self.show_notice("无法更新账号", str(exc), "error")
            return

        if self.store.profile_is_available(profile) and current_hash == profile.sha256:
            try:
                stored_hash = validate_auth_file(self.store.profile_path(profile))
            except InvalidAuthFileError:
                stored_hash = None
            if stored_hash == current_hash:
                self.show_notice(
                    "认证已是最新",
                    "“{}”与当前 Codex 认证一致。".format(profile.remark),
                    "info",
                )
                return

        confirmed = self.show_confirm(
            "更新认证文件",
            "将使用当前 Codex 的 auth.json 更新“{}”。更新前会备份原认证副本。".format(
                profile.remark
            ),
            "确认更新",
        )
        if not confirmed:
            return

        self._set_busy(True)
        try:
            result = self.store.update_profile(profile.profile_id, self.target_auth_path)
        except AccountStoreError as exc:
            error_message = str(exc)
            result = None
        else:
            error_message = None
        finally:
            self._set_busy(False)

        if error_message is not None:
            self.show_notice("更新失败", error_message, "error")
            return
        if result is None:
            return

        self.refresh_profiles(profile.profile_id)
        if result.changed:
            backup_note = ""
            if result.backup_path is not None:
                backup_note = "\n\n原认证已备份至：\n{}".format(result.backup_path)
            self.show_notice(
                "账号已更新",
                "已更新“{}”的认证文件。{}".format(result.profile.remark, backup_note),
                "success",
            )
        else:
            self.show_notice(
                "认证已是最新",
                "“{}”与当前 Codex 认证一致。".format(result.profile.remark),
                "info",
            )

    def rename_selected_profile(self) -> None:
        if self.busy:
            return
        profile = self._selected_profile()
        if profile is None:
            self.show_notice("未选择账号", "请先选择一个账号。", "warning")
            return

        dialog = RemarkDialog(
            self.root,
            self.font_family,
            "修改备注",
            "备注仅用于本机显示，不会修改认证内容。",
            "保存",
            profile.remark,
        )
        if dialog.result is None or dialog.result == profile.remark:
            return

        self._set_busy(True)
        try:
            renamed = self.store.rename_profile(profile.profile_id, dialog.result)
        except AccountStoreError as exc:
            error_message = str(exc)
            renamed = None
        else:
            error_message = None
        finally:
            self._set_busy(False)

        if error_message is not None:
            self.show_notice("修改失败", error_message, "error")
            return
        if renamed is None:
            return
        self.refresh_profiles(renamed.profile_id)
        self.show_notice("备注已更新", "账号备注已保存。", "success")

    def remove_selected_profile(self) -> None:
        if self.busy:
            return
        profile = self._selected_profile()
        if profile is None:
            self.show_notice("未选择账号", "请先选择一个账号。", "warning")
            return

        remove_message = "将删除“{}”的本地认证副本。此操作不会修改当前 auth.json。".format(
            profile.remark
        )
        if profile.profile_id == self.active_profile_id:
            remove_message += " 当前 Codex 的登录状态不会改变。"
        confirmed = self.show_confirm(
            "移除账号",
            remove_message,
            "确认移除",
            destructive=True,
        )
        if not confirmed:
            return

        self._set_busy(True)
        try:
            removed = self.store.remove_profile(profile.profile_id)
        except AccountStoreError as exc:
            error_message = str(exc)
            removed = None
        else:
            error_message = None
        finally:
            self._set_busy(False)

        if error_message is not None:
            self.show_notice("移除失败", error_message, "error")
            self.refresh_profiles()
            return
        if removed is None:
            return
        self.refresh_profiles()
        self.show_notice(
            "账号已移除",
            "已删除“{}”的本地认证副本。".format(removed.remark),
            "success",
        )


def main() -> None:
    enable_windows_dpi_awareness()
    root = tk.Tk()
    AccountSwitcherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
