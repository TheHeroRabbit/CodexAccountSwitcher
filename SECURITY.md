# 安全策略

Codex 切换器会处理本机认证文件和模型服务密钥。安全问题应优先私密报告，不要在公开 Issue 中提交凭据、配置文件、备份或可复现的真实账号数据。

## 支持范围

当前维护版本为 `1.5.x`。安全修复默认基于最新源码提供；旧版可执行文件不保证单独回补。

## 报告安全问题

请使用 GitHub 仓库的 **Security → Report a vulnerability** 私密安全公告功能。报告中可以包含：

- 受影响版本与 Windows 版本。
- 最小化复现步骤。
- 预期影响和攻击前提。
- 已去除凭据、用户名和本机路径的日志或截图。

不要提交以下内容：

- `%USERPROFILE%\.codex\auth.json` 或任何 `auth_*.json`。
- `profiles.json`、`provider_state.dat`、`.codex-account-switcher-provider-transaction.dat`、`.codex-account-switcher-account-transaction.dat`、`config.toml` 或备份目录。
- DeepSeek/OpenAI API Key、Bearer Token、私钥或 Windows 凭据导出。

## 本机数据边界

- 账号认证副本与备份位于 EXE 同级“账号文件”目录。
- ChatGPT 账号切换前，程序只在 Codex 已停止、账号身份唯一匹配且原副本校验通过时自动保存刷新后的 `auth.json`；保存使用预期旧哈希比较更新，身份值只用于内存中的不可逆指纹比较。
- 账号存储由独立本机锁串行化，完整账号切换与模型服务切换共用同一个 Codex 操作锁；源文件先复制到应用专属临时文件并校验预期哈希，备份后会复检原文件，任何失败恢复或后续回滚前也会确认目标仍是本次写入的版本。锁文件不包含凭据，但不要在程序运行时删除。
- 账号副本、`profiles.json` 与清单备份的多文件变更会先在“账号文件”目录写入 DPAPI 加密的 `.codex-account-switcher-account-transaction.dat`。下次账号存储入口会在同一锁内先恢复：全量事务后状态确认提交，混合状态逆序恢复，外部第三态保留现场并失败关闭。旧版本留下的严格命名移除墓碑也只在有效清单能够证明归属时恢复或清理。
- DeepSeek API Key 的持久副本位于按规范化 `CODEX_HOME` 派生并隔离的 Windows 凭据目标。v1.4 使用的全局目标只允许由规范默认 `%USERPROFILE%\.codex` 在验证后复制，并写入独立迁移标记；由于 Windows 凭据管理器没有原子比较删除，旧目标为降级兼容而保留，避免并行旧版本写入时误删较新值。自定义 home 不读取、认领或删除该全局凭据，只能从无冲突的活动受管 DeepSeek 配置对账到自己的目标。
- DeepSeek 只允许官方精确 slug `deepseek-v4-flash` 与 `deepseek-v4-pro`。未知或不受支持的模型不会被程序接管；在 DeepSeek 模式下更新 API Key 时保持锁内重新确认的当前实际模型。
- 本程序对接管的 Flash 和 Pro 统一写入 `model_reasoning_effort = "max"`。这是本程序选择的运行策略，不是 DeepSeek 官方强制要求。
- DeepSeek 模式激活期间，API Key 会按 Codex 配置要求写入本机 `config.toml`；切回 OpenAI 时移除受管字段。同服务切换模型或更新密钥不会改变最初保存的 OpenAI 恢复基线。
- DeepSeek 配置事务对 `provider_state.dat`、`models.json` 和 `config.toml` 使用预期旧内容比较更新，并在每次写入及回滚前复检。提交前会在 `CODEX_HOME` 写入 DPAPI 加密的全局 `.codex-account-switcher-provider-transaction.dat`：全部目标都已达到事务后状态时确认提交；事务前/事务后状态混合时只回滚仍属于该事务的内容；任一目标出现第三种外部内容时失败关闭、保留日志且不覆盖外部内容。
- `provider_state.dat` 和事务日志只允许同一 Windows 用户解密，不能作为跨用户或跨设备备份。事务日志还绑定创建事务时的 `provider_state.dat` 路径；若不同便携副本共享同一 `CODEX_HOME` 而数据目录不同，必须返回原始副本恢复。先备份相关目录，不要手动删除日志或 state；不能确认来源时通过私密安全公告联系维护者。
- 共享锁只能约束遵守它的本程序实例。对每个文件的预期内容比较、原子替换和多点复检会显著缩小竞争窗口，但普通文件 API 仍无法消除最终检查与替换之间的 TOCTOU；无法确认所有权时程序选择停止，而不是覆盖可疑内容。
- 硬退出恢复后，如果受管、无冲突的 DeepSeek `config.toml` 与保存凭据不同，活动配置中的经过校验的 API Key 是实际运行值和对账权威；OpenAI 模式下 Windows 凭据中的值仅表示下一次切换偏好。
- 账号存储日志、当前 `auth.json`、provider 日志、Windows 凭据和 Codex 进程状态是分开的恢复边界。程序不会声称这些资源具有单一文件系统事务；若进程恰好在边界之间被强制终止，下次启动会先收敛可证明的日志并展示磁盘真实状态，可能仍需重新执行账号切换或手动启动 Codex。

如怀疑凭据已泄露，请立即在对应服务撤销或轮换密钥，并移除受影响的本机认证副本。
