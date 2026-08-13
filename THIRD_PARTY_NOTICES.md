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
- 模型目录接入日期：2026-07-31
- V4 Pro 官方 Codex 支持复核日期：2026-08-13

截至复核日期，DeepSeek 官方文档明确提供 `deepseek-v4-flash` 与 `deepseek-v4-pro` 的原生 Codex 接入，两者均要求 Codex 0.144.0 或更高版本。本项目只使用这两个精确 slug；“存在模型元数据”与“官方文档已开放支持”分别核对，不以目录中出现名称代替官方支持声明。

应用在运行时校验该文件哈希，防止模型目录被意外替换。v1.5.0 的项目侧支持范围包含两个模型的配置、同服务切换、冲突检测和失败回滚；正式发布前必须按发布清单完成代码、界面与成品验收。若官方文档、脚本或模型目录后续变化，必须重新核对来源并重新验收，不能由旧快照推断未来兼容性。

## Python 依赖

项目依赖 PyInstaller、Pillow、tomlkit 和 Ruff。各依赖的版权和许可条款以其上游项目为准；固定版本见 `requirements*.txt`。本项目不修改或内嵌 Ruff，Ruff 仅用于开发和 CI 检查。
