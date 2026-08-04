"""Transactional Codex provider configuration with conflict-safe restoration."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import tomlkit
from tomlkit.exceptions import ParseError

from credential_store import CredentialStoreError, WindowsDataProtector, normalize_api_key

DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
STATE_FILE_NAME = "provider_state.dat"
MAX_CONFIG_BYTES = 5 * 1024 * 1024
MAX_MODELS_BYTES = 5 * 1024 * 1024
MAX_STATE_BYTES = 12 * 1024 * 1024
OFFICIAL_MODELS_SHA256 = "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"
OFFICIAL_SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex"

TARGET_VALUES = {
    "model": DEEPSEEK_MODEL,
    "model_provider": DEEPSEEK_PROVIDER,
    "preferred_auth_method": "apikey",
    "forced_login_method": "api",
    "model_reasoning_effort": "high",
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

MANAGED_ROOT_KEYS = tuple(TARGET_VALUES) + ("model_catalog_json",) + CONFLICTING_ROOT_KEYS


class ProviderConfigError(RuntimeError):
    """Base error for model-provider configuration operations."""


class InvalidCodexConfigError(ProviderConfigError):
    """Raised when an existing Codex configuration cannot be parsed safely."""


class ProviderConfigConflictError(ProviderConfigError):
    """Raised when managed settings were changed outside this application."""


class ProviderRestoreUnavailableError(ProviderConfigError):
    """Raised when the original provider settings cannot be recovered safely."""


@dataclass(frozen=True)
class ProviderStatus:
    mode: str
    managed: bool
    conflict: bool
    external_backup_available: bool


@dataclass(frozen=True)
class ProviderSwitchResult:
    changed: bool
    mode: str


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
        return path.read_bytes()
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


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ".{}.{}.tmp".format(path.name, uuid.uuid4().hex)
    try:
        with temporary.open("wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _replace_optional(path: Path, value: Optional[bytes]) -> None:
    if value is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
    else:
        _atomic_write_bytes(path, value)


class ProviderConfigManager:
    def __init__(
        self,
        codex_home: Path,
        state_directory: Path,
        models_asset_path: Path,
        protector=None,
    ) -> None:
        self.codex_home = Path(codex_home).resolve()
        self.config_path = self.codex_home / "config.toml"
        self.models_path = self.codex_home / "models.json"
        self.official_backup_path = self.codex_home / "backup-deepseek" / "config.toml"
        self.state_directory = Path(state_directory).resolve()
        self.state_path = self.state_directory / STATE_FILE_NAME
        self.models_asset_path = Path(models_asset_path).resolve()
        self.protector = protector if protector is not None else WindowsDataProtector()

    def _read_config(self) -> Tuple[Optional[bytes], Any]:
        value = _read_optional_bytes(self.config_path, MAX_CONFIG_BYTES, "config.toml")
        return value, _parse_config_bytes(value)

    @staticmethod
    def _mode(document) -> str:
        provider = document.get("model_provider")
        if provider is not None and str(provider) == DEEPSEEK_PROVIDER:
            return "deepseek"
        return "openai"

    def _models_hash_matches(self, state: Dict[str, Any]) -> bool:
        current = _read_optional_bytes(self.models_path, MAX_MODELS_BYTES, "models.json")
        if current is None:
            return False
        return _sha256(current) == state.get("expected_models_sha256")

    def status(self) -> ProviderStatus:
        _value, document = self._read_config()
        mode = self._mode(document)
        state = self._load_state(required=False)
        conflict = False
        if mode == "deepseek" and state is not None:
            conflict = _managed_fingerprint(document) != state[
                "expected_fingerprint"
            ] or not self._models_hash_matches(state)
        return ProviderStatus(
            mode=mode,
            managed=state is not None,
            conflict=conflict,
            external_backup_available=(
                mode == "deepseek" and state is None and self.official_backup_path.is_file()
            ),
        )

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
        if DEEPSEEK_MODEL not in slugs:
            raise ProviderConfigError("models.json 缺少 {}。".format(DEEPSEEK_MODEL))

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

    def _write_state(self, state: Dict[str, Any]) -> None:
        try:
            protected = self._encode_state(state)
            _atomic_write_bytes(self.state_path, protected)
        except ProviderConfigError:
            raise
        except OSError as exc:
            raise ProviderConfigError("无法保存配置恢复状态：{}".format(exc)) from exc

    def _load_state(self, required: bool) -> Optional[Dict[str, Any]]:
        protected = _read_optional_bytes(
            self.state_path,
            MAX_STATE_BYTES,
            "配置恢复状态",
        )
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

    def _commit_paths(self, changes: Sequence[Tuple[Path, Optional[bytes]]]) -> None:
        originals = [
            (
                path,
                _read_optional_bytes(
                    path,
                    MAX_MODELS_BYTES if path == self.models_path else MAX_CONFIG_BYTES,
                    path.name,
                ),
            )
            for path, _value in changes
        ]
        applied = []
        try:
            for path, value in changes:
                _replace_optional(path, value)
                applied.append(path)
        except OSError as exc:
            restore_errors = []
            for path, original in reversed(originals):
                if path not in applied:
                    continue
                try:
                    _replace_optional(path, original)
                except OSError as restore_error:
                    restore_errors.append(str(restore_error))
            if restore_errors:
                raise ProviderConfigError(
                    "配置写入失败，且未能完整恢复：{}".format("；".join(restore_errors))
                ) from exc
            raise ProviderConfigError("无法写入 Codex 配置：{}".format(exc)) from exc

    def _build_deepseek_document(self, document, api_key: str):
        updated = copy.deepcopy(document)
        for key in CONFLICTING_ROOT_KEYS:
            if key in updated:
                del updated[key]
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
    ) -> Dict[str, Any]:
        backup = _read_optional_bytes(
            self.official_backup_path,
            MAX_CONFIG_BYTES,
            "DeepSeek 官方配置备份",
        )
        if backup is None:
            raise ProviderRestoreUnavailableError(
                "当前 DeepSeek 配置不是由本程序管理，且未找到可用的 OpenAI 配置备份。"
            )
        _parse_config_bytes(backup)
        state = self._new_state(
            baseline_config=backup,
            baseline_models=None,
            expected_document=current_document,
            expected_models=current_models,
            source="official-backup",
        )
        self._write_state(state)
        return state

    def activate_deepseek(self, api_key: str) -> ProviderSwitchResult:
        normalized_key = normalize_api_key(api_key)
        current_config, current_document = self._read_config()
        current_models = _read_optional_bytes(
            self.models_path,
            MAX_MODELS_BYTES,
            "models.json",
        )
        models_asset = self._load_models_asset()
        state = self._load_state(required=False)
        current_mode = self._mode(current_document)

        if state is not None and current_mode != "deepseek":
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ProviderConfigError("无法清理过期的配置恢复状态：{}".format(exc)) from exc
            state = None
        if state is not None:
            if _managed_fingerprint(current_document) != state[
                "expected_fingerprint"
            ] or not self._models_hash_matches(state):
                raise ProviderConfigConflictError(
                    "Codex 配置已在外部修改。为避免覆盖，本次操作已取消。"
                )
        elif current_mode == "deepseek":
            if current_models is None:
                raise ProviderRestoreUnavailableError("当前 DeepSeek 配置缺少 models.json。")
            state = self._ensure_external_state(current_document, current_models)

        updated_document, updated_config = self._build_deepseek_document(
            current_document,
            normalized_key,
        )
        previous_state_bytes = _read_optional_bytes(
            self.state_path,
            MAX_STATE_BYTES,
            "配置恢复状态",
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
        try:
            self._write_state(state)
            self._commit_paths(
                (
                    (self.models_path, models_asset),
                    (self.config_path, updated_config),
                )
            )
        except Exception:
            try:
                _replace_optional(self.state_path, previous_state_bytes)
            except OSError:
                pass
            raise
        return ProviderSwitchResult(changed=changed, mode="deepseek")

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
        current_config, current_document = self._read_config()
        if self._mode(current_document) != "deepseek":
            return ProviderSwitchResult(changed=False, mode="openai")
        current_models = _read_optional_bytes(
            self.models_path,
            MAX_MODELS_BYTES,
            "models.json",
        )
        if current_models is None:
            raise ProviderConfigConflictError("当前 DeepSeek 配置缺少 models.json。")

        state = self._load_state(required=False)
        if state is None:
            state = self._ensure_external_state(current_document, current_models)
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
        if baseline_models is not None:
            self._validate_json(baseline_models, "models.json")

        self._commit_paths(
            (
                (self.config_path, restored_config),
                (self.models_path, baseline_models),
            )
        )
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A stale state is harmless while OpenAI is active and is cleaned before
            # the next activation. The provider change itself has already committed.
            pass
        return ProviderSwitchResult(changed=True, mode="openai")
