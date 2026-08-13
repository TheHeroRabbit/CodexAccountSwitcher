"""Transactional Codex provider configuration with conflict-safe restoration."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

import tomlkit
from tomlkit.exceptions import ParseError

from account_store import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    AccountStoreError,
    codex_operation_guard,
)
from credential_store import CredentialStoreError, WindowsDataProtector, normalize_api_key

DEEPSEEK_MODEL_FLASH = "deepseek-v4-flash"
DEEPSEEK_MODEL_PRO = "deepseek-v4-pro"
SUPPORTED_DEEPSEEK_MODELS = (DEEPSEEK_MODEL_FLASH, DEEPSEEK_MODEL_PRO)
# Keep the historical default for callers that do not yet pass a model explicitly.
# The UI always passes its snapshotted selection.
DEFAULT_DEEPSEEK_MODEL = DEEPSEEK_MODEL_FLASH
# Backward-compatible alias for integrations that imported the v1.4 constant.
DEEPSEEK_MODEL = DEFAULT_DEEPSEEK_MODEL
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
STATE_FILE_NAME = "provider_state.dat"
TRANSACTION_FILE_NAME = ".codex-account-switcher-provider-transaction.dat"
TRANSACTION_VERSION = 1
MAX_CONFIG_BYTES = 5 * 1024 * 1024
MAX_MODELS_BYTES = 5 * 1024 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_TRANSACTION_BYTES = 40 * 1024 * 1024
OFFICIAL_MODELS_SHA256 = "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"
OFFICIAL_SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex"

TARGET_VALUES = {
    "model_provider": DEEPSEEK_PROVIDER,
    "preferred_auth_method": "apikey",
    "forced_login_method": "api",
    "model_reasoning_effort": "max",
}

CONFLICTING_ROOT_KEYS = (
    "profile",
    "profiles",
    "oss_provider",
    "openai_base_url",
    "model_context_window",
    "model_auto_compact_token_limit",
    "model_auto_compact_token_limit_scope",
    "base_instructions",
    "model_instructions_file",
    "compact_prompt",
    "experimental_compact_prompt_file",
    "service_tier",
    "model_verbosity",
    "model_reasoning_summary",
    "plan_mode_reasoning_effort",
    "experimental_use_unified_exec_tool",
)

# Keep this set identical to v1.4.0 so existing version-1 state fingerprints remain valid.
MANAGED_ROOT_KEYS = (
    ("model",) + tuple(TARGET_VALUES) + ("model_catalog_json",) + CONFLICTING_ROOT_KEYS
)


class ProviderConfigError(RuntimeError):
    """Base error for model-provider configuration operations."""


class InvalidCodexConfigError(ProviderConfigError):
    """Raised when an existing Codex configuration cannot be parsed safely."""


class ProviderConfigConflictError(ProviderConfigError):
    """Raised when managed settings were changed outside this application."""


class ProviderRestoreUnavailableError(ProviderConfigError):
    """Raised when the original provider settings cannot be recovered safely."""


class ProviderTransactionRecoveryError(ProviderConfigConflictError):
    """Raised when a durable provider transaction cannot be recovered safely."""


@dataclass(frozen=True)
class ProviderStatus:
    mode: str
    managed: bool
    conflict: bool
    external_backup_available: bool
    active_model: Optional[str] = None


@dataclass(frozen=True)
class ProviderSwitchResult:
    changed: bool
    mode: str
    active_model: Optional[str] = None


def resolve_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_optional_bytes(path: Path, limit: int, label: str) -> Optional[bytes]:
    if not path.exists():
        return None
    if not path.is_file():
        raise ProviderConfigError("{}不是普通文件。".format(label))
    try:
        if path.stat().st_size > limit:
            raise ProviderConfigError("{}过大，已拒绝读取。".format(label))
        with path.open("rb") as source:
            value = source.read(limit + 1)
        if len(value) > limit:
            raise ProviderConfigError("{}过大，已拒绝读取。".format(label))
        return value
    except ProviderConfigError:
        raise
    except OSError as exc:
        raise ProviderConfigError("无法读取{}：{}".format(label, exc)) from exc


def _decode_utf8(value: bytes, label: str) -> str:
    try:
        return value.decode("utf-8-sig")
    except UnicodeError as exc:
        raise InvalidCodexConfigError("{}不是有效的 UTF-8 文件。".format(label)) from exc


def _parse_config_bytes(value: Optional[bytes]):
    text = "" if value is None else _decode_utf8(value, "config.toml")
    try:
        return tomlkit.parse(text)
    except (ParseError, ValueError, TypeError) as exc:
        raise InvalidCodexConfigError("config.toml 格式无效：{}".format(exc)) from exc


def _normalize_value(value: Any) -> Any:
    if hasattr(value, "unwrap"):
        value = value.unwrap()
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _managed_fingerprint(document) -> str:
    values: Dict[str, Any] = {}
    for key in MANAGED_ROOT_KEYS:
        if key in document:
            values[key] = _normalize_value(document[key])
        else:
            values[key] = {"missing": True}

    deepseek_value: Any = {"missing": True}
    providers = document.get("model_providers")
    if providers is not None and not hasattr(providers, "items"):
        raise InvalidCodexConfigError("model_providers 必须是 TOML 配置表。")
    if providers is not None and DEEPSEEK_PROVIDER in providers:
        deepseek_value = _normalize_value(providers[DEEPSEEK_PROVIDER])
        if isinstance(deepseek_value, dict) and "experimental_bearer_token" in deepseek_value:
            token = str(deepseek_value["experimental_bearer_token"])
            deepseek_value["experimental_bearer_token"] = {
                "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()
            }
    values["model_providers.deepseek"] = deepseek_value
    serialized = json.dumps(
        values,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(serialized)


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
    """Replace only if the target still matches immediately before commit."""

    if replacement is None:
        if not _optional_bytes_match(path, expected, limit, label):
            raise ProviderConfigConflictError("{}已在写入准备期间被外部修改。".format(label))
        if expected is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            raise ProviderConfigConflictError("{}已在删除提交期间被外部修改。".format(label))
        return

    temporary = _prepare_atomic_write(path, replacement)
    try:
        if not _optional_bytes_match(path, expected, limit, label):
            raise ProviderConfigConflictError("{}已在写入准备期间被外部修改。".format(label))
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ATOMIC_TEMPORARY_PATTERN = re.compile(
    r"^\.codex-account-switcher\.(?P<target>.+)\.[0-9a-f]{32}\.tmp$"
)
_TRANSACTION_TARGETS = ("config", "models", "state")


class ProviderConfigManager:
    def __init__(
        self,
        codex_home: Path,
        state_directory: Path,
        models_asset_path: Path,
        protector=None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise ValueError("lock_timeout must be a positive finite number")
        self.codex_home = Path(codex_home).resolve()
        self.config_path = self.codex_home / "config.toml"
        self.models_path = self.codex_home / "models.json"
        self.official_backup_path = self.codex_home / "backup-deepseek" / "config.toml"
        self.transaction_path = self.codex_home / TRANSACTION_FILE_NAME
        self.state_directory = Path(state_directory).resolve()
        self.state_path = self.state_directory / STATE_FILE_NAME
        self.models_asset_path = Path(models_asset_path).resolve()
        self.protector = protector if protector is not None else WindowsDataProtector()
        self.lock_timeout = float(lock_timeout)

    @contextmanager
    def _operation_guard(self, timeout: Optional[float] = None) -> Iterator[None]:
        try:
            with codex_operation_guard(
                self.codex_home / "auth.json",
                self.lock_timeout if timeout is None else timeout,
            ):
                self._cleanup_orphaned_temporaries_locked()
                yield
        except AccountStoreError as exc:
            raise ProviderConfigError(str(exc)) from exc

    def _cleanup_orphaned_temporaries_locked(self) -> None:
        """Remove strict app-owned temp files only after acquiring the shared lock."""

        targets = (
            self.config_path,
            self.models_path,
            self.state_path,
            self.transaction_path,
        )
        for target in targets:
            parent = target.parent
            if not parent.exists():
                continue
            try:
                entries = tuple(parent.iterdir())
            except OSError as exc:
                raise ProviderConfigError(
                    "无法检查{}临时文件：{}".format(target.name, exc)
                ) from exc
            for entry in entries:
                match = _ATOMIC_TEMPORARY_PATTERN.fullmatch(entry.name)
                if match is None or match.group("target") != target.name:
                    continue
                try:
                    if entry.is_file() or entry.is_symlink():
                        entry.unlink()
                except OSError as exc:
                    raise ProviderConfigError(
                        "无法清理{}临时文件：{}".format(target.name, exc)
                    ) from exc

    def _read_config(self) -> Tuple[Optional[bytes], Any]:
        value = _read_optional_bytes(self.config_path, MAX_CONFIG_BYTES, "config.toml")
        return value, _parse_config_bytes(value)

    @staticmethod
    def _mode(document) -> str:
        provider = document.get("model_provider")
        if provider is not None and str(provider) == DEEPSEEK_PROVIDER:
            return "deepseek"
        return "openai"

    @staticmethod
    def _active_model(document, mode: str) -> Optional[str]:
        if mode != "deepseek":
            return None
        model = document.get("model")
        return str(model) if model is not None else None

    def _models_hash_matches(self, state: Dict[str, Any]) -> bool:
        current = _read_optional_bytes(self.models_path, MAX_MODELS_BYTES, "models.json")
        if current is None:
            return False
        return _sha256(current) == state.get("expected_models_sha256")

    def status(self) -> ProviderStatus:
        with self._operation_guard(timeout=min(self.lock_timeout, 0.25)):
            self._recover_pending_transaction_locked()
            return self._status_locked()

    def _status_locked(self) -> ProviderStatus:
        _value, document = self._read_config()
        mode = self._mode(document)
        state = self._load_state(required=False)
        conflict = False
        if mode == "deepseek" and state is not None:
            conflict = _managed_fingerprint(document) != state[
                "expected_fingerprint"
            ] or not self._models_hash_matches(state)
        external_backup_available = False
        if mode == "deepseek" and state is None:
            try:
                backup = _read_optional_bytes(
                    self.official_backup_path,
                    MAX_CONFIG_BYTES,
                    "DeepSeek 官方配置备份",
                )
                if backup is not None:
                    external_backup_available = self._mode(_parse_config_bytes(backup)) == "openai"
            except ProviderConfigError:
                external_backup_available = False
        return ProviderStatus(
            mode=mode,
            managed=mode == "deepseek" and state is not None,
            conflict=conflict,
            external_backup_available=external_backup_available,
            active_model=self._active_model(document, mode),
        )

    def managed_deepseek_api_key(self) -> Optional[str]:
        """Return the verified active token without exposing it through status objects."""

        with self._operation_guard():
            self._recover_pending_transaction_locked()
            _value, document = self._read_config()
            if self._mode(document) != "deepseek":
                return None
            state = self._load_state(required=False)
            if state is None:
                raise ProviderConfigConflictError(
                    "当前 DeepSeek 配置不是由本程序管理，不能自动同步 API Key。"
                )
            if _managed_fingerprint(document) != state[
                "expected_fingerprint"
            ] or not self._models_hash_matches(state):
                raise ProviderConfigConflictError("Codex 配置已在外部修改，不能自动同步 API Key。")
            providers = document.get("model_providers")
            if providers is None or not hasattr(providers, "items"):
                raise InvalidCodexConfigError("model_providers 必须是 TOML 配置表。")
            deepseek = providers.get(DEEPSEEK_PROVIDER)
            if deepseek is None or not hasattr(deepseek, "items"):
                raise InvalidCodexConfigError("DeepSeek 模型服务配置无效。")
            token = deepseek.get("experimental_bearer_token")
            try:
                return normalize_api_key(str(token) if token is not None else "")
            except (CredentialStoreError, ValueError) as exc:
                raise InvalidCodexConfigError("活动 DeepSeek API Key 无效。") from exc

    def _load_models_asset(self) -> bytes:
        value = _read_optional_bytes(
            self.models_asset_path,
            MAX_MODELS_BYTES,
            "DeepSeek 模型目录",
        )
        if value is None:
            raise ProviderConfigError("安装包缺少 DeepSeek 模型目录。")
        if _sha256(value) != OFFICIAL_MODELS_SHA256:
            raise ProviderConfigError("DeepSeek 模型目录校验失败。")
        self._validate_models(value)
        return value

    @staticmethod
    def _validate_models(value: bytes) -> None:
        try:
            payload = json.loads(_decode_utf8(value, "models.json"))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderConfigError("models.json 格式无效：{}".format(exc)) from exc
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ProviderConfigError("models.json 缺少模型列表。")
        slugs = {
            model.get("slug")
            for model in models
            if isinstance(model, dict) and isinstance(model.get("slug"), str)
        }
        missing = [slug for slug in SUPPORTED_DEEPSEEK_MODELS if slug not in slugs]
        if missing:
            raise ProviderConfigError("models.json 缺少 {}。".format("、".join(missing)))

    @staticmethod
    def _validate_json(value: bytes, label: str) -> None:
        try:
            payload = json.loads(_decode_utf8(value, label))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderRestoreUnavailableError("{}备份格式无效：{}".format(label, exc)) from exc
        if not isinstance(payload, dict):
            raise ProviderRestoreUnavailableError("{}备份的顶层必须是对象。".format(label))

    def _encode_state(self, state: Dict[str, Any]) -> bytes:
        serialized = json.dumps(
            state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            return self.protector.protect(serialized)
        except CredentialStoreError as exc:
            raise ProviderConfigError(str(exc)) from exc

    def _decode_state(
        self,
        protected: Optional[bytes],
        required: bool,
    ) -> Optional[Dict[str, Any]]:
        if protected is None:
            if required:
                raise ProviderRestoreUnavailableError("未找到可用的 OpenAI 配置备份。")
            return None
        try:
            serialized = self.protector.unprotect(protected)
            state = json.loads(serialized.decode("utf-8"))
        except (CredentialStoreError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderRestoreUnavailableError("配置恢复状态损坏或无法读取。") from exc
        required_fields = {
            "version",
            "baseline_config",
            "baseline_models",
            "expected_fingerprint",
            "expected_models_sha256",
            "source",
        }
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or not required_fields.issubset(state)
        ):
            raise ProviderRestoreUnavailableError("配置恢复状态格式不受支持。")
        return state

    def _read_state(
        self,
        required: bool,
    ) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
        protected = _read_optional_bytes(
            self.state_path,
            MAX_STATE_BYTES,
            "配置恢复状态",
        )
        return protected, self._decode_state(protected, required)

    def _load_state(self, required: bool) -> Optional[Dict[str, Any]]:
        _protected, state = self._read_state(required)
        return state

    @staticmethod
    def _encode_optional(value: Optional[bytes]) -> Optional[str]:
        if value is None:
            return None
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _decode_optional(value: Any, label: str) -> Optional[bytes]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProviderRestoreUnavailableError("{}备份格式无效。".format(label))
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise ProviderRestoreUnavailableError("{}备份损坏。".format(label)) from exc

    def _new_state(
        self,
        baseline_config: Optional[bytes],
        baseline_models: Optional[bytes],
        expected_document,
        expected_models: bytes,
        source: str,
    ) -> Dict[str, Any]:
        return {
            "version": 1,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "baseline_config": self._encode_optional(baseline_config),
            "baseline_models": self._encode_optional(baseline_models),
            "expected_fingerprint": _managed_fingerprint(expected_document),
            "expected_models_sha256": _sha256(expected_models),
            "source": source,
        }

    def _path_limit(self, path: Path) -> int:
        if path == self.models_path:
            return MAX_MODELS_BYTES
        if path == self.state_path:
            return MAX_STATE_BYTES
        if path == self.transaction_path:
            return MAX_TRANSACTION_BYTES
        return MAX_CONFIG_BYTES

    def _validate_replacement_size(self, path: Path, replacement: Optional[bytes]) -> None:
        if replacement is None:
            return
        limit = self._path_limit(path)
        if len(replacement) > limit:
            raise ProviderConfigError("{}过大，已拒绝写入。".format(path.name))

    def _transaction_target(self, path: Path) -> str:
        if path == self.config_path:
            return "config"
        if path == self.models_path:
            return "models"
        if path == self.state_path:
            return "state"
        if path == self.official_backup_path:
            return "official_backup"
        raise ProviderConfigError("事务包含不受支持的写入目标。")

    def _guard_path(self, target: str, state_path: Optional[str]) -> Path:
        if target == "official_backup":
            return self.official_backup_path
        return self._transaction_path(target, state_path)

    def _transaction_path(self, target: str, state_path: Optional[str]) -> Path:
        if target == "config":
            return self.config_path
        if target == "models":
            return self.models_path
        if target != "state" or not isinstance(state_path, str):
            raise ProviderTransactionRecoveryError("配置事务日志包含无效目标。")
        if not state_path or len(state_path) > 32767 or any(ord(char) < 32 for char in state_path):
            raise ProviderTransactionRecoveryError("配置事务日志的数据目录路径无效。")
        try:
            candidate = Path(state_path)
            valid_path = (
                candidate.is_absolute()
                and candidate.name == STATE_FILE_NAME
                and candidate.resolve() == self.state_path
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProviderTransactionRecoveryError("配置事务日志的数据目录路径无效。") from exc
        if not valid_path:
            raise ProviderTransactionRecoveryError(
                "配置事务日志属于另一份程序数据目录，已停止自动恢复。"
            )
        return self.state_path

    @staticmethod
    def _signature(value: Optional[bytes]) -> Dict[str, Any]:
        if value is None:
            return {"exists": False, "size": 0, "sha256": None}
        return {"exists": True, "size": len(value), "sha256": _sha256(value)}

    @staticmethod
    def _matches_signature(value: Optional[bytes], signature: Dict[str, Any]) -> bool:
        if value is None:
            return not signature["exists"]
        return (
            signature["exists"]
            and len(value) == signature["size"]
            and _sha256(value) == signature["sha256"]
        )

    def _encode_transaction(
        self,
        operation: str,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
    ) -> bytes:
        entries = []
        guards = []
        for path, before, after in changes:
            if before == after:
                guards.append(
                    {
                        "target": self._transaction_target(path),
                        "before": self._signature(before),
                    }
                )
                continue
            entries.append(
                {
                    "target": self._transaction_target(path),
                    "before": self._encode_optional(before),
                    "after": self._signature(after),
                }
            )
        if not entries:
            raise ProviderConfigError("配置事务没有需要写入的内容。")
        payload = {
            "version": TRANSACTION_VERSION,
            "transaction_id": uuid.uuid4().hex,
            "operation": operation,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "state_path": str(self.state_path),
            "changes": entries,
            "guards": guards,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            protected = self.protector.protect(serialized)
        except CredentialStoreError as exc:
            raise ProviderConfigError(str(exc)) from exc
        self._validate_replacement_size(self.transaction_path, protected)
        return protected

    def _decode_transaction(self, protected: bytes) -> Dict[str, Any]:
        try:
            serialized = self.protector.unprotect(protected)
            transaction = json.loads(serialized.decode("utf-8"))
        except (CredentialStoreError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderTransactionRecoveryError(
                "配置事务日志损坏或无法读取，已停止自动恢复。"
            ) from exc
        if not isinstance(transaction, dict):
            raise ProviderTransactionRecoveryError("配置事务日志格式不受支持。")
        if set(transaction) != {
            "version",
            "transaction_id",
            "operation",
            "created_at",
            "state_path",
            "changes",
            "guards",
        }:
            raise ProviderTransactionRecoveryError("配置事务日志字段无效。")
        if transaction.get("version") != TRANSACTION_VERSION:
            raise ProviderTransactionRecoveryError("配置事务日志版本不受支持。")
        if transaction.get("operation") not in {"activate", "restore"}:
            raise ProviderTransactionRecoveryError("配置事务日志操作类型无效。")
        transaction_id = transaction.get("transaction_id")
        if not isinstance(transaction_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            raise ProviderTransactionRecoveryError("配置事务日志标识无效。")
        created_at = transaction.get("created_at")
        try:
            created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
        except ValueError as exc:
            raise ProviderTransactionRecoveryError("配置事务日志时间无效。") from exc
        if created is None or created.tzinfo is None or len(created_at) > 128:
            raise ProviderTransactionRecoveryError("配置事务日志时间无效。")
        state_path = transaction.get("state_path")
        # Validate the originating data directory even if a malformed journal
        # tries to omit the state target entirely.
        self._transaction_path("state", state_path)
        changes = transaction.get("changes")
        if not isinstance(changes, list) or not changes or len(changes) > len(_TRANSACTION_TARGETS):
            raise ProviderTransactionRecoveryError("配置事务日志变更列表无效。")
        seen = set()
        decoded = []
        for entry in changes:
            if not isinstance(entry, dict) or set(entry) != {"target", "before", "after"}:
                raise ProviderTransactionRecoveryError("配置事务日志变更项无效。")
            target = entry.get("target")
            if target not in _TRANSACTION_TARGETS or target in seen:
                raise ProviderTransactionRecoveryError("配置事务日志包含重复或未知目标。")
            seen.add(target)
            path = self._transaction_path(target, state_path)
            try:
                before = self._decode_optional(entry.get("before"), path.name)
            except ProviderRestoreUnavailableError as exc:
                raise ProviderTransactionRecoveryError("配置事务日志原始内容无效。") from exc
            signature = entry.get("after")
            if not isinstance(signature, dict) or set(signature) != {
                "exists",
                "size",
                "sha256",
            }:
                raise ProviderTransactionRecoveryError("配置事务日志目标摘要无效。")
            exists = signature.get("exists")
            size = signature.get("size")
            digest = signature.get("sha256")
            if not isinstance(exists, bool) or not isinstance(size, int) or isinstance(size, bool):
                raise ProviderTransactionRecoveryError("配置事务日志目标摘要无效。")
            if size < 0 or size > self._path_limit(path):
                raise ProviderTransactionRecoveryError("配置事务日志目标大小无效。")
            if exists:
                if size < 0 or not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
                    raise ProviderTransactionRecoveryError("配置事务日志目标摘要无效。")
            elif size != 0 or digest is not None:
                raise ProviderTransactionRecoveryError("配置事务日志目标摘要无效。")
            if before is not None and len(before) > self._path_limit(path):
                raise ProviderTransactionRecoveryError("配置事务日志原始内容过大。")
            decoded.append((path, before, signature))
        transaction["decoded_changes"] = decoded
        guards = transaction.get("guards")
        if not isinstance(guards, list) or len(guards) > len(_TRANSACTION_TARGETS) + 1:
            raise ProviderTransactionRecoveryError("配置事务日志守卫列表无效。")
        decoded_guards = []
        guard_targets = set()
        for guard in guards:
            if not isinstance(guard, dict) or set(guard) != {"target", "before"}:
                raise ProviderTransactionRecoveryError("配置事务日志守卫无效。")
            target = guard.get("target")
            if (
                target not in set(_TRANSACTION_TARGETS) | {"official_backup"}
                or target in guard_targets
            ):
                raise ProviderTransactionRecoveryError("配置事务日志包含未知守卫。")
            guard_targets.add(target)
            if target in seen:
                raise ProviderTransactionRecoveryError("配置事务日志目标不能同时是变更和守卫。")
            guard_path = self._guard_path(target, state_path)
            signature = guard.get("before")
            if not isinstance(signature, dict) or set(signature) != {
                "exists",
                "size",
                "sha256",
            }:
                raise ProviderTransactionRecoveryError("配置事务日志守卫摘要无效。")
            exists = signature.get("exists")
            size = signature.get("size")
            digest = signature.get("sha256")
            if (
                not isinstance(exists, bool)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > self._path_limit(guard_path)
                or (
                    exists
                    and (not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest))
                )
                or (not exists and (size != 0 or digest is not None))
            ):
                raise ProviderTransactionRecoveryError("配置事务日志守卫摘要无效。")
            decoded_guards.append((guard_path, signature))
        if (seen | guard_targets) & set(_TRANSACTION_TARGETS) != set(_TRANSACTION_TARGETS):
            raise ProviderTransactionRecoveryError("配置事务日志缺少受管目标。")
        transaction["decoded_guards"] = decoded_guards
        return transaction

    def _read_transaction(self) -> Tuple[Optional[bytes], Optional[Dict[str, Any]]]:
        protected = _read_optional_bytes(
            self.transaction_path,
            MAX_TRANSACTION_BYTES,
            "配置事务日志",
        )
        if protected is None:
            return None, None
        return protected, self._decode_transaction(protected)

    def _recover_pending_transaction_locked(self, abandon_external: bool = False) -> str:
        protected, transaction = self._read_transaction()
        if transaction is None or protected is None:
            return "none"

        guard_errors = []
        guard_values = []
        for path, signature in transaction["decoded_guards"]:
            guard_value = _read_optional_bytes(path, self._path_limit(path), path.name)
            guard_values.append(guard_value)
            if not self._matches_signature(guard_value, signature):
                guard_errors.append("{}已被外部修改。".format(path.name))

        states = []
        current_values = []
        for path, before, after_signature in transaction["decoded_changes"]:
            current = _read_optional_bytes(path, self._path_limit(path), path.name)
            current_values.append(current)
            if current == before:
                states.append("before")
            elif self._matches_signature(current, after_signature):
                states.append("after")
            else:
                states.append("external")

        if all(state == "after" for state in states) and not guard_errors:
            _guarded_replace_optional(
                self.transaction_path,
                protected,
                None,
                MAX_TRANSACTION_BYTES,
                "配置事务日志",
            )
            return "committed"
        if all(state == "before" for state in states):
            _guarded_replace_optional(
                self.transaction_path,
                protected,
                None,
                MAX_TRANSACTION_BYTES,
                "配置事务日志",
            )
            return "rolled_back"

        recovery_errors = list(guard_errors)
        for (path, before, _after_signature), current, state in reversed(
            list(zip(transaction["decoded_changes"], current_values, states))
        ):
            if state == "external":
                recovery_errors.append("{}包含较新的外部内容，未覆盖。".format(path.name))
                continue
            if state == "before":
                continue
            try:
                _guarded_replace_optional(
                    path,
                    current,
                    before,
                    self._path_limit(path),
                    path.name,
                )
            except (OSError, ProviderConfigError) as exc:
                recovery_errors.append(str(exc))

        fully_restored = True
        external_stable = True
        for (path, before, _after_signature), original_current, state in zip(
            transaction["decoded_changes"], current_values, states
        ):
            try:
                current = _read_optional_bytes(path, self._path_limit(path), path.name)
                if state == "external":
                    if current != original_current:
                        external_stable = False
                elif current != before:
                    fully_restored = False
            except ProviderConfigError:
                fully_restored = False
        guards_stable = True
        for (path, _signature), original_current in zip(
            transaction["decoded_guards"], guard_values
        ):
            try:
                if (
                    _read_optional_bytes(path, self._path_limit(path), path.name)
                    != original_current
                ):
                    guards_stable = False
            except ProviderConfigError:
                guards_stable = False
        if (
            abandon_external
            and fully_restored
            and external_stable
            and guards_stable
            and (recovery_errors or guard_errors)
        ):
            _guarded_replace_optional(
                self.transaction_path,
                protected,
                None,
                MAX_TRANSACTION_BYTES,
                "配置事务日志",
            )
            return "abandoned"
        if recovery_errors or not fully_restored:
            detail = "；".join(recovery_errors) or "部分文件未能恢复到事务前状态。"
            raise ProviderTransactionRecoveryError(
                "检测到未完成的配置事务，但无法安全恢复：{}".format(detail)
            )
        _guarded_replace_optional(
            self.transaction_path,
            protected,
            None,
            MAX_TRANSACTION_BYTES,
            "配置事务日志",
        )
        return "rolled_back"

    def _transaction_is_current(self, expected: bytes) -> bool:
        return _optional_bytes_match(
            self.transaction_path,
            expected,
            MAX_TRANSACTION_BYTES,
            "配置事务日志",
        )

    def _all_replacements_match(
        self,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
    ) -> bool:
        for path, _expected, replacement in changes:
            if not _optional_bytes_match(
                path,
                replacement,
                self._path_limit(path),
                path.name,
            ):
                return False
        return True

    def _rollback_changes_without_journal(
        self,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
    ) -> str:
        """Compensate owned partial writes if the journal disappeared mid-commit."""

        snapshots = []
        has_external = False
        for path, before, replacement in changes:
            current = _read_optional_bytes(path, self._path_limit(path), path.name)
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
                    self._path_limit(path),
                    path.name,
                )
            except (OSError, ProviderConfigError) as exc:
                recovery_errors.append(str(exc))

        for path, before, _replacement, original_current, state in snapshots:
            try:
                current = _read_optional_bytes(path, self._path_limit(path), path.name)
            except ProviderConfigError as exc:
                recovery_errors.append(str(exc))
                continue
            if state == "external":
                if current != original_current:
                    recovery_errors.append("{}的外部内容再次发生变化。".format(path.name))
            elif current != before:
                recovery_errors.append("{}未能恢复到事务前状态。".format(path.name))

        if recovery_errors:
            raise ProviderTransactionRecoveryError(
                "配置事务日志已消失，且部分写入无法安全恢复：{}".format("；".join(recovery_errors))
            )
        return "abandoned" if has_external else "rolled_back"

    def _assert_unchanged(self, path: Path, expected: Optional[bytes]) -> None:
        if not _optional_bytes_match(path, expected, self._path_limit(path), path.name):
            raise ProviderConfigConflictError(
                "{}已在操作期间被外部修改。为避免覆盖，本次操作已取消。".format(path.name)
            )

    def _commit_paths(
        self,
        changes: Sequence[Tuple[Path, Optional[bytes], Optional[bytes]]],
        operation: str,
    ) -> None:
        for path, _expected, replacement in changes:
            self._validate_replacement_size(path, replacement)
        for path, expected, _replacement in changes:
            self._assert_unchanged(path, expected)

        effective_changes = [change for change in changes if change[1] != change[2]]
        if not effective_changes:
            return
        transaction_bytes = self._encode_transaction(operation, changes)
        _guarded_replace_optional(
            self.transaction_path,
            None,
            transaction_bytes,
            MAX_TRANSACTION_BYTES,
            "配置事务日志",
        )

        try:
            if not self._transaction_is_current(transaction_bytes):
                raise ProviderTransactionRecoveryError(
                    "配置事务日志已在提交前被外部修改，已停止写入。"
                )
            for path, expected, replacement in changes:
                if not self._transaction_is_current(transaction_bytes):
                    raise ProviderTransactionRecoveryError(
                        "配置事务日志已在提交期间被外部修改，已停止写入。"
                    )
                self._assert_unchanged(path, expected)
                if expected == replacement:
                    continue
                _guarded_replace_optional(
                    path,
                    expected,
                    replacement,
                    self._path_limit(path),
                    path.name,
                )
                if not _optional_bytes_match(
                    path,
                    replacement,
                    self._path_limit(path),
                    path.name,
                ):
                    raise ProviderConfigConflictError(
                        "{}在写入后再次发生变化。为避免覆盖，本次操作已停止。".format(path.name)
                    )
            # Recheck the whole document set after the last write. This catches an
            # external change to an earlier or no-op path while later paths were
            # being committed.
            for path, _expected, replacement in changes:
                if not _optional_bytes_match(
                    path,
                    replacement,
                    self._path_limit(path),
                    path.name,
                ):
                    raise ProviderConfigConflictError(
                        "{}在事务完成前再次发生变化。为避免覆盖，本次操作已停止。".format(path.name)
                    )
            _guarded_replace_optional(
                self.transaction_path,
                transaction_bytes,
                None,
                MAX_TRANSACTION_BYTES,
                "配置事务日志",
            )
        except (OSError, ProviderConfigError) as exc:
            try:
                recovery = self._recover_pending_transaction_locked(abandon_external=True)
            except ProviderConfigError as recovery_error:
                raise ProviderTransactionRecoveryError(
                    "配置写入失败，且未能完整恢复：{}".format(recovery_error)
                ) from exc
            if recovery == "committed" or (
                recovery == "none" and self._all_replacements_match(changes)
            ):
                return
            if recovery == "none":
                recovery = self._rollback_changes_without_journal(changes)
                if recovery == "committed":
                    return
            if recovery == "abandoned":
                raise ProviderConfigConflictError(
                    "检测到操作期间的外部修改；本程序已恢复自身写入并保留外部内容。"
                ) from exc
            if isinstance(exc, ProviderConfigError):
                raise
            raise ProviderConfigError("无法写入 Codex 配置：{}".format(exc)) from exc

    @staticmethod
    def _validate_model(model_slug: str) -> str:
        if not isinstance(model_slug, str) or model_slug not in SUPPORTED_DEEPSEEK_MODELS:
            raise ProviderConfigError("不支持的 DeepSeek 模型。")
        return model_slug

    def _build_deepseek_document(self, document, api_key: str, model_slug: str):
        updated = copy.deepcopy(document)
        for key in CONFLICTING_ROOT_KEYS:
            if key in updated:
                del updated[key]
        updated["model"] = model_slug
        for key, value in TARGET_VALUES.items():
            updated[key] = value
        updated["model_catalog_json"] = self.models_path.as_posix()

        providers = updated.get("model_providers")
        if providers is None:
            providers = tomlkit.table()
            updated["model_providers"] = providers
        elif not hasattr(providers, "items"):
            raise InvalidCodexConfigError("model_providers 必须是 TOML 配置表。")
        deepseek = tomlkit.table()
        deepseek.add("name", DEEPSEEK_PROVIDER)
        deepseek.add("base_url", DEEPSEEK_BASE_URL)
        deepseek.add("wire_api", "responses")
        deepseek.add("experimental_bearer_token", api_key)
        providers[DEEPSEEK_PROVIDER] = deepseek
        rendered = tomlkit.dumps(updated)
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"
        encoded = rendered.encode("utf-8")
        _parse_config_bytes(encoded)
        return updated, encoded

    def _ensure_external_state(
        self,
        current_document,
        current_models: bytes,
    ) -> Tuple[Dict[str, Any], bytes]:
        backup = _read_optional_bytes(
            self.official_backup_path,
            MAX_CONFIG_BYTES,
            "DeepSeek 官方配置备份",
        )
        if backup is None:
            raise ProviderRestoreUnavailableError(
                "当前 DeepSeek 配置不是由本程序管理，且未找到可用的 OpenAI 配置备份。"
            )
        try:
            backup_document = _parse_config_bytes(backup)
        except InvalidCodexConfigError as exc:
            raise ProviderRestoreUnavailableError(
                "DeepSeek 官方配置备份格式无效，不能作为 OpenAI 恢复基线。"
            ) from exc
        if self._mode(backup_document) == "deepseek":
            raise ProviderRestoreUnavailableError(
                "DeepSeek 官方配置备份仍指向 DeepSeek，不能作为 OpenAI 恢复基线。"
            )
        state = self._new_state(
            baseline_config=backup,
            baseline_models=None,
            expected_document=current_document,
            expected_models=current_models,
            source="official-backup",
        )
        return state, backup

    def activate_deepseek(
        self,
        api_key: str,
        model_slug: str = DEFAULT_DEEPSEEK_MODEL,
    ) -> ProviderSwitchResult:
        with self._operation_guard():
            self._recover_pending_transaction_locked()
            return self._activate_deepseek_locked(api_key, model_slug)

    def _activate_deepseek_locked(
        self,
        api_key: str,
        model_slug: str,
    ) -> ProviderSwitchResult:
        model_slug = self._validate_model(model_slug)
        normalized_key = normalize_api_key(api_key)
        current_config, current_document = self._read_config()
        current_models = _read_optional_bytes(
            self.models_path,
            MAX_MODELS_BYTES,
            "models.json",
        )
        models_asset = self._load_models_asset()
        previous_state_bytes, state = self._read_state(required=False)
        current_mode = self._mode(current_document)
        external_backup_guard = None

        if state is not None and current_mode != "deepseek":
            state = None
        if state is not None:
            if (
                _managed_fingerprint(current_document) != state["expected_fingerprint"]
                or current_models is None
                or _sha256(current_models) != state.get("expected_models_sha256")
            ):
                raise ProviderConfigConflictError(
                    "Codex 配置已在外部修改。为避免覆盖，本次操作已取消。"
                )
        elif current_mode == "deepseek":
            if current_models is None:
                raise ProviderRestoreUnavailableError("当前 DeepSeek 配置缺少 models.json。")
            state, external_backup_guard = self._ensure_external_state(
                current_document,
                current_models,
            )

        updated_document, updated_config = self._build_deepseek_document(
            current_document,
            normalized_key,
            model_slug,
        )
        if state is None:
            state = self._new_state(
                baseline_config=current_config,
                baseline_models=current_models,
                expected_document=updated_document,
                expected_models=models_asset,
                source="codex-switcher",
            )
        else:
            state = dict(state)
            state["expected_fingerprint"] = _managed_fingerprint(updated_document)
            state["expected_models_sha256"] = _sha256(models_asset)

        changed = current_config != updated_config or current_models != models_asset
        state_changed = previous_state_bytes is None
        if previous_state_bytes is not None:
            previous_state = self._decode_state(previous_state_bytes, required=True)
            state_changed = previous_state != state
        updated_state_bytes = self._encode_state(state) if state_changed else previous_state_bytes
        changes = []
        if external_backup_guard is not None:
            changes.append(
                (
                    self.official_backup_path,
                    external_backup_guard,
                    external_backup_guard,
                )
            )
        changes.extend(
            (
                (self.state_path, previous_state_bytes, updated_state_bytes),
                (self.models_path, current_models, models_asset),
                (self.config_path, current_config, updated_config),
            )
        )
        self._commit_paths(changes, "activate")
        return ProviderSwitchResult(
            changed=changed,
            mode="deepseek",
            active_model=model_slug,
        )

    def _restore_managed_fields(self, current_document, baseline_document):
        restored = copy.deepcopy(current_document)
        for key in MANAGED_ROOT_KEYS:
            if key in baseline_document:
                restored[key] = copy.deepcopy(baseline_document[key])
            elif key in restored:
                del restored[key]

        current_providers = restored.get("model_providers")
        baseline_providers = baseline_document.get("model_providers")
        if current_providers is not None and not hasattr(current_providers, "items"):
            raise InvalidCodexConfigError("model_providers 必须是 TOML 配置表。")
        if baseline_providers is not None and not hasattr(baseline_providers, "items"):
            raise ProviderRestoreUnavailableError("原始配置中的 model_providers 不是 TOML 配置表。")
        baseline_deepseek = None
        if baseline_providers is not None and DEEPSEEK_PROVIDER in baseline_providers:
            baseline_deepseek = baseline_providers[DEEPSEEK_PROVIDER]

        if baseline_deepseek is not None:
            if current_providers is None:
                current_providers = tomlkit.table()
                restored["model_providers"] = current_providers
            current_providers[DEEPSEEK_PROVIDER] = copy.deepcopy(baseline_deepseek)
        elif current_providers is not None and DEEPSEEK_PROVIDER in current_providers:
            del current_providers[DEEPSEEK_PROVIDER]
            if not list(current_providers.keys()) and "model_providers" not in baseline_document:
                del restored["model_providers"]
        return restored

    def restore_openai(self) -> ProviderSwitchResult:
        with self._operation_guard():
            self._recover_pending_transaction_locked()
            return self._restore_openai_locked()

    def _restore_openai_locked(self) -> ProviderSwitchResult:
        current_config, current_document = self._read_config()
        if self._mode(current_document) != "deepseek":
            return ProviderSwitchResult(changed=False, mode="openai", active_model=None)
        current_models = _read_optional_bytes(
            self.models_path,
            MAX_MODELS_BYTES,
            "models.json",
        )
        if current_models is None:
            raise ProviderConfigConflictError("当前 DeepSeek 配置缺少 models.json。")

        current_state_bytes, state = self._read_state(required=False)
        external_backup_guard = None
        if state is None:
            state, external_backup_guard = self._ensure_external_state(
                current_document,
                current_models,
            )
        if (
            _managed_fingerprint(current_document) != state["expected_fingerprint"]
            or _sha256(current_models) != state["expected_models_sha256"]
        ):
            raise ProviderConfigConflictError(
                "Codex 配置已在外部修改。为避免覆盖，本次操作已取消。"
            )

        baseline_config = self._decode_optional(state["baseline_config"], "config.toml")
        baseline_models = self._decode_optional(state["baseline_models"], "models.json")
        baseline_document = _parse_config_bytes(baseline_config)
        restored_document = self._restore_managed_fields(
            current_document,
            baseline_document,
        )
        rendered = tomlkit.dumps(restored_document)
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"
        restored_config: Optional[bytes] = rendered.encode("utf-8")
        if not rendered.strip() and baseline_config is None:
            restored_config = None
        if restored_config is not None:
            _parse_config_bytes(restored_config)
        # The encrypted state is an exact snapshot of whatever models.json the
        # user had before activation. Restoring it must not impose a stricter
        # JSON contract than activation did, otherwise arbitrary pre-existing
        # bytes could be backed up successfully but become impossible to restore.

        changes = []
        if external_backup_guard is not None:
            changes.append(
                (
                    self.official_backup_path,
                    external_backup_guard,
                    external_backup_guard,
                )
            )
        changes.extend(
            (
                (self.config_path, current_config, restored_config),
                (self.models_path, current_models, baseline_models),
                (self.state_path, current_state_bytes, None),
            )
        )
        self._commit_paths(changes, "restore")
        return ProviderSwitchResult(changed=True, mode="openai", active_model=None)
