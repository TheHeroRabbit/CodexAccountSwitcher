"""Fail closed when repository contents are unsafe or not release-ready."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FILES = (
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "app.py",
    "assets/app_icon_source.png",
    "assets/deepseek_models.json",
    "build.ps1",
    "docs/VERIFICATION.md",
    "release.ps1",
    "requirements.txt",
    "requirements-build.txt",
    "requirements-dev.txt",
    "setup.ps1",
    "version_info.txt",
)
EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "_local_artifacts",
    "artifacts",
    "build",
    "dist",
    "release",
    "venv",
    "账号文件",
}
FORBIDDEN_DIRECTORY_NAMES = {
    "__pycache__",
    "_local_artifacts",
    "artifacts",
    "backup-deepseek",
    "build",
    "dist",
    "release",
    "venv",
    ".venv",
    "切换备份",
    "更新备份",
    "账号文件",
}
FORBIDDEN_FILE_NAMES = {
    ".env",
    "auth.json",
    "config.toml",
    "profiles.json",
    "profiles.json.bak",
    "provider_state.dat",
    "sha256.txt",
    "验收报告.txt",
}
FORBIDDEN_FILE_PATTERNS = (
    "auth_*.json",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.pyc",
    "*.spec",
)
ROOT_GENERATED_IMAGE_PATTERNS = (
    "dialog_*.png",
    "embedded_icon_*.png",
    "notice_*.png",
    "ui_*.png",
)
TEXT_SUFFIXES = {
    ".cfg",
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("API key", re.compile(rb"sk-[A-Za-z0-9_-]{16,}")),
    ("Bearer credential", re.compile(rb"Bearer[ \t]+[A-Za-z0-9._-]{20,}", re.I)),
    ("GitHub token", re.compile(rb"gh[oprsu]_[A-Za-z0-9]{20,}")),
    ("private key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
ABSOLUTE_ENVIRONMENT_PATTERN = re.compile(
    rb"(?:^|[^A-Za-z0-9])(?:[A-Za-z]:[\\/])venv(?:[\\/]|$)",
    re.I,
)
OFFICIAL_MODELS_SHA256 = "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"


def _repository_relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _git_tracked_files() -> Sequence[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        return ()
    paths = []
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="strict")
        paths.append(PROJECT_ROOT / Path(relative))
    return tuple(paths)


def _fallback_source_files() -> Sequence[Path]:
    paths = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_ROOT)
        if any(part.casefold() in EXCLUDED_DIRECTORY_NAMES for part in relative.parts[:-1]):
            continue
        if _path_is_ignored_fallback(relative):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def _path_is_ignored_fallback(relative: Path) -> bool:
    name = relative.name
    if relative.parent == Path("."):
        if name in {"SHA256.txt", "验收报告.txt"}:
            return True
        if any(fnmatch.fnmatch(name, pattern) for pattern in ROOT_GENERATED_IMAGE_PATTERNS):
            return True
    if len(relative.parts) >= 2 and relative.parts[0] == "assets":
        if fnmatch.fnmatch(name, "app_icon_source_v*.png"):
            return True
    return False


def discover_repository_files() -> Sequence[Path]:
    tracked = _git_tracked_files()
    return tracked if tracked else _fallback_source_files()


def validate_path(relative: Path) -> List[str]:
    errors = []
    folded_parts = [part.casefold() for part in relative.parts]
    if any(part in FORBIDDEN_DIRECTORY_NAMES for part in folded_parts[:-1]):
        errors.append("forbidden generated or runtime directory")

    folded_name = relative.name.casefold()
    if folded_name in FORBIDDEN_FILE_NAMES:
        errors.append("forbidden runtime or generated file")
    if folded_name.startswith(".env."):
        errors.append("environment file must not be tracked")
    if any(fnmatch.fnmatch(folded_name, pattern) for pattern in FORBIDDEN_FILE_PATTERNS):
        errors.append("forbidden credential or generated filename")
    if relative.parent == Path(".") and any(
        fnmatch.fnmatch(relative.name, pattern) for pattern in ROOT_GENERATED_IMAGE_PATTERNS
    ):
        errors.append("generated visual artifact belongs outside the repository root")
    return errors


def is_text_path(path: Path) -> bool:
    return path.suffix.casefold() in TEXT_SUFFIXES or path.name in {
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
    }


def validate_text(path: Path, value: bytes) -> List[str]:
    errors = []
    if value.startswith(b"\xef\xbb\xbf"):
        errors.append("UTF-8 BOM is not allowed")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return ["text file is not valid UTF-8"]
    if "\r" in text:
        errors.append("line endings must be LF")
    if text and not text.endswith("\n"):
        errors.append("text file must end with a newline")
    if any(line.endswith((" ", "\t")) for line in text.splitlines()):
        errors.append("trailing whitespace is not allowed")
    if path.suffix.casefold() == ".ps1" and any(byte > 0x7F for byte in value):
        errors.append("PowerShell scripts must remain ASCII-compatible with Windows PowerShell 5.1")
    if ABSOLUTE_ENVIRONMENT_PATTERN.search(value):
        errors.append("hard-coded absolute virtual environment path detected")
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(value):
            errors.append("possible {} detected".format(label))
    return errors


def validate_models_asset() -> List[str]:
    errors = []
    path = PROJECT_ROOT / "assets" / "deepseek_models.json"
    try:
        value = path.read_bytes()
    except OSError as exc:
        return ["unable to read assets/deepseek_models.json: {}".format(exc)]
    digest = hashlib.sha256(value).hexdigest()
    if digest != OFFICIAL_MODELS_SHA256:
        errors.append("assets/deepseek_models.json hash does not match the pinned official asset")
    try:
        payload = json.loads(value.decode("utf-8"))
        slugs = {item.get("slug") for item in payload.get("models", []) if isinstance(item, dict)}
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        errors.append("assets/deepseek_models.json is invalid: {}".format(exc))
    else:
        required = {"deepseek-v4-flash", "deepseek-v4-pro"}
        if not required.issubset(slugs):
            errors.append("assets/deepseek_models.json is missing required model metadata")
    return errors


def validate_repository(paths: Iterable[Path]) -> List[str]:
    errors = []
    relative_paths = set()
    for path in paths:
        try:
            relative = path.relative_to(PROJECT_ROOT)
        except ValueError:
            errors.append("path escaped repository root: {}".format(path))
            continue
        relative_paths.add(relative.as_posix())
        for error in validate_path(relative):
            errors.append("{}: {}".format(relative.as_posix(), error))
        if not path.is_file():
            errors.append("{}: tracked path is missing or not a file".format(relative.as_posix()))
            continue
        try:
            value = path.read_bytes()
        except OSError as exc:
            errors.append("{}: unable to read file: {}".format(relative.as_posix(), exc))
            continue
        if len(value) > 2 * 1024 * 1024:
            errors.append("{}: tracked file exceeds 2 MiB".format(relative.as_posix()))
        if is_text_path(path):
            for error in validate_text(path, value):
                errors.append("{}: {}".format(relative.as_posix(), error))

    for expected in EXPECTED_FILES:
        if expected not in relative_paths:
            errors.append("required repository file is missing: {}".format(expected))
    errors.extend(validate_models_asset())
    return errors


def main() -> int:
    paths = discover_repository_files()
    errors = validate_repository(paths)
    if errors:
        print("Repository check failed:", file=sys.stderr)
        for error in errors:
            print("- {}".format(error), file=sys.stderr)
        return 1
    print("Repository check passed: {} source files inspected.".format(len(paths)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
