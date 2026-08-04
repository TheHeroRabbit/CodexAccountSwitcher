"""Scoped Win32 integration for restarting the Microsoft Store Codex client."""

from __future__ import annotations

import ctypes
import sys
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

CODEX_AUMID = "OpenAI.Codex_2p2nqsd0c76g0!App"
CODEX_PACKAGE_PATH_MARKER = "\\windowsapps\\openai.codex_"

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010
CLSCTX_LOCAL_SERVER = 0x00000004
COINIT_APARTMENTTHREADED = 0x00000002
RPC_E_CHANGED_MODE = -2147417850


class CodexClientError(RuntimeError):
    """Raised when the Microsoft Store Codex client cannot be restarted."""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    image_path: str


@dataclass(frozen=True)
class StopResult:
    was_running: bool
    forced_close: bool


@dataclass(frozen=True)
class RestartResult:
    was_running: bool
    forced_close: bool
    launched_pid: Optional[int]


def is_codex_store_process(image_path: str) -> bool:
    normalized = image_path.replace("/", "\\").casefold()
    return CODEX_PACKAGE_PATH_MARKER in normalized


class _Guid(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_string(cls, value: str) -> "_Guid":
        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


class WindowsStoreBackend:
    """Small Win32 boundary used by StoreCodexClient and its unit tests."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CodexClientError("自动重启仅支持 Windows。")
        self.kernel32 = ctypes.windll.kernel32
        self.user32 = ctypes.windll.user32
        self.psapi = ctypes.windll.psapi
        self.ole32 = ctypes.windll.ole32
        self._configure_functions()

    def _configure_functions(self) -> None:
        self.psapi.EnumProcesses.argtypes = (
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.psapi.EnumProcesses.restype = wintypes.BOOL

        self.kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel32.TerminateProcess.restype = wintypes.BOOL

        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self.user32.PostMessageW.restype = wintypes.BOOL

        self.ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        self.ole32.CoInitializeEx.restype = ctypes.c_long
        self.ole32.CoUninitialize.argtypes = ()
        self.ole32.CoCreateInstance.argtypes = (
            ctypes.POINTER(_Guid),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.ole32.CoCreateInstance.restype = ctypes.c_long

    def list_processes(self) -> List[ProcessInfo]:
        capacity = 1024
        while True:
            process_ids = (wintypes.DWORD * capacity)()
            needed = wintypes.DWORD()
            if not self.psapi.EnumProcesses(
                process_ids,
                ctypes.sizeof(process_ids),
                ctypes.byref(needed),
            ):
                raise CodexClientError("无法读取 Codex 运行状态。")
            count = needed.value // ctypes.sizeof(wintypes.DWORD)
            if count < capacity:
                break
            capacity *= 2

        processes: List[ProcessInfo] = []
        for process_id in process_ids[:count]:
            if not process_id:
                continue
            image_path = self._query_image_path(int(process_id))
            if image_path:
                processes.append(ProcessInfo(int(process_id), image_path))
        return processes

    def _query_image_path(self, process_id: int) -> str:
        handle = self.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not handle:
            return ""
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not self.kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(capacity),
            ):
                return ""
            return buffer.value
        finally:
            self.kernel32.CloseHandle(handle)

    def request_close(self, process_ids: Sequence[int]) -> int:
        targets = set(process_ids)
        if not targets:
            return 0
        closed_windows = 0
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def close_window(window_handle, _parameter):
            nonlocal closed_windows
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
            if process_id.value in targets and self.user32.PostMessageW(
                window_handle,
                WM_CLOSE,
                0,
                0,
            ):
                closed_windows += 1
            return True

        callback = callback_type(close_window)
        if not self.user32.EnumWindows(callback, 0):
            raise CodexClientError("无法请求 Codex 退出。")
        return closed_windows

    def terminate_processes(self, process_ids: Iterable[int]) -> int:
        terminated = 0
        for process_id in set(process_ids):
            handle = self.kernel32.OpenProcess(PROCESS_TERMINATE, False, process_id)
            if not handle:
                continue
            try:
                if self.kernel32.TerminateProcess(handle, 1):
                    terminated += 1
            finally:
                self.kernel32.CloseHandle(handle)
        return terminated

    def activate_application(self, app_user_model_id: str) -> Optional[int]:
        initialized = self.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        should_uninitialize = initialized in (0, 1)
        if initialized < 0 and initialized != RPC_E_CHANGED_MODE:
            raise CodexClientError("无法初始化 Codex 启动服务。")

        activation_manager = ctypes.c_void_p()
        class_id = _Guid.from_string("45BA127D-10A8-46EA-8AB7-56EA9078943C")
        interface_id = _Guid.from_string("2E941141-7F97-4756-BA1D-9DECDE894A3D")
        try:
            result = self.ole32.CoCreateInstance(
                ctypes.byref(class_id),
                None,
                CLSCTX_LOCAL_SERVER,
                ctypes.byref(interface_id),
                ctypes.byref(activation_manager),
            )
            if result < 0 or not activation_manager.value:
                raise CodexClientError("未检测到 Microsoft Store 版 Codex。")

            vtable = ctypes.cast(
                activation_manager,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            activate_type = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            )
            release_type = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
            activate = activate_type(vtable[3])
            release = release_type(vtable[2])
            launched_process_id = wintypes.DWORD()
            try:
                result = activate(
                    activation_manager,
                    app_user_model_id,
                    None,
                    0,
                    ctypes.byref(launched_process_id),
                )
            finally:
                release(activation_manager)
            if result < 0:
                raise CodexClientError("未检测到 Microsoft Store 版 Codex。")
            return int(launched_process_id.value) or None
        finally:
            if should_uninitialize:
                self.ole32.CoUninitialize()

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


class StoreCodexClient:
    def __init__(
        self,
        backend=None,
        graceful_timeout: float = 6.0,
        force_timeout: float = 3.0,
        launch_timeout: float = 10.0,
        poll_interval: float = 0.2,
    ) -> None:
        self.backend = backend if backend is not None else WindowsStoreBackend()
        self.graceful_timeout = graceful_timeout
        self.force_timeout = force_timeout
        self.launch_timeout = launch_timeout
        self.poll_interval = poll_interval

    def running_processes(self) -> List[ProcessInfo]:
        return [
            process
            for process in self.backend.list_processes()
            if is_codex_store_process(process.image_path)
        ]

    def restart(self) -> RestartResult:
        stop_result = self.stop()
        launched_pid = self.start()
        return RestartResult(
            was_running=stop_result.was_running,
            forced_close=stop_result.forced_close,
            launched_pid=launched_pid,
        )

    def stop(self) -> StopResult:
        initial_processes = self.running_processes()
        was_running = bool(initial_processes)
        forced_close = False

        if was_running:
            self.backend.request_close([process.pid for process in initial_processes])
            if not self._wait_for_stopped(self.graceful_timeout):
                forced_close = True
                if not self._force_stop():
                    raise CodexClientError("无法关闭 Codex。请手动退出后重试。")

        return StopResult(
            was_running=was_running,
            forced_close=forced_close,
        )

    def start(self) -> Optional[int]:
        launched_pid = self.backend.activate_application(CODEX_AUMID)
        if not self._wait_for_started(self.launch_timeout):
            raise CodexClientError("未能启动 Codex。")
        return launched_pid

    def _wait_for_stopped(self, timeout: float) -> bool:
        deadline = self.backend.monotonic() + max(0.0, timeout)
        while True:
            if not self.running_processes():
                return True
            if self.backend.monotonic() >= deadline:
                return False
            self.backend.sleep(self.poll_interval)

    def _force_stop(self) -> bool:
        deadline = self.backend.monotonic() + max(0.0, self.force_timeout)
        while True:
            remaining = self.running_processes()
            if not remaining:
                return True
            self.backend.terminate_processes(process.pid for process in remaining)
            if self.backend.monotonic() >= deadline:
                return not self.running_processes()
            self.backend.sleep(self.poll_interval)

    def _wait_for_started(self, timeout: float) -> bool:
        deadline = self.backend.monotonic() + max(0.0, timeout)
        while True:
            if self.running_processes():
                return True
            if self.backend.monotonic() >= deadline:
                return False
            self.backend.sleep(self.poll_interval)
