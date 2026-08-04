# 第三方资料说明

## OpenAI Codex 认证刷新

ChatGPT 账号自动同步设计参考 OpenAI 官方 Codex 文档：

- 认证与本地缓存：https://learn.chatgpt.com/docs/auth
- 持久化 Codex 刷新后的 `auth.json`：https://learn.chatgpt.com/docs/auth/ci-cd-auth
- App Server 认证接口：https://learn.chatgpt.com/docs/app-server

官方客户端负责在使用过程中刷新令牌；使用文件凭据存储时，刷新结果会写回 `auth.json`。本项目不复制 OAuth 实现、不调用私有令牌端点，也不把任何观察到的客户端刷新阈值硬编码为协议；只在本地账号切换覆盖文件前保存客户端已经写回的最新认证。

## DeepSeek Codex 模型目录

`assets/deepseek_models.json` 来自 DeepSeek 官方 Codex 接入脚本中提供的模型元数据，用于向 Codex 声明 DeepSeek V4 模型能力。

- 官方文档：https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex/
- 官方 Windows 脚本：https://cdn.deepseek.com/api-docs/codex-deepseek-setup-en.ps1
- 固定 SHA-256：`B459A6E438D6A9939D01FD0DBB4693F165ED732BC8E4FD58D7145D9D94BD49A4`
- 项目接入日期：2026-07-31

应用在运行时校验该文件哈希，防止模型目录被意外替换。该文件包含 `deepseek-v4-flash` 与 `deepseek-v4-pro` 的元数据，但 v1.4.0 界面只开放已经完成本项目配置、回滚、界面与发布物验收的 `deepseek-v4-flash`；模型元数据不构成本版本的支持承诺。

## Python 依赖

项目依赖 PyInstaller、Pillow、tomlkit 和 Ruff。各依赖的版权和许可条款以其上游项目为准；固定版本见 `requirements*.txt`。本项目不修改或内嵌 Ruff，Ruff 仅用于开发和 CI 检查。
