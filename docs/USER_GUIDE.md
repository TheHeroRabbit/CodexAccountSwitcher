# 使用指南

## OpenAI 账号

![OpenAI 账号页](images/openai-accounts.png)

### 添加当前账号

1. 打开“OpenAI 账号”。
2. 点击“从当前 Codex 添加”。
3. 输入用于本机识别的备注并保存。

程序读取 `%USERPROFILE%\.codex\auth.json`，校验后将副本保存到 EXE 同级“账号文件”目录。备注不会写入认证文件。

### 手动选择认证文件

1. 点击“选择 auth.json”。
2. 从文件选择器中选择有效的 UTF-8 JSON 文件。
3. 输入备注并保存。

文件选择器默认打开当前用户的 `.codex` 目录。重复内容不会再次创建账号记录。

### 切换账号

1. 在账号列表中选择目标账号。
2. 点击“切换账号并重启”。
3. 确认正在运行的 Codex 任务可以中断。

程序会关闭 Microsoft Store 版 Codex、备份当前认证文件、替换 `auth.json`，然后重新启动并确认客户端状态。自动启动失败时，已成功完成的认证切换会保留，界面会提示手动启动 Codex。

### 更新、修改备注和移除

- “从当前 Codex 更新”会用当前 `.codex\auth.json` 替换所选账号的旧副本，并保留更新备份。
- “修改备注”只更改本地显示名称。
- “移除”删除所选账号的本地副本和清单记录，不修改当前 `.codex\auth.json`。

如果保存的认证副本在程序外被修改，应用会停止切换并提示重新添加或更新，避免使用来源不明的内容。

## DeepSeek

![DeepSeek 模型服务页](images/deepseek-provider.png)

### 切换到 DeepSeek

1. 打开“DeepSeek”。
2. 点击“设置 API Key”，输入以 `sk-` 开头的 DeepSeek API Key。
3. 点击“切换到 DeepSeek 并重启”。
4. 确认切换并等待 Codex 重新启动。

API Key 保存在 Windows 凭据管理器；界面只显示末四位。切换期间，程序按官方要求更新 `.codex\config.toml` 和 `models.json`。

### 更新或移除 API Key

- 在 OpenAI 模式下，可通过 API Key 编辑窗口更新或移除保存的密钥。
- 在 DeepSeek 模式下更新密钥会同步更新当前受管配置，不改变原 OpenAI 恢复基线。
- 移除密钥前必须先切回 OpenAI，避免当前 DeepSeek 配置失去认证。

### 切回 OpenAI

点击“切回 OpenAI 并重启”。应用只恢复自己接管的配置字段，保留切换后新增的 MCP、插件、项目配置等其他设置。

如果受管字段或 `models.json` 已被外部程序修改，应用会停止覆盖并提示配置冲突。请先确认外部修改来源，不要直接删除 `provider_state.dat`，否则可能失去安全恢复基线。

### 在 DeepSeek 模式下切换 OpenAI 账号

直接返回“OpenAI 账号”页并选择账号。程序会一次完成：关闭 Codex、恢复 OpenAI 配置、替换账号认证、重新启动。任一步失败时会按已完成阶段回滚。

## 本机数据

EXE 同级“账号文件”目录可能包含：

- `profiles.json`：账号备注、文件索引和校验值。
- `auth_<账号ID>.json`：认证副本。
- `切换备份`、`更新备份`：操作前备份。
- `provider_state.dat`：DPAPI 加密的模型服务恢复状态。

这些文件均可能包含敏感信息。不要上传到 GitHub、网盘、聊天工具或公共日志；不要把整个程序目录交给他人。迁移设备时，应在目标设备重新添加账号和 API Key，不要依赖 DPAPI 状态文件跨设备恢复。

## 常见提示

### 未检测到 Microsoft Store 版 Codex

自动重启只支持 Microsoft Store 版。切换已成功时可手动启动 Codex；其他安装方式不在自动重启范围内。

### 认证文件无效

确认文件存在、大小不超过 10 MB、采用 UTF-8 编码、JSON 顶层为对象，且未被其他程序截断或替换。

### 配置冲突

表示 DeepSeek 激活后受管字段或模型目录被外部程序修改。先备份当前 `.codex` 目录，再确认修改来源；不要强行覆盖。

### Windows 安全提示

当前 EXE 未使用商业代码签名证书。请只使用可信来源的发布包，并先核对同目录 `SHA256.txt` 或 ZIP 的 `.sha256.txt` 文件。
