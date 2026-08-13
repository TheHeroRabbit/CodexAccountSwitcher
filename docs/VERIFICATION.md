# v1.5.0 源码与发布成品验收记录

验收日期：2026-08-13

本记录对应当前工作区的 v1.5.0 交付源码，以及 2026-08-13 16:32（Asia/Shanghai）由该源码强制重建的发布成品。结果只证明下列环境、源码快照和明确列出的产物；后续若修改任何代码、测试、依赖、资源、说明或打包配置，必须重新执行 `RELEASE_CHECKLIST.md` 并更新本记录。

## 官方接入依据

- 2026-08-13 重新核对 DeepSeek 官方 [Integrate with Codex](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/) 页面：Codex 使用 DeepSeek 原生支持的 Responses API；官方模型目录同时包含精确 slug `deepseek-v4-flash` 和 `deepseek-v4-pro`；目录声明最低 Codex 客户端版本为 0.144.0。
- 本地 `assets/deepseek_models.json` 与官方 Windows 一键脚本当日提供的模型目录逐字节一致，大小 76,079 字节，SHA-256 为 `B459A6E438D6A9939D01FD0DBB4693F165ED732BC8E4FD58D7145D9D94BD49A4`。
- 官方目录声明 Flash/Pro 均支持 `low`、`high`、`max`，官方默认仍为 `high`；本程序在接管任一模型时统一写入 `model_reasoning_effort = "max"`，这是用户要求和本程序策略，不冒充官方强制默认值。
- 本次未使用真实 DeepSeek API Key，也未执行可能计费的真实 API 请求。验收范围是官方字段/slug/固定资产对齐、配置事务、UI、打包和本地故障注入。

## 验收环境

- 操作系统：Microsoft Windows 10 专业版，10.0.19045，64 位（CIM 实际值；Python 兼容层在 PyInstaller 日志中显示 19041）。
- Python：3.8.5，项目 `.venv\Scripts\python.exe`。
- PyInstaller：6.19.0。
- pyinstaller-hooks-contrib：2026.6。
- Pillow：10.4.0。
- tomlkit：0.13.3。
- Ruff：0.12.12。
- 检查、测试和构建均使用项目固定虚拟环境，未向全局 Python 安装项目依赖。

## 源码、静态门禁与测试

最终候选源码实际执行并通过：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe tools\repository_check.py
git diff --check
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q
```

- Ruff 静态检查通过。
- Ruff 格式检查通过，18 个 Python 文件符合规范。
- `compileall` 通过。
- 仓库安全与完整性检查通过，共检查 55 个受跟踪源码文件。
- `git diff --check` 通过。
- 功能实现冻结后的独立全量测试：216 项，64.851 秒，全部通过；随后新增“验收报告正文版本不得滞后于标题”的仓库门禁回归测试。
- 最终主工作区 `release.ps1 -Force` 执行 217 项测试：64.245 秒，全部通过，然后清理 PyInstaller 缓存并全新构建；验收报告回填完成后再次独立执行最终工作树 217 项测试：65.775 秒，全部通过。

测试覆盖的关键交付不变量包括：

- 只接受 `deepseek-v4-flash` / `deepseek-v4-pro`，拒绝近似拼写和未知模型；两模型写入 Responses provider、固定目录与 `max`。
- OpenAI → Flash/Pro、Flash ↔ Pro、同模型更新 Key、DeepSeek → OpenAI，以及 v1.4 Flash/`high` 固定历史状态升级到 Pro/`max` 后恢复原 OpenAI 基线。
- 更新 Key 在共享锁内保持实际活动模型；模型仅切换时以受管活动配置中的实际 Key 为权威；多窗口 expected-key CAS 在对账、停止 Codex和写入前执行。
- Provider 的 DPAPI 多文件日志覆盖首个目标前、部分提交、最后目标后未清日志、恢复再次中断、日志中途消失、损坏/非法 schema、容量、不同便携 state 路径和 changed target 第三态。
- 账号副本、主清单与清单备份的 DPAPI 日志覆盖导入、更新、移除、重命名、修复的关键硬退出阶段、恢复再次中断、日志中途消失、第三态、旧 `.remove` 墓碑、损坏/超限和错误目录。
- 配置、账号和 Windows 凭据失败补偿只回滚仍能证明属于本次操作的内容，不覆盖较新的第三方值；Codex 原本未运行时，失败补偿不会意外启动客户端。
- v1.4 全局 Windows 凭据只由规范默认 `%USERPROFILE%\.codex` 复制到 home-scoped 目标并写迁移标记；旧目标为降级兼容保留，自定义 `CODEX_HOME` 不认领或删除全局值。
- 账号身份轮换同步、预期旧哈希比较、备份窗口、跨账号/跨模式拒绝、重复身份、遗留格式、锁超时、唯一 terminal 结果和 UI 控件真实按钮接线。
- 真实子进程 `os._exit` 故障注入验证应用专属 config/auth 原子临时文件会在下次取得共享锁后清理，近似名称和目录不被误删，虚构密钥/令牌不残留在孤儿临时文件中。
- 仓库扫描确认跟踪文件与发布输入不含真实 `auth.json`、API Key、Bearer Token、GitHub Token、私钥、provider/account 事务日志或本机绝对环境路径。

## 仅受跟踪文件的隔离复现

从最终应用源码状态仅复制 `git ls-files` 返回的 55 个文件到 Git 忽略的 `_local_artifacts\v1.5-source-snapshot-final-20260813-1632`；没有复制 `.git`、`.venv`、`build`、`dist`、`release`、`artifacts`、账号数据或其他忽略文件。快照构建后仅修改本报告中的最终测试时长和验收措辞，未再修改应用、测试、资源、打包脚本或随包说明。快照使用主项目固定 Python 解释器执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\release.ps1 `
  -PythonPath E:\PythonProject\CodexAccountSwitcher\.venv\Scripts\python.exe `
  -Force
```

- 回退式仓库检查仍识别并通过 55 个源码文件。
- 最终 217 项测试再次全部通过；其中第 217 项验证本验收报告正文的版本证据不得滞后于标题，不进入应用运行时。
- PyInstaller 从空的快照 `build`/`dist`/`release` 目录成功构建并打包。
- 快照 EXE 通过真实窗口、进程树、隔离配置目录创建和退出码 0 冒烟。
- 快照成品用于证明构建不依赖主工作区旧缓存或未跟踪文件；由于 PyInstaller 会嵌入构建路径等信息，其二进制哈希不要求与主发布 EXE 相同，也不作为上传资产。

## UI 与对话框验收

最终候选源码重新生成并逐张检查以下截图：

- OpenAI 账号页：1080×720、900×600。
- DeepSeek V4 Flash：1080×720、900×600。
- DeepSeek V4 Pro：1080×720、900×600。
- 账号处理中：900×600。
- Provider 处理中：900×600。
- 成功、确认、启动失败警告、备注、API Key 五类对话框。

检查结果：无截断、重叠或不可达操作；900×600 下底部操作仍完整；Flash/Pro 选择态和当前模型一致；处理中禁用态可辨识；成功、确认和启动失败文案均显示准确的 `DeepSeek V4 Pro`；API Key 仅以掩码呈现。冲突、未知模型、外部恢复备份和凭据错误状态另由组件测试与确定性构造状态断言覆盖。

文档图 `docs/images/deepseek-provider.png` 为 1080×720 的 Pro 受管状态，不包含真实用户名、路径、账号或凭据。

## 最终构建与运行冒烟

主工作区实际执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\release.ps1 -Force
```

脚本完成仓库检查、图标生成、217 项测试、PyInstaller 清缓存单文件构建、发布目录重建、EXE/ZIP 校验文件生成。随后独立执行：

- `tools/exe_smoke.py`：`dist` EXE 通过。
- `tools/exe_smoke.py`：发布目录 EXE 通过。
- ZIP 解压到新目录后，解压 EXE 通过。
- 隔离受跟踪文件快照 EXE 通过。

四次结果均为：创建一个主窗口、识别预期进程树、在隔离目录创建运行时账号目录、正常退出且退出码 0。三个主发布位置的 EXE 大小和 SHA-256 完全一致。

PyInstaller 归档查看器确认 EXE 内含：

- `assets\app_icon.png`
- `assets\app_icon_64.png`
- `assets\deepseek_models.json`

PyInstaller warning 文件已人工审查，仅有 Windows 构建不使用的 POSIX/Jython/VMS 可选模块、`uuid` 可选网络模块，以及账号锁在非 Windows 分支延迟导入的 `fcntl`；未发现产品运行所需模块缺失。

## v1.5.0 发布成品

- 发布目录：`release\Codex账号切换器-v1.5.0`。
- 发布包：`release\CodexAccountSwitcher-v1.5.0.zip`。
- ZIP 校验文件：`release\CodexAccountSwitcher-v1.5.0.zip.sha256.txt`。
- EXE：9,222,866 字节。
- EXE SHA-256：`C8DE54FE9870EBEC1E44A27C89265B18DF27D5AD82990DC433837312DE4653EA`。
- ZIP：8,990,416 字节。
- ZIP SHA-256：`6C6AFFFDA57D10BBF3B69F6CD7FFA2B91602FF9F850ADB2844CAC2D0B2191590`。
- 发布目录 `SHA256.txt` 与独立复算 EXE 哈希一致。
- 外层 ZIP `.sha256.txt` 与独立复算 ZIP 哈希一致。

Windows 版本资源已按 Unicode 码点核对：

- FileVersion：1.5.0。
- ProductVersion：1.5.0。
- ProductName：Codex切换器。
- CompanyName：本地工具。
- FileDescription：Codex账号与模型服务切换工具。
- OriginalFilename：Codex账号切换器.exe。
- Authenticode：`NotSigned`。

ZIP 使用 UTF-8 entry flag，`zipfile.testzip()` 返回 `None`，共且仅有 5 个条目：

| 条目 | 字节 | CRC32 |
| --- | ---: | --- |
| `Codex账号切换器-v1.5.0/Codex账号切换器.exe` | 9,222,866 | `6517CFBE` |
| `Codex账号切换器-v1.5.0/SHA256.txt` | 92 | `4D34C4CB` |
| `Codex账号切换器-v1.5.0/使用说明.txt` | 5,612 | `8CD38B4B` |
| `Codex账号切换器-v1.5.0/更新说明.txt` | 3,198 | `E345E2AD` |
| `Codex账号切换器-v1.5.0/账号文件/请勿分享此文件夹.txt` | 399 | `4CA4F971` |

工作区 `release` 根目录还保留历史 v1.4.0 本地产物；它们不在 v1.5.0 ZIP 内，也不属于本记录验收/上传范围。交付或 GitHub Release 只应选择上述 v1.5.0 三个目标。

## 已知且已披露的边界

- EXE 未使用商业代码签名证书，签名状态为 `NotSigned`；首次运行可能显示 Windows 信誉提示。
- 未使用真实 OpenAI/DeepSeek 凭据或执行真实计费请求；身份、凭据和故障路径使用隔离临时目录中的虚构数据验证。
- Provider 与账号日志证明的是应用进程硬退出后的可重放恢复。Windows/Python 没有为普通跨文件写入提供单一原子事务，也未宣称突然断电下具备文件系统/硬件级 durability。
- 账号副本/清单日志、当前 `auth.json`、provider 日志、Windows 凭据和 Codex 进程控制是独立恢复边界。强制终止可能停在最后一个已完成阶段；下次启动会先收敛各自日志并显示磁盘真实状态，用户可能需要重试账号切换、安全切回 OpenAI或手动启动 Codex。
- 跨进程锁只协调本工具实例，不能强制第三方程序遵守。文件写入和凭据补偿均做所有权复检，但普通文件 API/Windows Credential Manager 在最终复检与替换之间仍有极小 TOCTOU 窗口；无法证明归属时按失败关闭处理。
- Provider 全局日志绑定创建事务的 `provider_state.dat` 绝对路径；共享同一 `CODEX_HOME` 的另一便携副本会拒绝代替原副本恢复。账号日志同样绑定原“账号文件”目录。遇到路径不匹配或损坏日志时必须先备份并返回原副本，不要手动删除 journal/state。
- DPAPI 状态和日志只能由同一 Windows 用户在本机解密，不是跨用户、跨设备备份格式。
- GitHub Actions 文件已完成本地等价命令验证，但托管流水线只能在推送后实际运行；本次未创建或上传 GitHub Release。
