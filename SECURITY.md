# 安全策略

Codex 切换器会处理本机认证文件和模型服务密钥。安全问题应优先私密报告，不要在公开 Issue 中提交凭据、配置文件、备份或可复现的真实账号数据。

## 支持范围

当前维护版本为 `1.3.x`。安全修复默认基于最新源码提供；旧版可执行文件不保证单独回补。

## 报告安全问题

请使用 GitHub 仓库的 **Security → Report a vulnerability** 私密安全公告功能。报告中可以包含：

- 受影响版本与 Windows 版本。
- 最小化复现步骤。
- 预期影响和攻击前提。
- 已去除凭据、用户名和本机路径的日志或截图。

不要提交以下内容：

- `%USERPROFILE%\.codex\auth.json` 或任何 `auth_*.json`。
- `profiles.json`、`provider_state.dat`、`config.toml` 或备份目录。
- DeepSeek/OpenAI API Key、Bearer Token、私钥或 Windows 凭据导出。

## 本机数据边界

- 账号认证副本与备份位于 EXE 同级“账号文件”目录。
- DeepSeek API Key 的持久副本位于 Windows 凭据管理器。
- DeepSeek 模式激活期间，API Key 会按 Codex 配置要求写入本机 `config.toml`；切回 OpenAI 时移除受管字段。
- `provider_state.dat` 使用当前 Windows 用户的 DPAPI 保护，不能作为跨设备备份。

如怀疑凭据已泄露，请立即在对应服务撤销或轮换密钥，并移除受影响的本机认证副本。
