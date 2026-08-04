"""Launch a packaged executable and verify its real Windows UI process lifecycle."""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

WM_CLOSE = 0x0010
SW_HIDE = 0
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_parent_map():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return {}
    parents = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def process_tree_ids(root_process_id: int):
    parents = process_parent_map()
    tree = {root_process_id}
    changed = True
    while changed:
        changed = False
        for process_id, parent_process_id in parents.items():
            if parent_process_id in tree and process_id not in tree:
                tree.add(process_id)
                changed = True
    return tree


def windows_for_processes(process_ids):
    user32 = ctypes.windll.user32
    windows = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(window_handle, _parameter):
        owner_process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(
            window_handle,
            ctypes.byref(owner_process_id),
        )
        if owner_process_id.value in process_ids:
            text_length = user32.GetWindowTextLengthW(window_handle)
            title_buffer = ctypes.create_unicode_buffer(text_length + 1)
            user32.GetWindowTextW(window_handle, title_buffer, len(title_buffer))
            windows.append((window_handle, owner_process_id.value, title_buffer.value))
        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return windows


def terminate_processes(process_ids) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    for process_id in sorted(process_ids, reverse=True):
        process_handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, process_id)
        if process_handle:
            try:
                kernel32.TerminateProcess(process_handle, 1)
            finally:
                kernel32.CloseHandle(process_handle)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    arguments = parser.parse_args()
    source_executable = arguments.executable.resolve()
    if not source_executable.is_file():
        raise SystemExit("Executable not found: {}".format(source_executable))

    with tempfile.TemporaryDirectory(prefix="codex-switcher-smoke-") as temporary:
        temporary_root = Path(temporary)
        smoke_executable = temporary_root / source_executable.name
        shutil.copy2(str(source_executable), str(smoke_executable))

        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = SW_HIDE
        process = subprocess.Popen(
            [str(smoke_executable)],
            cwd=str(temporary_root),
            startupinfo=startup_info,
        )

        windows = []
        tree_ids = {process.pid}
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise SystemExit(
                    "Packaged application exited early with code {}.".format(return_code)
                )
            tree_ids = process_tree_ids(process.pid)
            windows = windows_for_processes(tree_ids)
            if (
                any(title == "Codex 切换器" for _handle, _pid, title in windows)
                and (temporary_root / "账号文件").is_dir()
            ):
                break
            time.sleep(0.1)

        application_windows = [item for item in windows if item[2] == "Codex 切换器"]
        if not application_windows or not (temporary_root / "账号文件").is_dir():
            terminate_processes(tree_ids)
            process.wait(timeout=5)
            raise SystemExit("Packaged application did not create a window.")

        storage_created = (temporary_root / "账号文件").is_dir()
        for window_handle, _process_id, _title in application_windows:
            ctypes.windll.user32.PostMessageW(window_handle, WM_CLOSE, 0, 0)

        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            terminate_processes(process_tree_ids(process.pid))
            process.wait(timeout=5)
            raise SystemExit("Packaged application did not close after WM_CLOSE.")

        if return_code != 0:
            raise SystemExit("Packaged application closed with code {}.".format(return_code))

        print(
            json.dumps(
                {
                    "launched": True,
                    "window_count": len(application_windows),
                    "process_count": len(tree_ids),
                    "storage_created": storage_created,
                    "exit_code": return_code,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
