# 安全策略

Codex 切换器会处理本机认证文件和模型服务密钥。安全问题应优先私密报告，不要在公开 Issue 中提交凭据、配置文件、备份或可复现的真实账号数据。

## 支持范围

当前维护版本为 `1.4.x`。安全修复默认基于最新源码提供；旧版可执行文件不保证单独回补。

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
- ChatGPT 账号切换前，程序只在 Codex 已停止、账号身份唯一匹配且原副本校验通过时自动保存刷新后的 `auth.json`；保存使用预期旧哈希比较更新，身份值只用于内存中的不可逆指纹比较。
- 账号存储由独立本机锁串行化，完整账号切换与模型服务切换共用同一个 Codex 操作锁；源文件先复制到临时文件并校验预期哈希，备份后会复检原文件，任何失败恢复或后续回滚前也会确认目标仍是本次写入的版本。锁文件不包含凭据，但不要在程序运行时删除。
- DeepSeek API Key 的持久副本位于 Windows 凭据管理器。
- DeepSeek 模式激活期间，API Key 会按 Codex 配置要求写入本机 `config.toml`；切回 OpenAI 时移除受管字段。
- `provider_state.dat` 使用当前 Windows 用户的 DPAPI 保护，不能作为跨设备备份。

如怀疑凭据已泄露，请立即在对应服务撤销或轮换密钥，并移除受影响的本机认证副本。
