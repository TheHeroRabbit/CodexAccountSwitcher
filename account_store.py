"""Validated, atomic storage for local Codex authentication profiles."""

from __future__ import annotations

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
from typing import Any, BinaryIO, Dict, Iterator, List, Optional

STORAGE_FOLDER_NAME = "账号文件"
BACKUP_FOLDER_NAME = "切换备份"
UPDATE_BACKUP_FOLDER_NAME = "更新备份"
MANIFEST_FILE_NAME = "profiles.json"
MANIFEST_BACKUP_FILE_NAME = "profiles.json.bak"
STORAGE_LOCK_FILE_NAME = ".profiles.lock"
TARGET_LOCK_FILE_NAME = ".codex-account-switcher.lock"
MAX_AUTH_FILE_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_FILE_BYTES = 2 * 1024 * 1024
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
_LOCK_RETRY_SECONDS = 0.05
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: Dict[str, "_InterprocessFileLock"] = {}


class AccountStoreError(Exception):
    """Base error for account storage operations."""


class InvalidAuthFileError(AccountStoreError):
    """Raised when a selected auth file is not a usable JSON object."""


class ProfileNotFoundError(AccountStoreError):
    """Raised when a selected profile no longer exists."""


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
    temporary = destination.parent / ".{}.{}.tmp".format(destination.name, uuid.uuid4().hex)
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
    temporary = destination.parent / ".{}.{}.tmp".format(destination.name, uuid.uuid4().hex)
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


class ProfileStore:
    def __init__(
        self,
        app_directory: Path,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise ValueError("lock_timeout must be a positive finite number")
        self.app_directory = Path(app_directory).resolve()
        self.storage_directory = self.app_directory / STORAGE_FOLDER_NAME
        self.backup_directory = self.storage_directory / BACKUP_FOLDER_NAME
        self.update_backup_directory = self.storage_directory / UPDATE_BACKUP_FOLDER_NAME
        self.manifest_path = self.storage_directory / MANIFEST_FILE_NAME
        self.manifest_backup_path = self.storage_directory / MANIFEST_BACKUP_FILE_NAME
        self.storage_lock_path = self.storage_directory / STORAGE_LOCK_FILE_NAME
        self.lock_timeout = float(lock_timeout)
        self.recovered_from_backup = False

    @contextmanager
    def _storage_guard(self) -> Iterator[None]:
        lock = _shared_file_lock(self.storage_lock_path)
        with lock.hold(self.lock_timeout, "账号存储锁"):
            yield

    @contextmanager
    def codex_operation_guard(self, target_auth_path: Path) -> Iterator[None]:
        """Serialize a Codex mutation across threads and app processes."""

        target = Path(target_auth_path).resolve()
        target_lock = _shared_file_lock(target.parent / TARGET_LOCK_FILE_NAME)
        with target_lock.hold(self.lock_timeout, "Codex 操作锁"):
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

    def _save_profiles(self, profiles: List[AccountProfile]) -> None:
        payload: Dict[str, Any] = {
            "version": 1,
            "profiles": [profile.to_dict() for profile in profiles],
        }

        if self.manifest_path.exists() and not self.recovered_from_backup:
            _atomic_copy(self.manifest_path, self.manifest_backup_path)
        _atomic_write_json(self.manifest_path, payload)
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
        copied = False

        try:
            if destination.exists():
                raise AccountStoreError("新账号的目标文件已存在，已停止导入以免覆盖外部文件。")
            _atomic_copy(source, destination, expected_sha256=source_hash)
            copied = True
            copied_hash = validate_auth_file(destination)
            if copied_hash != source_hash:
                raise AccountStoreError("源认证文件在导入过程中发生变化，请重新选择后再试。")
            self._save_profiles(profiles + [profile])
        except (OSError, AccountStoreError) as exc:
            if copied:
                try:
                    destination_still_ours = (
                        destination.is_file() and _sha256_file(destination) == source_hash
                    )
                except OSError:
                    destination_still_ours = False
                if not destination_still_ours:
                    raise AccountStoreError(
                        "导入失败，且认证副本在写入后又发生变化；"
                        "为避免删除外部内容，未自动清理目标文件。"
                    ) from exc
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
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
            self._save_profiles(updated_profiles)
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
                self._save_profiles(repaired_profiles)
            except OSError as exc:
                raise AccountStoreError("修复账号记录失败：{}".format(exc)) from exc
            return UpdateResult(profile=repaired, changed=True, backup_path=None)

        backup_path: Optional[Path] = None
        copied = False
        try:
            if destination_exists:
                if (
                    destination_hash is None
                    or not destination.is_file()
                    or _sha256_file(destination) != destination_hash
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
                if not destination.is_file() or _sha256_file(destination) != destination_hash:
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
            _atomic_copy(source, destination, expected_sha256=source_hash)
            copied = True

            copied_hash = validate_auth_file(destination)
            if copied_hash != source_hash:
                raise AccountStoreError("当前认证文件在更新过程中发生变化，请重试。")

            updated = replace(
                profile,
                updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                sha256=source_hash,
            )
            updated_profiles = [
                updated if item.profile_id == profile_id else item for item in profiles
            ]
            self._save_profiles(updated_profiles)
        except (OSError, AccountStoreError) as exc:
            if copied:
                try:
                    destination_still_ours = (
                        destination.is_file() and _sha256_file(destination) == source_hash
                    )
                except OSError:
                    destination_still_ours = False
                if not destination_still_ours:
                    backup_note = (
                        "旧副本已保留在“更新备份”目录"
                        if backup_path is not None
                        else "更新前没有旧副本"
                    )
                    raise AccountStoreError(
                        "更新失败，且认证副本在写入后又发生变化；为避免覆盖新内容，"
                        "未自动恢复。{}。".format(backup_note)
                    ) from exc
                try:
                    if backup_path is not None and backup_path.is_file():
                        _atomic_copy(
                            backup_path,
                            destination,
                            expected_sha256=destination_hash,
                        )
                    else:
                        destination.unlink()
                except FileNotFoundError:
                    pass
                except (OSError, AccountStoreError) as restore_error:
                    raise AccountStoreError(
                        "更新失败，且无法恢复原认证文件：{}".format(restore_error)
                    ) from exc
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
        tombstone = self.storage_directory / ".{}.{}.remove".format(
            source.name,
            uuid.uuid4().hex,
        )
        moved = False
        manifest_saved = False

        try:
            if source.exists():
                if not source.is_file():
                    raise AccountStoreError("所选账号的认证文件不是普通文件，无法移除。")
                os.replace(str(source), str(tombstone))
                moved = True

            self._save_profiles(remaining)
            manifest_saved = True
            if moved:
                tombstone.unlink()
        except (OSError, AccountStoreError) as exc:
            restore_errors = []
            if moved and tombstone.exists():
                try:
                    os.replace(str(tombstone), str(source))
                except OSError as restore_error:
                    restore_errors.append(str(restore_error))
            if manifest_saved:
                try:
                    self._save_profiles(profiles)
                except (OSError, AccountStoreError) as restore_error:
                    restore_errors.append(str(restore_error))
            if restore_errors:
                raise AccountStoreError(
                    "移除失败，且无法完整恢复原账号：{}".format("；".join(restore_errors))
                ) from exc
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
