"""Windows Credential Manager and DPAPI boundaries used by the application."""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from ctypes import wintypes
from typing import Optional

DEFAULT_CREDENTIAL_TARGET = "CodexSwitcher.DeepSeekApiKey"
LEGACY_MIGRATION_MARKER_SUFFIX = ".LegacyMigrated.v15"
LEGACY_MIGRATION_MARKER_VALUE = "sk-" + "migrated-v15"
MAX_API_KEY_LENGTH = 1024


class CredentialStoreError(RuntimeError):
    """Raised when Windows cannot read or persist the protected credential."""


class InvalidApiKeyError(ValueError):
    """Raised when a DeepSeek API key is not safe to place in Codex TOML."""


def credential_target_for_codex_home(codex_home) -> str:
    """Return a stable Credential Manager target scoped to one Codex home."""

    normalized = os.path.normcase(os.path.abspath(os.fspath(codex_home)))
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return "{}.{}".format(DEFAULT_CREDENTIAL_TARGET, suffix)


def legacy_migration_marker_target_for_codex_home(codex_home) -> str:
    """Return the non-secret migration marker target for one Codex home."""

    return credential_target_for_codex_home(codex_home) + LEGACY_MIGRATION_MARKER_SUFFIX


def migrate_legacy_credential(
    scoped_store,
    legacy_store,
    marker_store,
    *,
    allow_claim: bool,
    canonical_owner: bool = False,
    trusted_active_key: Optional[str] = None,
) -> Optional[str]:
    """Copy the v1.4 global credential once without racing a legacy writer.

    Windows Credential Manager has no compare-and-delete primitive. The legacy
    value is therefore retained for downgrade compatibility, while a separate
    marker prevents a later scoped deletion from resurrecting that stale value.
    """

    scoped = scoped_store.read()
    if trusted_active_key is not None:
        normalize_api_key(trusted_active_key)
    if not allow_claim or not canonical_owner:
        return scoped
    if marker_store is None:
        raise CredentialStoreError("缺少旧版 DeepSeek API Key 迁移标记存储。")
    marker = marker_store.read()
    if marker == LEGACY_MIGRATION_MARKER_VALUE:
        return scoped
    if marker is not None:
        raise CredentialStoreError("旧版 DeepSeek API Key 迁移标记无效。")

    legacy = legacy_store.read()
    if scoped is None and legacy is None:
        return None

    if scoped is None:
        scoped_store.write(legacy)
        if scoped_store.read() != legacy:
            raise CredentialStoreError("无法验证迁移后的 DeepSeek API Key。")
        scoped = legacy
    marker_store.write(LEGACY_MIGRATION_MARKER_VALUE)
    if marker_store.read() != LEGACY_MIGRATION_MARKER_VALUE:
        raise CredentialStoreError("无法验证旧版 DeepSeek API Key 迁移标记。")
    return scoped


def reconcile_active_credential(scoped_store, active_key: str) -> str:
    """Make the saved credential match the verified managed DeepSeek config."""

    trusted = normalize_api_key(active_key)
    if scoped_store.read() != trusted:
        scoped_store.write(trusted)
        if scoped_store.read() != trusted:
            raise CredentialStoreError("无法验证活动 DeepSeek API Key 的凭据同步结果。")
    return trusted


def normalize_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidApiKeyError("请输入有效的 DeepSeek API Key。")
    normalized = value.strip()
    if not normalized.startswith("sk-"):
        raise InvalidApiKeyError("API Key 应以 sk- 开头。")
    if len(normalized) < 8:
        raise InvalidApiKeyError("API Key 长度无效。")
    if len(normalized) > MAX_API_KEY_LENGTH:
        raise InvalidApiKeyError("API Key 过长。")
    if any(character.isspace() for character in normalized):
        raise InvalidApiKeyError("API Key 不能包含空格或换行。")
    if '"' in normalized or "'" in normalized:
        raise InvalidApiKeyError("API Key 包含不支持的字符。")
    return normalized


def masked_api_key(value: Optional[str]) -> str:
    if not value:
        return "未保存"
    suffix = value[-4:] if len(value) >= 4 else value
    return "已保存 ····{}".format(suffix)


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsCredentialStore:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self, target_name: str = DEFAULT_CREDENTIAL_TARGET, advapi32=None) -> None:
        if sys.platform != "win32" and advapi32 is None:
            raise CredentialStoreError("此功能仅支持 Windows。")
        self.target_name = target_name
        self.advapi32 = (
            advapi32 if advapi32 is not None else ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        )
        self._configure_functions()

    def _configure_functions(self) -> None:
        self.advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
        self.advapi32.CredWriteW.restype = wintypes.BOOL
        self.advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        self.advapi32.CredReadW.restype = wintypes.BOOL
        self.advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.advapi32.CredDeleteW.restype = wintypes.BOOL
        self.advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self.advapi32.CredFree.restype = None

    @staticmethod
    def _last_error() -> int:
        return ctypes.get_last_error()

    def read(self) -> Optional[str]:
        pointer = ctypes.POINTER(_CredentialW)()
        if not self.advapi32.CredReadW(
            self.target_name,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error_code = self._last_error()
            if error_code == self.ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError("无法读取 Windows 凭据（错误代码 {}）。".format(error_code))
        try:
            credential = pointer.contents
            if not credential.CredentialBlob or credential.CredentialBlobSize == 0:
                return None
            raw = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            try:
                return normalize_api_key(raw.decode("utf-16-le"))
            except (UnicodeError, InvalidApiKeyError) as exc:
                raise CredentialStoreError("Windows 凭据中的 API Key 无效。") from exc
        finally:
            self.advapi32.CredFree(pointer)

    def write(self, value: str) -> None:
        normalized = normalize_api_key(value)
        blob = normalized.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob)
        credential = _CredentialW()
        credential.Flags = 0
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self.target_name
        credential.Comment = "Codex 切换器保存的 DeepSeek API Key"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            buffer,
            ctypes.POINTER(ctypes.c_ubyte),
        )
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = "DeepSeek API"
        if not self.advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreError(
                "无法保存 Windows 凭据（错误代码 {}）。".format(self._last_error())
            )

    def delete(self) -> None:
        if self.advapi32.CredDeleteW(
            self.target_name,
            self.CRED_TYPE_GENERIC,
            0,
        ):
            return
        error_code = self._last_error()
        if error_code != self.ERROR_NOT_FOUND:
            raise CredentialStoreError("无法删除 Windows 凭据（错误代码 {}）。".format(error_code))


class WindowsDataProtector:
    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self, crypt32=None, kernel32=None) -> None:
        if sys.platform != "win32" and (crypt32 is None or kernel32 is None):
            raise CredentialStoreError("此功能仅支持 Windows。")
        self.crypt32 = (
            crypt32 if crypt32 is not None else ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        )
        self.kernel32 = (
            kernel32 if kernel32 is not None else ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        )
        self._configure_functions()

    def _configure_functions(self) -> None:
        self.crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self.crypt32.CryptProtectData.restype = wintypes.BOOL
        self.crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _input_blob(value: bytes):
        buffer = ctypes.create_string_buffer(value)
        blob = _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return buffer, blob

    def protect(self, value: bytes) -> bytes:
        if not isinstance(value, bytes) or not value:
            raise CredentialStoreError("待保护的数据为空。")
        buffer, input_blob = self._input_blob(value)
        output_blob = _DataBlob()
        if not self.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Codex 切换器配置状态",
            None,
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise CredentialStoreError(
                "无法保护配置状态（错误代码 {}）。".format(ctypes.get_last_error())
            )
        try:
            _ = buffer
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self.kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, value: bytes) -> bytes:
        if not isinstance(value, bytes) or not value:
            raise CredentialStoreError("受保护的配置状态为空。")
        buffer, input_blob = self._input_blob(value)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        if not self.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise CredentialStoreError(
                "无法读取受保护的配置状态（错误代码 {}）。".format(ctypes.get_last_error())
            )
        try:
            _ = buffer
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if description:
                self.kernel32.LocalFree(description)
            self.kernel32.LocalFree(output_blob.pbData)
