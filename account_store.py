"""Validated, atomic storage for local Codex authentication profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

STORAGE_FOLDER_NAME = "账号文件"
BACKUP_FOLDER_NAME = "切换备份"
UPDATE_BACKUP_FOLDER_NAME = "更新备份"
MANIFEST_FILE_NAME = "profiles.json"
MANIFEST_BACKUP_FILE_NAME = "profiles.json.bak"
MAX_AUTH_FILE_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_FILE_BYTES = 2 * 1024 * 1024
MAX_REMARK_LENGTH = 60

_PROFILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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


@dataclass(frozen=True)
class UpdateResult:
    profile: AccountProfile
    changed: bool
    backup_path: Optional[Path]


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


def validate_auth_file(path: Path) -> str:
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
        text = source.read_text(encoding="utf-8-sig")
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

    try:
        return _sha256_file(source)
    except OSError as exc:
        raise InvalidAuthFileError("无法校验认证文件：{}".format(exc)) from exc


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / ".{}.{}.tmp".format(destination.name, uuid.uuid4().hex)
    try:
        shutil.copy2(str(source), str(temporary))
        with temporary.open("r+b") as copied:
            copied.flush()
            os.fsync(copied.fileno())
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
    def __init__(self, app_directory: Path) -> None:
        self.app_directory = Path(app_directory).resolve()
        self.storage_directory = self.app_directory / STORAGE_FOLDER_NAME
        self.backup_directory = self.storage_directory / BACKUP_FOLDER_NAME
        self.update_backup_directory = self.storage_directory / UPDATE_BACKUP_FOLDER_NAME
        self.manifest_path = self.storage_directory / MANIFEST_FILE_NAME
        self.manifest_backup_path = self.storage_directory / MANIFEST_BACKUP_FILE_NAME
        self.recovered_from_backup = False

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
        self.ensure_storage()
        source = Path(source_path).resolve()
        normalized_remark = _normalize_remark(remark)
        source_hash = validate_auth_file(source)
        profiles = self.load_profiles()

        for profile in profiles:
            if profile.sha256 == source_hash and self.profile_is_available(profile):
                return ImportResult(profile=profile, created=False)

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
            _atomic_copy(source, destination)
            copied_hash = validate_auth_file(destination)
            if copied_hash != source_hash:
                raise AccountStoreError("源认证文件在导入过程中发生变化，请重新选择后再试。")
            self._save_profiles(profiles + [profile])
        except (OSError, AccountStoreError) as exc:
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

    def update_profile(self, profile_id: str, source_path: Path) -> UpdateResult:
        self.ensure_storage()
        source = Path(source_path).resolve()
        source_hash = validate_auth_file(source)
        profiles = self.load_profiles()
        profile = self._find_profile(profiles, profile_id)
        destination = self.profile_path(profile)

        destination_exists = destination.exists()
        if destination_exists and not destination.is_file():
            raise AccountStoreError("所选账号的认证文件不是普通文件，无法更新。")

        stored_hash: Optional[str] = None
        if destination_exists:
            try:
                stored_hash = validate_auth_file(destination)
            except InvalidAuthFileError:
                stored_hash = None

        if stored_hash == source_hash:
            if profile.sha256 == source_hash:
                return UpdateResult(profile=profile, changed=False, backup_path=None)
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

        for other in profiles:
            if other.profile_id == profile_id or not self.profile_is_available(other):
                continue
            try:
                other_hash = validate_auth_file(self.profile_path(other))
            except InvalidAuthFileError:
                continue
            if other_hash == source_hash:
                raise AccountStoreError(
                    "当前认证文件与“{}”相同，请直接使用该账号。".format(other.remark)
                )

        backup_path: Optional[Path] = None
        copied = False
        try:
            if destination_exists:
                self.update_backup_directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                backup_path = self.update_backup_directory / "auth_{}_{}.json".format(
                    profile.profile_id,
                    stamp,
                )
                _atomic_copy(destination, backup_path)
            _atomic_copy(source, destination)
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
                    if backup_path is not None and backup_path.is_file():
                        _atomic_copy(backup_path, destination)
                    else:
                        destination.unlink()
                except FileNotFoundError:
                    pass
                except OSError as restore_error:
                    raise AccountStoreError(
                        "更新失败，且无法恢复原认证文件：{}".format(restore_error)
                    ) from exc
            if isinstance(exc, AccountStoreError):
                raise
            raise AccountStoreError("更新账号认证文件失败：{}".format(exc)) from exc

        return UpdateResult(profile=updated, changed=True, backup_path=backup_path)

    def remove_profile(self, profile_id: str) -> AccountProfile:
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

    def active_profile_id(self, target_auth_path: Path) -> Optional[str]:
        target = Path(target_auth_path)
        if not target.is_file():
            return None
        try:
            target_hash = _sha256_file(target)
            profiles = self.load_profiles()
        except (OSError, AccountStoreError):
            return None

        for profile in profiles:
            if profile.sha256 == target_hash and self.profile_is_available(profile):
                return profile.profile_id
        return None

    def switch_profile(self, profile_id: str, target_auth_path: Path) -> SwitchResult:
        profiles = self.load_profiles()
        profile = self._find_profile(profiles, profile_id)

        source = self.profile_path(profile)
        if not source.is_file():
            raise AccountStoreError("所选账号的认证文件已丢失，请重新添加。")

        source_hash = validate_auth_file(source)
        if source_hash != profile.sha256:
            raise AccountStoreError("所选账号文件已在外部被修改。为确保安全，请重新添加该文件。")

        target = Path(target_auth_path)
        if target.is_file():
            try:
                if _sha256_file(target) == source_hash:
                    return SwitchResult(changed=False, target_path=target, backup_path=None)
            except OSError as exc:
                raise AccountStoreError("无法读取当前 Codex 认证文件：{}".format(exc)) from exc

        backup_path: Optional[Path] = None
        copied = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file():
                    raise AccountStoreError("目标 auth.json 不是普通文件，已停止切换。")
                self.backup_directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                backup_path = self.backup_directory / "auth_{}.json".format(stamp)
                _atomic_copy(target, backup_path)

            _atomic_copy(source, target)
            copied = True
            if _sha256_file(target) != source_hash:
                raise AccountStoreError("替换后的认证文件校验失败。")
        except (OSError, AccountStoreError) as exc:
            if copied:
                try:
                    if backup_path is not None and backup_path.is_file():
                        _atomic_copy(backup_path, target)
                    else:
                        target.unlink()
                except FileNotFoundError:
                    pass
                except OSError as restore_error:
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

        return SwitchResult(changed=True, target_path=target, backup_path=backup_path)

    def rollback_switch(self, result: SwitchResult) -> None:
        if not isinstance(result, SwitchResult) or not result.changed:
            return
        target = Path(result.target_path)
        try:
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
            _atomic_copy(backup, target)
        except AccountStoreError:
            raise
        except OSError as exc:
            raise AccountStoreError("无法恢复原认证文件：{}".format(exc)) from exc
