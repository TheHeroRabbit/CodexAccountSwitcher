"""Validated, atomic storage for local Codex authentication profiles."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Sequence, Tuple

from credential_store import CredentialStoreError, WindowsDataProtector

STORAGE_FOLDER_NAME = "账号文件"
BACKUP_FOLDER_NAME = "切换备份"
UPDATE_BACKUP_FOLDER_NAME = "更新备份"
MANIFEST_FILE_NAME = "profiles.json"
MANIFEST_BACKUP_FILE_NAME = "profiles.json.bak"
STORAGE_LOCK_FILE_NAME = ".profiles.lock"
TARGET_LOCK_FILE_NAME = ".codex-account-switcher.lock"
ACCOUNT_TRANSACTION_FILE_NAME = ".codex-account-switcher-account-transaction.dat"
ACCOUNT_TRANSACTION_VERSION = 1
MAX_AUTH_FILE_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_FILE_BYTES = 2 * 1024 * 1024
MAX_ACCOUNT_TRANSACTION_BYTES = 32 * 1024 * 1024
MAX_REMARK_LENGTH = 60
MANAGED_CHATGPT_AUTH_MODE = "chatgpt"
DEFAULT_LOCK_TIMEOUT_SECONDS = 15.0

SYNC_STATUS_MISSING = "missing"
SYNC_STATUS_UNSUPPORTED = "unsupported"
SYNC_STATUS_UNMATCHED = "unmatched"
SYNC_STATUS_UNCHANGED = "unchanged"
SYNC_STATUS_UPDATED = "updated"

_PROFILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_REMOVE_PATTERN = re.compile(
    r"^\.auth_(?P<profile_id>[0-9a-f]{32})\.json\.(?P<tombstone_id>[0-9a-f]{32})\.remove$"
)
_ATOMIC_TEMPORARY_ID_PATTERN = r"[0-9a-f]{32}"
_ACCOUNT_TRANSACTION_TARGETS = ("profile", "manifest_backup", "manifest")
_ACCOUNT_TRANSACTION_OPERATIONS = {"import", "update", "remove", "rename", "repair"}
_LOCK_RETRY_SECONDS = 0.05
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: Dict[str, "_InterprocessFileLock"] = {}


class AccountStoreError(Exception):
    """Base error for account storage operations."""


class InvalidAuthFileError(AccountStoreError):
    """Raised when a selected auth file is not a usable JSON object."""


class ProfileNotFoundError(AccountStoreError):
    """Raised when a selected profile no longer exists."""


class AccountTransactionRecoveryError(AccountStoreError):
    """Raised when a durable account-storage transaction cannot be recovered safely."""


@dataclass(frozen=True)
class AccountProfile:
    profile_id: str
    remark: str
    file_name: str
    added_at: str
    updated_at: str
    sha256: str

    @classmethod
    def from_dict(cls, value: Any) -> "AccountProfile":
        if not isinstance(value, dict):
            raise AccountStoreError("账号清单包含无效记录。")

        profile_id = value.get("id")
        remark = value.get("remark")
        file_name = value.get("file")
        added_at = value.get("added_at")
        updated_at = value.get("updated_at", added_at)
        sha256 = value.get("sha256")

        if not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise AccountStoreError("账号清单中的账号 ID 无效。")
        if not isinstance(remark, str) or not remark.strip():
            raise AccountStoreError("账号清单中的备注为空。")
        if len(remark) > MAX_REMARK_LENGTH:
            raise AccountStoreError("账号清单中的备注过长。")
        if not isinstance(file_name, str) or Path(file_name).name != file_name:
            raise AccountStoreError("账号清单中的文件名无效。")
        if file_name != "auth_{}.json".format(profile_id):
            raise AccountStoreError("账号清单中的文件名与账号 ID 不匹配。")
        if not isinstance(added_at, str) or not added_at:
            raise AccountStoreError("账号清单中的添加时间无效。")
        if not isinstance(updated_at, str) or not updated_at:
            raise AccountStoreError("账号清单中的更新时间无效。")
        if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
            raise AccountStoreError("账号清单中的文件校验值无效。")

        return cls(
            profile_id=profile_id,
            remark=remark.strip(),
            file_name=file_name,
            added_at=added_at,
            updated_at=updated_at,
            sha256=sha256,
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.profile_id,
            "remark": self.remark,
            "file": self.file_name,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ImportResult:
    profile: AccountProfile
    created: bool


@dataclass(frozen=True)
class SwitchResult:
    changed: bool
    target_path: Path
    backup_path: Optional[Path]
    replacement_sha256: Optional[str] = None
    backup_sha256: Optional[str] = None


@dataclass(frozen=True)
class UpdateResult:
    profile: AccountProfile
    changed: bool
    backup_path: Optional[Path]


@dataclass(frozen=True)
class SyncResult:
    status: str
    profile: Optional[AccountProfile]
    changed: bool
    backup_path: Optional[Path]


@dataclass(frozen=True)
class _AuthFileInfo:
    sha256: str
    auth_mode: Optional[str]
    managed_identity: Optional[str]


def _try_lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _InterprocessFileLock:
    """A process-shared, thread-reentrant advisory lock backed by one file byte."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._handle: Optional[BinaryIO] = None

    def _open_and_lock(self, deadline: float, purpose: str) -> BinaryIO:
        handle: Optional[BinaryIO] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())

            while True:
                try:
                    _try_lock_file(handle)
                    return handle
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AccountStoreError(
                            "等待{}超时。请确认没有其他切换器实例正在操作后重试。".format(purpose)
                        )
                    time.sleep(min(_LOCK_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
        except AccountStoreError:
            if handle is not None:
                handle.close()
            raise
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise AccountStoreError("无法建立{}：{}".format(purpose, exc)) from exc

    @contextmanager
    def hold(self, timeout: float, purpose: str) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        acquired = self._thread_lock.acquire(timeout=timeout)
        if not acquired:
            raise AccountStoreError(
                "等待{}超时。请确认没有其他切换器实例正在操作后重试。".format(purpose)
            )

        try:
            if self._depth == 0:
                self._handle = self._open_and_lock(deadline, purpose)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    handle = self._handle
                    self._handle = None
                    if handle is not None:
                        try:
                            _unlock_file(handle)
                        except OSError as exc:
                            raise AccountStoreError("无法释放{}：{}".format(purpose, exc)) from exc
                        finally:
                            handle.close()
        finally:
            self._thread_lock.release()


def _shared_file_lock(path: Path) -> _InterprocessFileLock:
    resolved = Path(path).resolve()
    key = os.path.normcase(str(resolved))
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(key)
        if lock is None:
            lock = _InterprocessFileLock(resolved)
            _LOCK_REGISTRY[key] = lock
        return lock


def _cleanup_atomic_temporaries(targets: Iterator[Path], label: str) -> None:
    """Remove only stale temp files created for the exact supplied targets."""

    patterns_by_parent: Dict[Path, List[re.Pattern[str]]] = {}
    for raw_target in targets:
        target = Path(raw_target).resolve()
        pattern = re.compile(
            r"^\.codex-account-switcher\.{}\.{}\.tmp$".format(
                re.escape(target.name),
                _ATOMIC_TEMPORARY_ID_PATTERN,
            )
        )
        patterns_by_parent.setdefault(target.parent, []).append(pattern)

    for parent, patterns in patterns_by_parent.items():
        if not parent.exists():
            continue
        try:
            entries = tuple(parent.iterdir())
        except OSError as exc:
            raise AccountStoreError("无法检查{}临时文件：{}".format(label, exc)) from exc
        for entry in entries:
            if not any(pattern.fullmatch(entry.name) for pattern in patterns):
                continue
            try:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
            except OSError as exc:
                raise AccountStoreError("无法清理{}临时文件：{}".format(label, exc)) from exc


@contextmanager
def codex_operation_guard(
    target_auth_path: Path,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize a Codex mutation across threads and app processes.

    This module-level guard deliberately shares the same in-process registry as
    ``ProfileStore.codex_operation_guard``. Provider recovery can therefore use
    the Codex lock on its public entry points while remaining reentrant when the
    application already holds that lock for a larger credential/configuration
    transaction.
    """

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    target = Path(target_auth_path).resolve()
    target_lock = _shared_file_lock(target.parent / TARGET_LOCK_FILE_NAME)
    with target_lock.hold(float(timeout), "Codex 操作锁"):
        _cleanup_atomic_temporaries(iter((target,)), "Codex 认证")
        yield


@contextmanager
def codex_operation_guard_for_home(
    codex_home: Path,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize a Codex-home operation without requiring an auth file path."""

    with codex_operation_guard(Path(codex_home).resolve() / "auth.json", timeout):
        yield


def _reject_non_standard_number(value: str) -> None:
    raise ValueError("JSON 中包含不支持的数值：{}".format(value))


def _normalize_remark(remark: str) -> str:
    normalized = " ".join(remark.split())
    if not normalized:
        raise AccountStoreError("请输入账号备注。")
    if len(normalized) > MAX_REMARK_LENGTH:
        raise AccountStoreError("账号备注不能超过 {} 个字符。".format(MAX_REMARK_LENGTH))
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_chatgpt_identity(payload: Dict[str, Any]) -> Optional[str]:
    if payload.get("auth_mode") != MANAGED_CHATGPT_AUTH_MODE:
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id or len(account_id) > 4096:
        return None
    try:
        account_id_bytes = account_id.encode("utf-8")
    except UnicodeEncodeError:
        return None
    identity = hashlib.sha256()
    identity.update(b"codex-account-switcher\0chatgpt\0")
    identity.update(account_id_bytes)
    return identity.hexdigest()


def _inspect_auth_file(path: Path) -> _AuthFileInfo:
    source = Path(path)
    if not source.is_file():
        raise InvalidAuthFileError("所选认证文件不存在或不是普通文件。")

    try:
        file_size = source.stat().st_size
    except OSError as exc:
        raise InvalidAuthFileError("无法读取认证文件信息：{}".format(exc)) from exc

    if file_size == 0:
        raise InvalidAuthFileError("所选认证文件为空。")
    if file_size > MAX_AUTH_FILE_BYTES:
        raise InvalidAuthFileError("认证文件超过 10 MB，已拒绝导入。")

    try:
        with source.open("rb") as auth_file:
            value = auth_file.read(MAX_AUTH_FILE_BYTES + 1)
        if len(value) > MAX_AUTH_FILE_BYTES:
            raise InvalidAuthFileError("认证文件超过 10 MB，已拒绝导入。")
        text = value.decode("utf-8-sig")
    except InvalidAuthFileError:
        raise
    except UnicodeError as exc:
        raise InvalidAuthFileError("认证文件不是有效的 UTF-8 JSON。") from exc
    except OSError as exc:
        raise InvalidAuthFileError("无法读取认证文件：{}".format(exc)) from exc

    try:
        payload = json.loads(text, parse_constant=_reject_non_standard_number)
    except (json.JSONDecodeError, ValueError) as exc:
        raise InvalidAuthFileError("认证文件不是有效的 JSON：{}".format(exc)) from exc

    if not isinstance(payload, dict):
        raise InvalidAuthFileError("认证文件的 JSON 顶层必须是对象。")

    return _AuthFileInfo(
        sha256=hashlib.sha256(value).hexdigest(),
        auth_mode=payload.get("auth_mode") if isinstance(payload.get("auth_mode"), str) else None,
        managed_identity=_managed_chatgpt_identity(payload),
    )


def validate_auth_file(path: Path) -> str:
    return _inspect_auth_file(path).sha256


def _atomic_copy(
    source: Path,
    destination: Path,
    *,
    expected_sha256: Optional[str] = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / ".codex-account-switcher.{}.{}.tmp".format(
        destination.name, uuid.uuid4().hex
    )
    try:
        shutil.copy2(str(source), str(temporary))
        with temporary.open("r+b") as copied:
            copied.flush()
            os.fsync(copied.fileno())
        if expected_sha256 is not None and _sha256_file(temporary) != expected_sha256:
            raise AccountStoreError("源文件在复制过程中发生变化，请重试。")
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _atomic_write_json(destination: Path, value: Dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / ".codex-account-switcher.{}.{}.tmp".format(
        destination.name, uuid.uuid4().hex
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _read_optional_bytes(path: Path, limit: int, label: str) -> Optional[bytes]:
    """Read one optional regular file with a hard bound before and during the read."""

    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AccountStoreError("无法读取{}信息：{}".format(label, exc)) from exc
    if not path.is_file():
        raise AccountStoreError("{}不是普通文件。".format(label))
    if file_size > limit:
        raise AccountStoreError("{}过大，已拒绝读取。".format(label))
    try:
        with path.open("rb") as source:
            value = source.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AccountStoreError("无法读取{}：{}".format(label, exc)) from exc
    if len(value) > limit:
        raise AccountStoreError("{}过大，已拒绝读取。".format(label))
    return value


def _prepare_atomic_write(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ".codex-account-switcher.{}.{}.tmp".format(
        path.name, uuid.uuid4().hex
    )
    try:
        with temporary.open("wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return temporary


def _optional_bytes_match(path: Path, expected: Optional[bytes], limit: int, label: str) -> bool:
    return _read_optional_bytes(path, limit, label) == expected


def _guarded_replace_optional(
    path: Path,
    expected: Optional[bytes],
    replacement: Optional[bytes],
    limit: int,
    label: str,
) -> None:
    """Replace or delete only while the target still has the expected bytes."""

    if replacement is not None and len(replacement) > limit:
        raise AccountStoreError("{}过大，已拒绝写入。".format(label))
    if replacement is None:
        if not _optional_bytes_match(path, expected, limit, label):
            raise AccountStoreError("{}已在删除准备期间被外部修改。".format(label))
        if expected is None:
            return
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise AccountStoreError("{}已在删除提交期间被外部修改。".format(label)) from exc
        return

    temporary = _prepare_atomic_write(path, replacement)
    try:
        if not _optional_bytes_match(path, expected, limit, label):
            raise AccountStoreError("{}已在写入准备期间被外部修改。".format(label))
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


class ProfileStore:
    def __init__(
        self,
        app_directory: Path,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        protector=None,
    ) -> None:
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise ValueError("lock_timeout must be a positive finite number")
        self.app_directory = Path(app_directory).resolve()
        self.storage_directory = self.app_directory / STORAGE_FOLDER_NAME
        self.backup_directory = self.storage_directory / BACKUP_FOLDER_NAME
        self.update_backup_directory = self.storage_directory / UPDATE_BACKUP_FOLDER_NAME
        self.manifest_path = self.storage_directory / MANIFEST_FILE_NAME
        self.manifest_backup_path = self.storage_directory / MANIFEST_BACKUP_FILE_NAME
        self.account_transaction_path = self.storage_directory / ACCOUNT_TRANSACTION_FILE_NAME
        self.storage_lock_path = self.storage_directory / STORAGE_LOCK_FILE_NAME
        self.lock_timeout = float(lock_timeout)
        self.protector = protector if protector is not None else WindowsDataProtector()
        self.recovered_from_backup = False

    @contextmanager
    def _storage_guard(self) -> Iterator[None]:
        lock = _shared_file_lock(self.storage_lock_path)
        with lock.hold(self.lock_timeout, "账号存储锁"):
            self._cleanup_storage_temporaries_locked()
            self._recover_pending_account_transaction_locked()
            self._recover_legacy_remove_tombstones_locked()
            yield

    def _cleanup_storage_temporaries_locked(self) -> None:
        targets = [
            self.manifest_path,
            self.manifest_backup_path,
            self.account_transaction_path,
        ]
        if self.storage_directory.exists():
            targets.extend(
                path
                for path in self.storage_directory.glob("auth_*.json")
                if re.fullmatch(r"auth_[0-9a-f]{32}\.json", path.name)
            )
        for directory in (self.backup_directory, self.update_backup_directory):
            if directory.exists():
                targets.extend(
                    path
                    for path in directory.glob("auth_*.json")
                    if re.fullmatch(r"auth_[0-9a-f_]+\.json", path.name)
                )
                # A crash before os.replace leaves only the hidden temp name,
                # so derive its intended target from the strict app-owned form.
                for path in directory.iterdir():
                    match = re.fullmatch(
                        r"\.codex-account-switcher\.(auth_[0-9a-f_]+\.json)\.{}\.tmp".format(
                            _ATOMIC_TEMPORARY_ID_PATTERN
                        ),
                        path.name,
                    )
                    if match is not None:
                        targets.append(directory / match.group(1))
        if self.storage_directory.exists():
            for path in self.storage_directory.iterdir():
                match = re.fullmatch(
                    r"\.codex-account-switcher\.(auth_[0-9a-f]{{32}}\.json)\.{}\.tmp".format(
                        _ATOMIC_TEMPORARY_ID_PATTERN
                    ),
                    path.name,
                )
                if match is not None:
                    targets.append(self.storage_directory / match.group(1))
        _cleanup_atomic_temporaries(iter(targets), "账号存储")

    def _profile_path_from_id(self, profile_id: str) -> Path:
        if not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise AccountTransactionRecoveryError("账号存储事务日志包含无效账号 ID。")
        return self.storage_directory / "auth_{}.json".format(profile_id)

    def _path_limit(self, path: Path, profile_id: Optional[str] = None) -> int:
        if path == self.account_transaction_path:
            return MAX_ACCOUNT_TRANSACTION_BYTES
        if path in (self.manifest_path, self.manifest_backup_path):
            return MAX_MANIFEST_FILE_BYTES
        if profile_id is not None and path == self._profile_path_from_id(profile_id):
            return MAX_AUTH_FILE_BYTES
        raise AccountTransactionRecoveryError("账号存储事务包含不受支持的目标。")

    def _transaction_target(self, path: Path, profile_id: Optional[str]) -> str:
        if path == self.manifest_path:
            return "manifest"
        if path == self.manifest_backup_path:
            return "manifest_backup"
        if profile_id is not None and path == self._profile_path_from_id(profile_id):
            return "profile"
        raise AccountStoreError("账号存储事务包含不受支持的写入目标。")

    def _transaction_path(self, target: str, profile_id: Optional[str]) -> Path:
        if target == "manifest":
            return self.manifest_path
        if target == "manifest_backup":
            return self.manifest_backup_path
        if target == "profile" and profile_id is not None:
            return self._profile_path_from_id(profile_id)
        raise AccountTransactionRecoveryError("账号存储事务日志包含无效目标。")

    @staticmethod
    def _encode_optional(value: Optional[bytes]) -> Optional[str]:
        if value is None:
            return None
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _decode_optional(value: Any, label: str, limit: int) -> Optional[bytes]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise AccountTransactionRecoveryError("{}事务前内容格式无效。".format(label))
        try:
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise AccountTransactionRecoveryError("{}事务前内容损坏。".format(label)) from exc
        if len(decoded) > limit:
            raise AccountTransactionRecoveryError("{}事务前内容过大。".format(label))
        return decoded

    @staticmethod
    def _signature(value: Optional[bytes]) -> Dict[str, Any]:
        if value is None:
            return {"exists": False, "size": 0, "sha256": None}
        return {
            "exists": True,
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }

    @staticmethod
    def _matches_signature(value: Optional[bytes], signature: Dict[str, Any]) -> bool:
        if value is None:
            return not signature["exists"]
        return (
            signature["exists"]
            and len(value) == signature["size"]
            and hashlib.sha256(value).hexdigest() == signature["sha256"]
        )

    def _validate_signature(self, value: Any, path: Path, profile_id: Optional[str]) -> None:
        if not isinstance(value, dict) or set(value) != {"exists", "size", "sha256"}:
            raise AccountTransactionRecoveryError("账号存储事务日志目标摘要无效。")
        exists = value.get("exists")
        size = value.get("size")
        digest = value.get("sha256")
        if (
            not isinstance(exists, bool)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > self._path_limit(path, profile_id)
            or (exists and (not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest)))
            or (not exists and (size != 0 or digest is not None))
        ):
            raise AccountTransactionRecoveryError("账号存储事务日志目标摘要无效。")

    def _encode_account_transaction(
        self,
        operation: str,
        profile_id: Optional[str],
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
    ) -> bytes:
        if operation not in _ACCOUNT_TRANSACTION_OPERATIONS:
            raise AccountStoreError("账号存储事务操作类型无效。")
        if operation in {"import", "update", "remove", "rename"} and (
            not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id)
        ):
            raise AccountStoreError("账号存储事务缺少有效账号 ID。")

        entries = []
        guards = []
        for path, before, after in changes:
            target = self._transaction_target(path, profile_id)
            if before == after:
                guards.append({"target": target, "before": self._signature(before)})
            else:
                entries.append(
                    {
                        "target": target,
                        "before": self._encode_optional(before),
                        "after": self._signature(after),
                    }
                )
        if not entries:
            raise AccountStoreError("账号存储事务没有需要写入的内容。")

        payload = {
            "version": ACCOUNT_TRANSACTION_VERSION,
            "transaction_id": uuid.uuid4().hex,
            "operation": operation,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "storage_path": str(self.storage_directory),
            "profile_id": profile_id,
            "changes": entries,
            "guards": guards,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) > MAX_ACCOUNT_TRANSACTION_BYTES:
            raise AccountStoreError("账号存储事务日志过大，已拒绝写入。")
        try:
            protected = self.protector.protect(serialized)
        except CredentialStoreError as exc:
            raise AccountStoreError("无法加密账号存储事务日志：{}".format(exc)) from exc
        if not isinstance(protected, bytes) or len(protected) > MAX_ACCOUNT_TRANSACTION_BYTES:
            raise AccountStoreError("账号存储事务日志过大或加密结果无效。")
        return protected

    def _decode_account_transaction(self, protected: bytes) -> Dict[str, Any]:
        try:
            serialized = self.protector.unprotect(protected)
            if not isinstance(serialized, bytes) or len(serialized) > MAX_ACCOUNT_TRANSACTION_BYTES:
                raise ValueError("unprotected transaction is too large")
            transaction = json.loads(serialized.decode("utf-8"))
        except (CredentialStoreError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AccountTransactionRecoveryError(
                "账号存储事务日志损坏或无法解密，已停止自动恢复。"
            ) from exc
        if not isinstance(transaction, dict):
            raise AccountTransactionRecoveryError("账号存储事务日志格式不受支持。")
        if set(transaction) != {
            "version",
            "transaction_id",
            "operation",
            "created_at",
            "storage_path",
            "profile_id",
            "changes",
            "guards",
        }:
            raise AccountTransactionRecoveryError("账号存储事务日志字段无效。")
        if transaction.get("version") != ACCOUNT_TRANSACTION_VERSION:
            raise AccountTransactionRecoveryError("账号存储事务日志版本不受支持。")
        operation = transaction.get("operation")
        if operation not in _ACCOUNT_TRANSACTION_OPERATIONS:
            raise AccountTransactionRecoveryError("账号存储事务日志操作类型无效。")
        transaction_id = transaction.get("transaction_id")
        if not isinstance(transaction_id, str) or not _PROFILE_ID_PATTERN.fullmatch(transaction_id):
            raise AccountTransactionRecoveryError("账号存储事务日志标识无效。")
        created_at = transaction.get("created_at")
        try:
            created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
        except ValueError as exc:
            raise AccountTransactionRecoveryError("账号存储事务日志时间无效。") from exc
        if created is None or created.tzinfo is None or len(created_at) > 128:
            raise AccountTransactionRecoveryError("账号存储事务日志时间无效。")

        storage_path = transaction.get("storage_path")
        storage_candidate = Path(storage_path) if isinstance(storage_path, str) else None
        if (
            storage_candidate is None
            or not storage_candidate.is_absolute()
            or storage_candidate.resolve() != self.storage_directory.resolve()
        ):
            raise AccountTransactionRecoveryError(
                "账号存储事务日志不属于当前账号文件夹，已停止自动恢复。"
            )

        profile_id = transaction.get("profile_id")
        if profile_id is not None and (
            not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id)
        ):
            raise AccountTransactionRecoveryError("账号存储事务日志包含无效账号 ID。")
        if operation in {"import", "update", "remove", "rename"} and profile_id is None:
            raise AccountTransactionRecoveryError("账号存储事务日志缺少账号 ID。")

        changes = transaction.get("changes")
        guards = transaction.get("guards")
        if not isinstance(changes, list) or not changes or len(changes) > 3:
            raise AccountTransactionRecoveryError("账号存储事务日志变更列表无效。")
        if not isinstance(guards, list) or len(guards) > 3:
            raise AccountTransactionRecoveryError("账号存储事务日志守卫列表无效。")

        seen = set()
        decoded_changes = []
        for entry in changes:
            if not isinstance(entry, dict) or set(entry) != {"target", "before", "after"}:
                raise AccountTransactionRecoveryError("账号存储事务日志变更项无效。")
            target = entry.get("target")
            if target not in _ACCOUNT_TRANSACTION_TARGETS or target in seen:
                raise AccountTransactionRecoveryError("账号存储事务日志包含重复或未知目标。")
            seen.add(target)
            path = self._transaction_path(target, profile_id)
            before = self._decode_optional(
                entry.get("before"), path.name, self._path_limit(path, profile_id)
            )
            signature = entry.get("after")
            self._validate_signature(signature, path, profile_id)
            decoded_changes.append((path, before, signature))

        decoded_guards = []
        for guard in guards:
            if not isinstance(guard, dict) or set(guard) != {"target", "before"}:
                raise AccountTransactionRecoveryError("账号存储事务日志守卫无效。")
            target = guard.get("target")
            if target not in _ACCOUNT_TRANSACTION_TARGETS or target in seen:
                raise AccountTransactionRecoveryError("账号存储事务日志包含重复或未知守卫。")
            seen.add(target)
            path = self._transaction_path(target, profile_id)
            signature = guard.get("before")
            self._validate_signature(signature, path, profile_id)
            decoded_guards.append((path, signature))

        expected_targets = {"manifest", "manifest_backup"}
        if operation in {"import", "update", "remove"}:
            expected_targets.add("profile")
        if seen != expected_targets:
            raise AccountTransactionRecoveryError("账号存储事务日志缺少或多出受管目标。")
        transaction["decoded_changes"] = decoded_changes
        transaction["decoded_guards"] = decoded_guards
        return transaction

    def _read_account_transaction(self) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
        protected = _read_optional_bytes(
            self.account_transaction_path,
            MAX_ACCOUNT_TRANSACTION_BYTES,
            "账号存储事务日志",
        )
        if protected is None:
            return None, None
        return protected, self._decode_account_transaction(protected)

    def _account_transaction_is_current(self, expected: bytes) -> bool:
        return _optional_bytes_match(
            self.account_transaction_path,
            expected,
            MAX_ACCOUNT_TRANSACTION_BYTES,
            "账号存储事务日志",
        )

    def _recover_pending_account_transaction_locked(self, abandon_external: bool = False) -> str:
        protected, transaction = self._read_account_transaction()
        if protected is None or transaction is None:
            return "none"
        profile_id = transaction.get("profile_id")

        guard_errors = []
        guard_values = []
        for path, signature in transaction["decoded_guards"]:
            current = _read_optional_bytes(path, self._path_limit(path, profile_id), path.name)
            guard_values.append(current)
            if not self._matches_signature(current, signature):
                guard_errors.append("{}已被外部修改。".format(path.name))

        states = []
        current_values = []
        for path, before, after_signature in transaction["decoded_changes"]:
            current = _read_optional_bytes(path, self._path_limit(path, profile_id), path.name)
            current_values.append(current)
            if current == before:
                states.append("before")
            elif self._matches_signature(current, after_signature):
                states.append("after")
            else:
                states.append("external")

        if all(state == "after" for state in states) and not guard_errors:
            _guarded_replace_optional(
                self.account_transaction_path,
                protected,
                None,
                MAX_ACCOUNT_TRANSACTION_BYTES,
                "账号存储事务日志",
            )
            return "committed"
        if all(state == "before" for state in states) and not guard_errors:
            _guarded_replace_optional(
                self.account_transaction_path,
                protected,
                None,
                MAX_ACCOUNT_TRANSACTION_BYTES,
                "账号存储事务日志",
            )
            return "rolled_back"

        recovery_errors = list(guard_errors)
        for (path, before, _after_signature), current, state in reversed(
            list(zip(transaction["decoded_changes"], current_values, states))
        ):
            if state == "external":
                recovery_errors.append("{}包含外部新内容，未覆盖。".format(path.name))
                continue
            if state == "before":
                continue
            try:
                if not self._account_transaction_is_current(protected):
                    raise AccountTransactionRecoveryError("账号存储事务日志在恢复期间被修改。")
                _guarded_replace_optional(
                    path,
                    current,
                    before,
                    self._path_limit(path, profile_id),
                    path.name,
                )
            except (OSError, AccountStoreError) as exc:
                recovery_errors.append(str(exc))

        fully_restored = True
        external_stable = True
        for (path, before, _after_signature), original_current, state in zip(
            transaction["decoded_changes"], current_values, states
        ):
            try:
                current = _read_optional_bytes(path, self._path_limit(path, profile_id), path.name)
                if state == "external":
                    if current != original_current:
                        external_stable = False
                elif current != before:
                    fully_restored = False
            except AccountStoreError:
                fully_restored = False

        guards_stable = True
        for (path, _signature), original_current in zip(
            transaction["decoded_guards"], guard_values
        ):
            try:
                if (
                    _read_optional_bytes(path, self._path_limit(path, profile_id), path.name)
                    != original_current
                ):
                    guards_stable = False
            except AccountStoreError:
                guards_stable = False

        if (
            abandon_external
            and fully_restored
            and external_stable
            and guards_stable
            and recovery_errors
        ):
            _guarded_replace_optional(
                self.account_transaction_path,
                protected,
                None,
                MAX_ACCOUNT_TRANSACTION_BYTES,
                "账号存储事务日志",
            )
            return "abandoned"
        if recovery_errors or not fully_restored or not guards_stable:
            detail = "；".join(recovery_errors) or "部分文件未能恢复到事务前状态。"
            raise AccountTransactionRecoveryError(
                "检测到未完成的账号存储事务，但无法安全恢复：{}".format(detail)
            )
        _guarded_replace_optional(
            self.account_transaction_path,
            protected,
            None,
            MAX_ACCOUNT_TRANSACTION_BYTES,
            "账号存储事务日志",
        )
        return "rolled_back"

    def _all_storage_replacements_match(
        self,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
        profile_id: Optional[str],
    ) -> bool:
        for path, _expected, replacement in changes:
            if not _optional_bytes_match(
                path,
                replacement,
                self._path_limit(path, profile_id),
                path.name,
            ):
                return False
        return True

    def _rollback_storage_changes_without_journal(
        self,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
        profile_id: Optional[str],
    ) -> str:
        """Compensate owned partial writes if the account journal disappears."""

        snapshots = []
        has_external = False
        for path, before, replacement in changes:
            current = _read_optional_bytes(path, self._path_limit(path, profile_id), path.name)
            if current == before:
                state = "before"
            elif current == replacement:
                state = "after"
            else:
                state = "external"
                has_external = True
            snapshots.append((path, before, replacement, current, state))

        if all(state == "after" for *_values, state in snapshots):
            return "committed"

        recovery_errors = []
        for path, before, _replacement, current, state in reversed(snapshots):
            if state != "after" or current == before:
                continue
            try:
                _guarded_replace_optional(
                    path,
                    current,
                    before,
                    self._path_limit(path, profile_id),
                    path.name,
                )
            except (OSError, AccountStoreError) as exc:
                recovery_errors.append(str(exc))

        for path, before, _replacement, original_current, state in snapshots:
            try:
                current = _read_optional_bytes(path, self._path_limit(path, profile_id), path.name)
            except AccountStoreError as exc:
                recovery_errors.append(str(exc))
                continue
            if state == "external":
                if current != original_current:
                    recovery_errors.append("{}的外部内容再次发生变化。".format(path.name))
            elif current != before:
                recovery_errors.append("{}未能恢复到事务前状态。".format(path.name))

        if recovery_errors:
            raise AccountTransactionRecoveryError(
                "账号存储事务日志已消失，且部分写入无法安全恢复：{}".format(
                    "；".join(recovery_errors)
                )
            )
        return "abandoned" if has_external else "rolled_back"

    def _assert_storage_unchanged(
        self,
        path: Path,
        expected: Optional[bytes],
        profile_id: Optional[str],
    ) -> None:
        if not _optional_bytes_match(
            path,
            expected,
            self._path_limit(path, profile_id),
            path.name,
        ):
            raise AccountStoreError(
                "{}已在操作期间被外部修改。为避免覆盖，本次操作已取消。".format(path.name)
            )

    def _commit_storage_transaction(
        self,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
        operation: str,
        profile_id: Optional[str],
    ) -> None:
        targets = []
        for path, before, replacement in changes:
            target = self._transaction_target(path, profile_id)
            if target in targets:
                raise AccountStoreError("账号存储事务包含重复目标。")
            targets.append(target)
            limit = self._path_limit(path, profile_id)
            if before is not None and len(before) > limit:
                raise AccountStoreError("{}事务前内容过大。".format(path.name))
            if replacement is not None and len(replacement) > limit:
                raise AccountStoreError("{}过大，已拒绝写入。".format(path.name))
            self._assert_storage_unchanged(path, before, profile_id)

        expected_targets = {"manifest", "manifest_backup"}
        if operation in {"import", "update", "remove"}:
            expected_targets.add("profile")
        if set(targets) != expected_targets:
            raise AccountStoreError("账号存储事务缺少或多出受管目标。")

        effective_changes = [change for change in changes if change[1] != change[2]]
        if not effective_changes:
            return
        transaction_bytes = self._encode_account_transaction(operation, profile_id, changes)
        _guarded_replace_optional(
            self.account_transaction_path,
            None,
            transaction_bytes,
            MAX_ACCOUNT_TRANSACTION_BYTES,
            "账号存储事务日志",
        )

        try:
            if not self._account_transaction_is_current(transaction_bytes):
                raise AccountTransactionRecoveryError("账号存储事务日志已在提交前被外部修改。")
            for path, expected, replacement in changes:
                if not self._account_transaction_is_current(transaction_bytes):
                    raise AccountTransactionRecoveryError(
                        "账号存储事务日志已在提交期间被外部修改。"
                    )
                self._assert_storage_unchanged(path, expected, profile_id)
                if expected == replacement:
                    continue
                _guarded_replace_optional(
                    path,
                    expected,
                    replacement,
                    self._path_limit(path, profile_id),
                    path.name,
                )
                if not _optional_bytes_match(
                    path,
                    replacement,
                    self._path_limit(path, profile_id),
                    path.name,
                ):
                    raise AccountStoreError("{}在写入后再次发生变化。".format(path.name))

            for path, _expected, replacement in changes:
                if not _optional_bytes_match(
                    path,
                    replacement,
                    self._path_limit(path, profile_id),
                    path.name,
                ):
                    raise AccountStoreError("{}在事务完成前再次发生变化。".format(path.name))
            _guarded_replace_optional(
                self.account_transaction_path,
                transaction_bytes,
                None,
                MAX_ACCOUNT_TRANSACTION_BYTES,
                "账号存储事务日志",
            )
        except (OSError, AccountStoreError) as exc:
            try:
                recovery = self._recover_pending_account_transaction_locked(abandon_external=True)
            except AccountStoreError as recovery_error:
                raise AccountTransactionRecoveryError(
                    "账号存储写入失败，且未能完整恢复：{}".format(recovery_error)
                ) from exc
            if recovery == "committed" or (
                recovery == "none" and self._all_storage_replacements_match(changes, profile_id)
            ):
                return
            if recovery == "none":
                recovery = self._rollback_storage_changes_without_journal(changes, profile_id)
                if recovery == "committed":
                    return
            if recovery == "abandoned":
                raise AccountStoreError(
                    "检测到操作期间的外部修改；已恢复本程序自身写入并保留外部内容。"
                ) from exc
            if isinstance(exc, AccountStoreError):
                raise
            raise AccountStoreError("无法写入账号存储：{}".format(exc)) from exc

    def _recover_legacy_remove_tombstones_locked(self) -> None:
        if not self.storage_directory.exists():
            return
        try:
            entries = tuple(self.storage_directory.iterdir())
        except OSError as exc:
            raise AccountTransactionRecoveryError(
                "无法检查旧版移除临时文件：{}".format(exc)
            ) from exc
        tombstones = []
        seen_ids = set()
        for path in entries:
            match = _LEGACY_REMOVE_PATTERN.fullmatch(path.name)
            if match is None:
                continue
            profile_id = match.group("profile_id")
            if profile_id in seen_ids:
                raise AccountTransactionRecoveryError(
                    "同一账号存在多个旧版移除临时文件，已停止自动恢复。"
                )
            seen_ids.add(profile_id)
            tombstones.append((profile_id, path))
        if not tombstones:
            return

        manifest_bytes = _read_optional_bytes(
            self.manifest_path,
            MAX_MANIFEST_FILE_BYTES,
            "账号清单",
        )
        if manifest_bytes is None:
            raise AccountTransactionRecoveryError(
                "发现旧版移除临时文件，但主账号清单缺失，无法判断应恢复还是删除。"
            )
        try:
            manifest_profiles = self._read_manifest(self.manifest_path)
        except AccountStoreError as exc:
            raise AccountTransactionRecoveryError(
                "发现旧版移除临时文件，但主账号清单不可用，已停止自动恢复。"
            ) from exc
        manifest_ids = {profile.profile_id for profile in manifest_profiles}

        for profile_id, tombstone in tombstones:
            if not _optional_bytes_match(
                self.manifest_path,
                manifest_bytes,
                MAX_MANIFEST_FILE_BYTES,
                "账号清单",
            ):
                raise AccountTransactionRecoveryError("账号清单在旧版恢复期间被外部修改。")
            tombstone_bytes = _read_optional_bytes(
                tombstone,
                MAX_AUTH_FILE_BYTES,
                "旧版移除临时文件",
            )
            if tombstone_bytes is None:
                continue
            source = self._profile_path_from_id(profile_id)
            source_bytes = _read_optional_bytes(
                source,
                MAX_AUTH_FILE_BYTES,
                "账号认证副本",
            )
            if profile_id in manifest_ids:
                if source_bytes is None:
                    _guarded_replace_optional(
                        source,
                        None,
                        tombstone_bytes,
                        MAX_AUTH_FILE_BYTES,
                        source.name,
                    )
                elif source_bytes != tombstone_bytes:
                    raise AccountTransactionRecoveryError(
                        "旧版移除恢复遇到外部新认证副本，已保留双方内容并停止。"
                    )
                _guarded_replace_optional(
                    tombstone,
                    tombstone_bytes,
                    None,
                    MAX_AUTH_FILE_BYTES,
                    tombstone.name,
                )
            else:
                if source_bytes is not None:
                    raise AccountTransactionRecoveryError(
                        "旧版移除状态与主账号清单冲突，已停止自动清理。"
                    )
                _guarded_replace_optional(
                    tombstone,
                    tombstone_bytes,
                    None,
                    MAX_AUTH_FILE_BYTES,
                    tombstone.name,
                )

    @contextmanager
    def codex_operation_guard(self, target_auth_path: Path) -> Iterator[None]:
        """Serialize a Codex mutation across threads and app processes."""

        with codex_operation_guard(target_auth_path, self.lock_timeout):
            yield

    @contextmanager
    def account_switch_guard(self, target_auth_path: Path) -> Iterator[None]:
        """Serialize one complete account switch and its profile-store writes."""

        with self.codex_operation_guard(target_auth_path):
            with self._storage_guard():
                yield

    def ensure_storage(self) -> None:
        try:
            self.storage_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AccountStoreError(
                "无法创建账号文件夹。请将程序移动到有写入权限的目录后重试：{}".format(exc)
            ) from exc

    def profile_path(self, profile: AccountProfile) -> Path:
        return self.storage_directory / profile.file_name

    def profile_is_available(self, profile: AccountProfile) -> bool:
        return self.profile_path(profile).is_file()

    @staticmethod
    def _find_profile(profiles: List[AccountProfile], profile_id: str) -> AccountProfile:
        profile = next((item for item in profiles if item.profile_id == profile_id), None)
        if profile is None:
            raise ProfileNotFoundError("所选账号已不在账号列表中，请刷新后重试。")
        return profile

    def _read_manifest(self, path: Path) -> List[AccountProfile]:
        try:
            if path.stat().st_size > MAX_MANIFEST_FILE_BYTES:
                raise AccountStoreError("账号清单文件过大，已拒绝读取。")
            text = path.read_text(encoding="utf-8-sig")
            payload = json.loads(text, parse_constant=_reject_non_standard_number)
        except AccountStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AccountStoreError("账号清单损坏或无法读取：{}".format(exc)) from exc

        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise AccountStoreError("账号清单格式或版本不受支持。")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise AccountStoreError("账号清单缺少 profiles 列表。")

        profiles = [AccountProfile.from_dict(item) for item in raw_profiles]
        ids = [profile.profile_id for profile in profiles]
        file_names = [profile.file_name for profile in profiles]
        if len(ids) != len(set(ids)) or len(file_names) != len(set(file_names)):
            raise AccountStoreError("账号清单包含重复记录。")
        return profiles

    def load_profiles(self) -> List[AccountProfile]:
        with self._storage_guard():
            return self._load_profiles_locked()

    def _load_profiles_locked(self) -> List[AccountProfile]:
        self.ensure_storage()
        self.recovered_from_backup = False

        if not self.manifest_path.exists():
            if self.manifest_backup_path.exists():
                profiles = self._read_manifest(self.manifest_backup_path)
                self.recovered_from_backup = True
                return profiles
            return []

        try:
            return self._read_manifest(self.manifest_path)
        except AccountStoreError as primary_error:
            if self.manifest_backup_path.exists():
                try:
                    profiles = self._read_manifest(self.manifest_backup_path)
                except AccountStoreError:
                    pass
                else:
                    self.recovered_from_backup = True
                    return profiles
            raise primary_error

    @staticmethod
    def _serialize_profiles(profiles: List[AccountProfile]) -> bytes:
        payload: Dict[str, Any] = {
            "version": 1,
            "profiles": [profile.to_dict() for profile in profiles],
        }
        try:
            value = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise AccountStoreError("无法序列化账号清单：{}".format(exc)) from exc
        if len(value) > MAX_MANIFEST_FILE_BYTES:
            raise AccountStoreError("账号清单文件过大，已拒绝写入。")
        return value

    def _save_profiles(
        self,
        profiles: List[AccountProfile],
        *,
        operation: str = "repair",
        profile_id: Optional[str] = None,
        profile_change: Optional[Tuple[Path, Optional[bytes], Optional[bytes]]] = None,
    ) -> None:
        before_manifest = _read_optional_bytes(
            self.manifest_path,
            MAX_MANIFEST_FILE_BYTES,
            "账号清单",
        )
        before_backup = _read_optional_bytes(
            self.manifest_backup_path,
            MAX_MANIFEST_FILE_BYTES,
            "账号清单备份",
        )
        after_manifest = self._serialize_profiles(profiles)
        after_backup = (
            before_manifest
            if before_manifest is not None and not self.recovered_from_backup
            else before_backup
        )
        changes: List[Tuple[Path, Optional[bytes], Optional[bytes]]] = []
        if profile_change is not None:
            changes.append(profile_change)
        changes.extend(
            (
                (self.manifest_backup_path, before_backup, after_backup),
                (self.manifest_path, before_manifest, after_manifest),
            )
        )
        self._commit_storage_transaction(changes, operation, profile_id)
        self.recovered_from_backup = False

    def import_profile(self, source_path: Path, remark: str) -> ImportResult:
        with self._storage_guard():
            return self._import_profile_locked(source_path, remark)

    def _import_profile_locked(self, source_path: Path, remark: str) -> ImportResult:
        self.ensure_storage()
        source = Path(source_path).resolve()
        normalized_remark = _normalize_remark(remark)
        source_info = _inspect_auth_file(source)
        source_hash = source_info.sha256
        profiles = self.load_profiles()

        for profile in profiles:
            path = self.profile_path(profile)
            if not path.is_file():
                continue
            try:
                stored_hash = _inspect_auth_file(path).sha256
            except InvalidAuthFileError:
                continue
            if stored_hash == source_hash:
                if profile.sha256 != source_hash:
                    raise AccountStoreError(
                        "认证副本“{}”已在外部修改且与待添加文件相同。"
                        "请先手动更新或移除该记录。".format(profile.remark)
                    )
                return ImportResult(profile=profile, created=False)

        identity_matches = []
        if source_info.managed_identity is not None:
            for profile in profiles:
                path = self.profile_path(profile)
                if not path.is_file():
                    continue
                try:
                    stored_info = _inspect_auth_file(path)
                except InvalidAuthFileError:
                    continue
                if stored_info.managed_identity != source_info.managed_identity:
                    continue
                if stored_info.sha256 != profile.sha256:
                    raise AccountStoreError(
                        "同一 ChatGPT 账号的认证副本“{}”已在外部修改。"
                        "请先手动更新或移除该记录。".format(profile.remark)
                    )
                identity_matches.append(profile)

        if len(identity_matches) == 1:
            return ImportResult(profile=identity_matches[0], created=False)
        if len(identity_matches) > 1:
            raise AccountStoreError("同一 ChatGPT 账号已有多个记录，请先保留正确账号并移除重复项。")

        profile_id = uuid.uuid4().hex
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        profile = AccountProfile(
            profile_id=profile_id,
            remark=normalized_remark,
            file_name="auth_{}.json".format(profile_id),
            added_at=now,
            updated_at=now,
            sha256=source_hash,
        )
        destination = self.profile_path(profile)
        try:
            if destination.exists():
                raise AccountStoreError("新账号的目标文件已存在，已停止导入以免覆盖外部文件。")
            source_bytes = _read_optional_bytes(
                source,
                MAX_AUTH_FILE_BYTES,
                "源认证文件",
            )
            if source_bytes is None or hashlib.sha256(source_bytes).hexdigest() != source_hash:
                raise AccountStoreError("源认证文件在导入过程中发生变化，请重新选择后再试。")
            if not _optional_bytes_match(
                source,
                source_bytes,
                MAX_AUTH_FILE_BYTES,
                "源认证文件",
            ):
                raise AccountStoreError("源认证文件在导入过程中发生变化，请重新选择后再试。")
            self._save_profiles(
                profiles + [profile],
                operation="import",
                profile_id=profile.profile_id,
                profile_change=(destination, None, source_bytes),
            )
        except (OSError, AccountStoreError) as exc:
            if isinstance(exc, AccountStoreError):
                raise
            raise AccountStoreError("保存账号认证文件失败：{}".format(exc)) from exc

        return ImportResult(profile=profile, created=True)

    def rename_profile(self, profile_id: str, remark: str) -> AccountProfile:
        with self._storage_guard():
            return self._rename_profile_locked(profile_id, remark)

    def _rename_profile_locked(self, profile_id: str, remark: str) -> AccountProfile:
        profiles = self.load_profiles()
        profile = self._find_profile(profiles, profile_id)
        normalized_remark = _normalize_remark(remark)
        if normalized_remark == profile.remark:
            return profile

        renamed = replace(profile, remark=normalized_remark)
        updated_profiles = [renamed if item.profile_id == profile_id else item for item in profiles]
        try:
            self._save_profiles(
                updated_profiles,
                operation="rename",
                profile_id=profile_id,
            )
        except OSError as exc:
            raise AccountStoreError("保存账号备注失败：{}".format(exc)) from exc
        return renamed

    def update_profile(
        self,
        profile_id: str,
        source_path: Path,
        *,
        expected_sha256: Optional[str] = None,
    ) -> UpdateResult:
        with self._storage_guard():
            return self._update_profile_locked(
                profile_id,
                source_path,
                expected_sha256=expected_sha256,
            )

    def _update_profile_locked(
        self,
        profile_id: str,
        source_path: Path,
        *,
        expected_sha256: Optional[str] = None,
    ) -> UpdateResult:
        self.ensure_storage()
        source = Path(source_path).resolve()
        source_info = _inspect_auth_file(source)
        source_hash = source_info.sha256
        source_bytes = _read_optional_bytes(source, MAX_AUTH_FILE_BYTES, "当前认证文件")
        if source_bytes is None or hashlib.sha256(source_bytes).hexdigest() != source_hash:
            raise AccountStoreError("当前认证文件在读取过程中发生变化，请重试。")
        profiles = self.load_profiles()
        profile = self._find_profile(profiles, profile_id)
        destination = self.profile_path(profile)

        if expected_sha256 is not None and profile.sha256 != expected_sha256:
            raise AccountStoreError(
                "“{}”已被另一项操作更新，本次自动保存已取消，以免覆盖较新的认证。".format(
                    profile.remark
                )
            )

        destination_exists = destination.exists()
        if destination_exists and not destination.is_file():
            raise AccountStoreError("所选账号的认证文件不是普通文件，无法更新。")

        stored_info: Optional[_AuthFileInfo] = None
        destination_hash: Optional[str] = None
        destination_bytes: Optional[bytes] = None
        if destination_exists:
            try:
                stored_info = _inspect_auth_file(destination)
            except InvalidAuthFileError:
                stored_info = None
            try:
                destination_hash = (
                    stored_info.sha256 if stored_info is not None else _sha256_file(destination)
                )
            except OSError as exc:
                raise AccountStoreError("无法校验所选账号的现有认证副本：{}".format(exc)) from exc
            destination_bytes = _read_optional_bytes(
                destination,
                MAX_AUTH_FILE_BYTES,
                "账号认证副本",
            )
            if (
                destination_bytes is None
                or destination_hash is None
                or hashlib.sha256(destination_bytes).hexdigest() != destination_hash
            ):
                raise AccountStoreError(
                    "“{}”的认证副本在读取过程中发生变化，本次更新已取消。".format(profile.remark)
                )

        if expected_sha256 is not None and (
            stored_info is None or stored_info.sha256 != expected_sha256
        ):
            raise AccountStoreError(
                "“{}”的认证副本已发生变化，本次自动保存已取消，以免覆盖较新的认证。".format(
                    profile.remark
                )
            )

        if stored_info is not None and stored_info.sha256 == source_hash:
            if profile.sha256 == source_hash:
                return UpdateResult(profile=profile, changed=False, backup_path=None)
            manifest_repair_needed = True
        else:
            manifest_repair_needed = False

        for other in profiles:
            if other.profile_id == profile_id or not self.profile_is_available(other):
                continue
            try:
                other_info = _inspect_auth_file(self.profile_path(other))
            except InvalidAuthFileError:
                continue
            if other_info.sha256 == source_hash:
                raise AccountStoreError(
                    "当前认证文件与“{}”相同，请直接使用该账号。".format(other.remark)
                )
            if (
                source_info.managed_identity is not None
                and other_info.managed_identity == source_info.managed_identity
            ):
                if other_info.sha256 != other.sha256:
                    raise AccountStoreError(
                        "同一 ChatGPT 账号的认证副本“{}”已在外部修改。请先处理该记录。".format(
                            other.remark
                        )
                    )
                raise AccountStoreError(
                    "当前认证属于已保存的“{}”，请直接使用该账号。".format(other.remark)
                )

        if (
            stored_info is not None
            and stored_info.managed_identity is not None
            and source_info.managed_identity is not None
            and stored_info.managed_identity != source_info.managed_identity
        ):
            raise AccountStoreError(
                "当前 Codex 登录与“{}”不是同一 ChatGPT 账号，已拒绝覆盖。"
                "请将当前认证作为新账号添加。".format(profile.remark)
            )
        if (
            stored_info is not None
            and MANAGED_CHATGPT_AUTH_MODE in (stored_info.auth_mode, source_info.auth_mode)
            and (
                stored_info.auth_mode != source_info.auth_mode
                or stored_info.managed_identity is None
                or source_info.managed_identity is None
            )
        ):
            raise AccountStoreError(
                "无法确认当前认证与“{}”属于同一 ChatGPT 账号，已拒绝覆盖。"
                "请将当前认证作为新账号添加。".format(profile.remark)
            )

        if manifest_repair_needed:
            repaired = replace(
                profile,
                updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                sha256=source_hash,
            )
            repaired_profiles = [
                repaired if item.profile_id == profile_id else item for item in profiles
            ]
            try:
                self._save_profiles(
                    repaired_profiles,
                    operation="repair",
                    profile_id=profile_id,
                )
            except OSError as exc:
                raise AccountStoreError("修复账号记录失败：{}".format(exc)) from exc
            return UpdateResult(profile=repaired, changed=True, backup_path=None)

        backup_path: Optional[Path] = None
        try:
            if destination_exists:
                if (
                    destination_hash is None
                    or destination_bytes is None
                    or not _optional_bytes_match(
                        destination,
                        destination_bytes,
                        MAX_AUTH_FILE_BYTES,
                        "账号认证副本",
                    )
                ):
                    raise AccountStoreError(
                        "“{}”的认证副本刚刚发生变化，本次更新已取消，以免覆盖较新的认证。".format(
                            profile.remark
                        )
                    )
                self.update_backup_directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                backup_path = self.update_backup_directory / "auth_{}_{}.json".format(
                    profile.profile_id,
                    stamp,
                )
                _atomic_copy(
                    destination,
                    backup_path,
                    expected_sha256=destination_hash,
                )
                if _sha256_file(backup_path) != destination_hash:
                    raise AccountStoreError(
                        "“{}”的认证副本在备份时发生变化，本次更新已取消。".format(profile.remark)
                    )
                if not _optional_bytes_match(
                    destination,
                    destination_bytes,
                    MAX_AUTH_FILE_BYTES,
                    "账号认证副本",
                ):
                    raise AccountStoreError(
                        "“{}”的认证副本在备份后发生变化，本次更新已取消，以免覆盖较新的认证。".format(
                            profile.remark
                        )
                    )
            elif destination.exists():
                raise AccountStoreError(
                    "“{}”的认证副本刚刚被外部创建，本次更新已取消，以免覆盖新内容。".format(
                        profile.remark
                    )
                )
            if not _optional_bytes_match(
                source,
                source_bytes,
                MAX_AUTH_FILE_BYTES,
                "当前认证文件",
            ):
                raise AccountStoreError("当前认证文件在更新过程中发生变化，请重试。")

            updated = replace(
                profile,
                updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                sha256=source_hash,
            )
            updated_profiles = [
                updated if item.profile_id == profile_id else item for item in profiles
            ]
            self._save_profiles(
                updated_profiles,
                operation="update",
                profile_id=profile_id,
                profile_change=(destination, destination_bytes, source_bytes),
            )
        except (OSError, AccountStoreError) as exc:
            if isinstance(exc, AccountStoreError):
                raise
            raise AccountStoreError("更新账号认证文件失败：{}".format(exc)) from exc

        return UpdateResult(profile=updated, changed=True, backup_path=backup_path)

    def remove_profile(self, profile_id: str) -> AccountProfile:
        with self._storage_guard():
            return self._remove_profile_locked(profile_id)

    def _remove_profile_locked(self, profile_id: str) -> AccountProfile:
        profiles = self.load_profiles()
        profile = self._find_profile(profiles, profile_id)
        remaining = [item for item in profiles if item.profile_id != profile_id]
        source = self.profile_path(profile)
        try:
            if source.exists():
                if not source.is_file():
                    raise AccountStoreError("所选账号的认证文件不是普通文件，无法移除。")
            source_bytes = _read_optional_bytes(
                source,
                MAX_AUTH_FILE_BYTES,
                "账号认证副本",
            )
            self._save_profiles(
                remaining,
                operation="remove",
                profile_id=profile.profile_id,
                profile_change=(source, source_bytes, None),
            )
        except (OSError, AccountStoreError) as exc:
            if isinstance(exc, AccountStoreError):
                raise
            raise AccountStoreError("移除账号失败：{}".format(exc)) from exc

        return profile

    def sync_current_profile(self, target_auth_path: Path) -> SyncResult:
        with self.account_switch_guard(target_auth_path):
            return self._sync_current_profile_locked(target_auth_path)

    def _sync_current_profile_locked(self, target_auth_path: Path) -> SyncResult:
        target = Path(target_auth_path)
        if not target.is_file():
            return SyncResult(SYNC_STATUS_MISSING, None, False, None)

        try:
            current_info = _inspect_auth_file(target)
        except InvalidAuthFileError:
            return SyncResult(SYNC_STATUS_UNSUPPORTED, None, False, None)

        profiles = self.load_profiles()
        exact_matches = []
        identity_matches = []
        identity_conflicts = []
        for profile in profiles:
            path = self.profile_path(profile)
            if not path.is_file():
                continue
            try:
                stored_info = _inspect_auth_file(path)
            except InvalidAuthFileError:
                continue

            trusted = stored_info.sha256 == profile.sha256
            if stored_info.sha256 == current_info.sha256:
                if trusted:
                    exact_matches.append(profile)
                else:
                    identity_conflicts.append(profile)
                continue
            if (
                current_info.managed_identity is not None
                and stored_info.managed_identity == current_info.managed_identity
            ):
                if trusted:
                    identity_matches.append(profile)
                else:
                    identity_conflicts.append(profile)

        if len(exact_matches) == 1:
            return SyncResult(SYNC_STATUS_UNCHANGED, exact_matches[0], False, None)
        if len(exact_matches) > 1:
            raise AccountStoreError(
                "多个账号记录与当前认证完全相同，无法安全判断当前账号。请移除重复记录后重试。"
            )
        if identity_conflicts:
            raise AccountStoreError(
                "与当前账号对应的认证副本“{}”已在外部修改，无法自动保存最新认证。"
                "请先从当前 Codex 手动更新或重新添加该账号。".format(identity_conflicts[0].remark)
            )
        if current_info.managed_identity is None:
            return SyncResult(SYNC_STATUS_UNSUPPORTED, None, False, None)
        if not identity_matches:
            return SyncResult(SYNC_STATUS_UNMATCHED, None, False, None)
        if len(identity_matches) > 1:
            remarks = "、".join("“{}”".format(item.remark) for item in identity_matches[:3])
            raise AccountStoreError(
                "多个账号副本具有同一 ChatGPT 身份（{}），无法安全判断应更新哪一项。"
                "请保留正确账号后重试。".format(remarks)
            )

        matched_profile = identity_matches[0]
        update = self.update_profile(
            matched_profile.profile_id,
            target,
            expected_sha256=matched_profile.sha256,
        )
        status = SYNC_STATUS_UPDATED if update.changed else SYNC_STATUS_UNCHANGED
        return SyncResult(status, update.profile, update.changed, update.backup_path)

    def active_profile_id(self, target_auth_path: Path) -> Optional[str]:
        with self._storage_guard():
            return self._active_profile_id_locked(target_auth_path)

    def _active_profile_id_locked(self, target_auth_path: Path) -> Optional[str]:
        target = Path(target_auth_path)
        if not target.is_file():
            return None
        try:
            target_info = _inspect_auth_file(target)
            profiles = self.load_profiles()
        except AccountStoreError:
            return None

        exact_matches = []
        identity_matches = []
        for profile in profiles:
            path = self.profile_path(profile)
            if not path.is_file():
                continue
            try:
                stored_info = _inspect_auth_file(path)
            except InvalidAuthFileError:
                continue
            if stored_info.sha256 != profile.sha256:
                continue
            if stored_info.sha256 == target_info.sha256:
                exact_matches.append(profile.profile_id)
            elif (
                target_info.managed_identity is not None
                and stored_info.managed_identity == target_info.managed_identity
            ):
                identity_matches.append(profile.profile_id)

        if len(exact_matches) == 1:
            return exact_matches[0]
        if not exact_matches and len(identity_matches) == 1:
            return identity_matches[0]
        return None

    def switch_profile(self, profile_id: str, target_auth_path: Path) -> SwitchResult:
        with self.account_switch_guard(target_auth_path):
            return self._switch_profile_locked(profile_id, target_auth_path)

    def _switch_profile_locked(
        self,
        profile_id: str,
        target_auth_path: Path,
    ) -> SwitchResult:
        profiles = self.load_profiles()
        profile = self._find_profile(profiles, profile_id)

        source = self.profile_path(profile)
        if not source.is_file():
            raise AccountStoreError("所选账号的认证文件已丢失，请重新添加。")

        source_hash = validate_auth_file(source)
        if source_hash != profile.sha256:
            raise AccountStoreError("所选账号文件已在外部被修改。为确保安全，请重新添加该文件。")

        target = Path(target_auth_path)
        target_existed = target.exists()
        target_hash: Optional[str] = None
        if target_existed:
            if not target.is_file():
                raise AccountStoreError("目标 auth.json 不是普通文件，已停止切换。")
            try:
                target_hash = _sha256_file(target)
                if target_hash == source_hash:
                    return SwitchResult(
                        changed=False,
                        target_path=target,
                        backup_path=None,
                        replacement_sha256=source_hash,
                        backup_sha256=None,
                    )
            except OSError as exc:
                raise AccountStoreError("无法读取当前 Codex 认证文件：{}".format(exc)) from exc

        backup_path: Optional[Path] = None
        copied = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() != target_existed:
                raise AccountStoreError(
                    "当前 Codex 认证文件刚刚发生变化，本次切换已取消，以免覆盖新的登录状态。"
                )
            if target_existed:
                if not target.is_file() or target_hash is None:
                    raise AccountStoreError(
                        "当前 Codex 认证文件刚刚发生变化，本次切换已取消，以免覆盖新的登录状态。"
                    )
                self.backup_directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                backup_path = self.backup_directory / "auth_{}.json".format(stamp)
                _atomic_copy(target, backup_path, expected_sha256=target_hash)
                if (
                    _sha256_file(backup_path) != target_hash
                    or not target.is_file()
                    or _sha256_file(target) != target_hash
                ):
                    raise AccountStoreError(
                        "当前 Codex 认证文件在备份后发生变化，本次切换已取消，以免覆盖新的登录状态。"
                    )

            _atomic_copy(source, target, expected_sha256=source_hash)
            copied = True
            if _sha256_file(target) != source_hash:
                raise AccountStoreError("替换后的认证文件校验失败。")
        except (OSError, AccountStoreError) as exc:
            if copied:
                try:
                    target_still_ours = target.is_file() and _sha256_file(target) == source_hash
                except OSError:
                    target_still_ours = False
                if not target_still_ours:
                    backup_note = (
                        "原认证已保留在切换备份目录"
                        if backup_path is not None
                        else "切换前没有原认证文件"
                    )
                    raise AccountStoreError(
                        "切换失败，且当前认证在写入后又发生变化；为避免覆盖新的登录状态，"
                        "未自动恢复。{}。".format(backup_note)
                    ) from exc
                try:
                    if backup_path is not None and backup_path.is_file():
                        if target_hash is not None and _sha256_file(backup_path) != target_hash:
                            raise OSError("切换备份校验失败")
                        _atomic_copy(
                            backup_path,
                            target,
                            expected_sha256=target_hash,
                        )
                    else:
                        target.unlink()
                except FileNotFoundError:
                    pass
                except (OSError, AccountStoreError) as restore_error:
                    raise AccountStoreError(
                        "切换失败，且无法恢复原认证文件：{}".format(restore_error)
                    ) from exc
            if isinstance(exc, AccountStoreError):
                if copied:
                    message = str(exc).rstrip("。")
                    raise AccountStoreError("{}，已恢复原认证文件。".format(message)) from exc
                raise
            restore_note = "，已恢复原认证文件" if copied else ""
            raise AccountStoreError(
                "切换账号失败：{}{}。请确认目录可写，并完全退出可能占用文件的程序后重试。".format(
                    exc,
                    restore_note,
                )
            ) from exc

        return SwitchResult(
            changed=True,
            target_path=target,
            backup_path=backup_path,
            replacement_sha256=source_hash,
            backup_sha256=target_hash,
        )

    def rollback_switch(self, result: SwitchResult) -> None:
        if not isinstance(result, SwitchResult) or not result.changed:
            return
        with self.account_switch_guard(result.target_path):
            self._rollback_switch_locked(result)

    def _rollback_switch_locked(self, result: SwitchResult) -> None:
        target = Path(result.target_path)
        try:
            if result.replacement_sha256 is not None:
                if not target.is_file() or _sha256_file(target) != result.replacement_sha256:
                    raise AccountStoreError(
                        "认证文件在切换后已再次发生变化，已拒绝回滚，以免覆盖新的登录状态。"
                    )
            if result.backup_path is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                return

            backup = Path(result.backup_path).resolve()
            backup_root = self.backup_directory.resolve()
            try:
                backup.relative_to(backup_root)
            except ValueError as exc:
                raise AccountStoreError("认证备份路径无效，无法回滚账号切换。") from exc
            if not backup.is_file():
                raise AccountStoreError("认证备份已丢失，无法回滚账号切换。")
            if result.backup_sha256 is not None and _sha256_file(backup) != result.backup_sha256:
                raise AccountStoreError("认证备份已被修改，无法安全回滚账号切换。")
            _atomic_copy(
                backup,
                target,
                expected_sha256=result.backup_sha256,
            )
        except AccountStoreError:
            raise
        except OSError as exc:
            raise AccountStoreError("无法恢复原认证文件：{}".format(exc)) from exc
