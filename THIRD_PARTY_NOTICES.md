# 第三方资料说明

## DeepSeek Codex 模型目录

`assets/deepseek_models.json` 来自 DeepSeek 官方 Codex 接入脚本中提供的模型元数据，用于向 Codex 声明 DeepSeek V4 模型能力。

- 官方文档：https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex/
- 官方 Windows 脚本：https://cdn.deepseek.com/api-docs/codex-deepseek-setup-en.ps1
- 固定 SHA-256：`B459A6E438D6A9939D01FD0DBB4693F165ED732BC8E4FD58D7145D9D94BD49A4`
- 项目接入日期：2026-07-31

应用在运行时校验该文件哈希，防止模型目录被意外替换。该文件包含 `deepseek-v4-flash` 与 `deepseek-v4-pro` 的元数据，但界面只开放 DeepSeek 官方当前明确支持接入 Codex 的模型。

## Python 依赖

项目依赖 PyInstaller、Pillow、tomlkit 和 Ruff。各依赖的版权和许可条款以其上游项目为准；固定版本见 `requirements*.txt`。本项目不修改或内嵌 Ruff，Ruff 仅用于开发和 CI 检查。
