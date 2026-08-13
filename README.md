# Codex 切换器

Codex 切换器是一款仅在本机运行的 Windows 桌面工具，用于管理 Codex 的 OpenAI 账号认证，并在 OpenAI 与 DeepSeek 之间安全切换。

> 本项目是非官方辅助工具，不隶属于 OpenAI 或 DeepSeek。程序不会修改 Codex 客户端安装文件。

![OpenAI 账号管理界面](docs/images/openai-accounts.png)

## 功能

- 从当前 Codex 或指定的 `auth.json` 添加 OpenAI 账号。
- 切换前自动保存 Codex 已刷新并回写的最新 ChatGPT 认证，避免恢复过期快照。
- 更新认证副本、修改备注、移除账号并保留必要备份。
- 切换 OpenAI 账号后自动重启 Microsoft Store 版 Codex。
- 选择 DeepSeek V4 Flash 或 V4 Pro，并精确恢复切换前的 OpenAI 配置。
- 在 DeepSeek 模式下直接进行 V4 Flash 与 V4 Pro 双向切换；更新 API Key 时保持当前实际模型。
- 本程序接管 Flash 或 Pro 时统一设置 `model_reasoning_effort = "max"`；这是本程序的运行策略，不是 DeepSeek 官方对该字段的强制要求。
- DeepSeek 模式下可直接切换 OpenAI 账号，一次完成服务恢复、账号替换和客户端重启。
- 检测受管配置的外部修改；存在冲突时停止覆盖。
- 通过两套 DPAPI 加密的持久事务日志处理 provider 多文件配置与账号副本/清单的普通失败和进程硬退出；只恢复仍能证明属于本次事务的内容。

## 安全设计

- 账号认证副本仅保存在 EXE 同级的“账号文件”目录。
- ChatGPT 账号只通过 `auth_mode` 与 `tokens.account_id` 的不可逆内存指纹匹配；账号 ID、邮箱和令牌不会写入账号清单、日志或界面。
- 自动同步只在 Codex 已停止、账号唯一匹配且原副本哈希可信时执行；保存前再次比较预期旧哈希，身份重复、错配、并发更新或外部修改都会停止覆盖。
- 账号存储以及账号/模型服务完整切换事务使用进程内可重入锁与跨进程锁文件串行化，避免多个程序实例交叉停止客户端或互相覆盖；锁等待超时会在停止 Codex 和修改文件前安全退出。
- DeepSeek 模型仅接受 `deepseek-v4-flash` 与 `deepseek-v4-pro` 两个官方 slug；恢复状态、模型目录和配置文件使用预期旧值比较更新，失败回滚前也会确认目标仍是本次写入内容。
- DeepSeek API Key 保存在按 `CODEX_HOME` 隔离的 Windows 凭据目标中，界面只显示末四位；v1.4 的全局凭据只由规范的默认 `%USERPROFILE%\.codex` 复制并用独立迁移标记防止重复导入。旧目标为兼容降级而保留，自定义 home 不会读取或认领它。
- 模型服务恢复状态、位于 `CODEX_HOME` 的全局 `.codex-account-switcher-provider-transaction.dat`，以及“账号文件”目录中的 `.codex-account-switcher-account-transaction.dat` 均使用当前 Windows 用户的 DPAPI 加密。
- 每次读取状态、激活或恢复前都会先处理残留事务日志：所有目标都已是新内容时确认提交，只有部分目标写入时回滚，出现既非事务前也非事务后内容时停止自动恢复且绝不覆盖该外部内容。
- 配置和认证文件采用应用专属临时文件、落盘同步和原子替换；下次取得相应锁时会清理严格匹配的孤儿临时文件。
- 仓库检查会拒绝认证文件、私钥、疑似 API Key、运行时数据和构建产物。
- 发布包不会包含本机账号数据、Codex 配置或 Windows 凭据。

使用 DeepSeek 期间，Codex 会按官方配置要求从当前 `CODEX_HOME\config.toml` 读取 API Key；同一 DeepSeek 会话内切换模型或更新密钥会保留最初的 OpenAI 恢复基线，切回 OpenAI 后，本程序会移除受管的 DeepSeek 配置并恢复原状态。若进程硬退出后保存凭据与活动受管配置不一致，活动受管 `config.toml` 中经过校验的 API Key 是实际运行值和对账权威；OpenAI 模式下保存的凭据只是下一次切换使用的偏好值。

账号副本/清单事务、当前 `auth.json` 替换、provider 配置和 Codex 进程控制属于不同存储或进程边界，不冒充操作系统级单一原子事务。程序会按已完成阶段恢复或确认提交；若恰好在这些边界之间被强制终止，重新打开后会先收敛各自日志，界面按磁盘真实状态显示，必要时可重新执行目标账号切换或安全切回 OpenAI。此时不要手动删除事务日志。

ChatGPT 登录令牌的获取、刷新和失效判断仍完全由 Codex 官方客户端负责。本程序不调用私有 OAuth 接口，也不硬编码刷新周期；它只在下一次账号切换覆盖 `auth.json` 之前，保存 Codex 已经写回的最新文件。

## 环境要求

- Windows 10 或 Windows 11（64 位）。
- 自动重启功能需要 Microsoft Store 版 Codex。
- DeepSeek V4 Flash / V4 Pro 接入需要 Codex 0.144.0 或更高版本。
- 直接运行源码或构建时需要 Python 3.8 或更高版本。
- 项目依赖安装在仓库根目录的 `.venv`，不会安装到全局 Python 环境。

## 快速开始

### 运行源码

```powershell
& ".\setup.ps1"
& ".\.venv\Scripts\python.exe" ".\app.py"
```

`setup.ps1` 使用系统已安装的 Python 3.8 创建项目内 `.venv`，随后所有依赖只安装到该目录。如需指定其他基础解释器，可传入 `-BasePythonPath`；该解释器只用于创建 `.venv`。

### 构建 EXE

```powershell
& ".\build.ps1"
```

构建结果位于 `dist\Codex账号切换器.exe`。如果项目 `.venv` 不存在，构建脚本会先调用 `setup.ps1` 创建；随后执行仓库安全检查、图标生成和全部自动化测试。

### 创建交付包

```powershell
& ".\release.ps1"
```

发布脚本会生成带使用说明和 SHA-256 校验文件的 ASCII 命名 ZIP，例如 `release\CodexAccountSwitcher-v1.5.0.zip`。已有同名输出不会被覆盖；确认需要替换时显式传入 `-Force`。

## 质量检查

```powershell
& ".\.venv\Scripts\python.exe" -m ruff check "."
& ".\.venv\Scripts\python.exe" -m ruff format --check "."
& ".\.venv\Scripts\python.exe" -m unittest discover -s ".\tests" -v
& ".\.venv\Scripts\python.exe" ".\tools\repository_check.py"
```

GitHub Actions 会在 Windows 环境中重复执行静态检查、格式检查、测试、仓库安全检查和 PyInstaller 构建。

## 项目结构

```text
.
├── app.py                    # Tkinter 界面与交互编排
├── account_store.py          # 账号认证存储、DPAPI 持久日志与事务恢复
├── codex_client.py           # Microsoft Store Codex 重启边界
├── credential_store.py       # Windows 凭据管理器与 DPAPI
├── provider_config.py        # OpenAI / DeepSeek 配置事务
├── assets/                   # 图标源文件与固定模型目录
├── docs/                     # 用户、架构、构建与发布文档
├── packaging/                # 发布包内的纯文本说明
├── tests/                    # 自动化测试
├── tools/                    # 仓库检查、视觉检查与 EXE 冒烟测试
├── setup.ps1                 # 创建项目 .venv 并安装固定依赖
├── build.ps1                 # 可复现 EXE 构建
└── release.ps1               # 可复现交付 ZIP 构建
```

## 兼容性与限制

- 当前自动重启仅适用于 Microsoft Store 版 Codex；其他安装方式需要手动重启。
- 自动同步适用于当前带有 `auth_mode: "chatgpt"` 和 `tokens.account_id` 的 Codex 托管 ChatGPT 认证。旧格式和 API Key 认证可继续读取、添加和切换，同模式 API Key 及两侧都不是 ChatGPT 的旧格式仍可手动更新，但不会执行 ChatGPT 令牌自动同步；如果旧 ChatGPT 认证缺少可验证身份，程序会拒绝把变化后的 ChatGPT/API Key 文件手动覆盖到该记录，请移除旧记录并重新添加当前认证。
- 自动同步以 Codex 实际写回 `.codex/auth.json` 为前提；若 Codex 被配置为只把凭据保存在操作系统凭据存储中，本程序无法同步未出现在该文件里的更新。
- 自动同步不能保证永久免登录。刷新令牌被撤销、真正过期，或受到 MFA、密码、安全设置及组织策略影响时，Codex 仍可能要求重新登录。
- 如果旧版本已经保存了同一 ChatGPT 身份的多个副本，程序会停止自动覆盖并要求先保留正确记录，避免把最新令牌写错账号项。
- 跨进程锁用于协调本工具的实例；其他程序不会自动遵守该锁。切换期间应避免用第三方工具同时修改 `auth.json`、`config.toml`、`models.json` 或账号目录。程序会在判断、备份、写入和恢复阶段多次校验内容，但普通文件 API 无法提供真正的跨文件原子比较交换，最后一次校验与替换之间仍存在很小的 TOCTOU 竞争窗口；发现无法证明归属的内容时程序会失败关闭而不是强行覆盖。
- DeepSeek 页面只开放官方精确 slug `deepseek-v4-flash` 与 `deepseek-v4-pro`，默认选择 V4 Flash。当前配置为未知或不受支持的 DeepSeek 模型时，程序不会猜测或覆盖；只有存在可信 OpenAI 恢复基线时才允许安全切回。
- 全局事务日志会记录创建事务时使用的 `provider_state.dat` 路径。若另一个便携副本使用同一 `CODEX_HOME` 打开它，程序会停止自动恢复；请先备份当前 `.codex` 和两个程序数据目录，返回创建该日志的原始便携副本完成恢复。不要手动删除日志或 `provider_state.dat`；无法确认原始副本时请保留现场并通过私密安全渠道联系支持。
- 发布的 EXE 未使用商业代码签名证书，Windows 首次运行时可能显示安全提示。
- 本项目不会联网验证 API Key；真实调用可能产生费用，由用户自行确认。

## 文档

- [使用指南](docs/USER_GUIDE.md)
- [架构与安全边界](docs/ARCHITECTURE.md)
- [构建说明](docs/BUILDING.md)
- [发布验收清单](docs/RELEASE_CHECKLIST.md)
- [当前源码验收记录](docs/VERIFICATION.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [第三方资料说明](THIRD_PARTY_NOTICES.md)

## 许可证

本仓库当前未附加开源许可证。未经著作权人明确授权，不得复制、修改或再分发本项目源码。若计划将仓库作为开源项目公开发布，请先选择并补充合适的许可证。
