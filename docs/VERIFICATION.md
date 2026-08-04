# 当前源码验收记录

验收日期：2026-08-04

本记录对应当前 Git 暂存源码。结果只证明下列环境和源码快照已通过检查；后续改动应重新执行 `RELEASE_CHECKLIST.md`。

## 环境

- Windows 10 64 位（10.0.19041）。
- Python：项目 `.venv\Scripts\python.exe` 3.8.5。
- PyInstaller：6.19.0。
- PyInstaller hooks contrib：2026.6。
- Pillow：10.4.0。
- tomlkit：0.13.3。
- Ruff：0.12.12。
- 基础解释器仅用于创建项目 `.venv`；依赖安装、检查、测试和构建均由项目解释器执行。
- 未向全局 Python 安装项目依赖。

## 源码与仓库

- Ruff 静态检查通过。
- Ruff 格式检查通过，18 个 Python 文件符合规范。
- `compileall` 通过。
- 67 项自动化测试全部通过。
- 仓库检查通过，共检查 55 个 Git 跟踪文件。
- Git 暂存区 whitespace/error-marker 检查通过。
- 未跟踪认证文件、运行时配置、备份、构建产物或历史 UI 截图。
- 跟踪文本中未发现本机用户名或疑似 API Key、Bearer Token、GitHub Token、私钥。
- GitHub Actions 已统一通过 `setup.ps1` 创建项目 `.venv`，后续命令不使用工作流提供的全局环境。

## 干净快照

已直接从 Git 暂存区生成内存 ZIP，并在全新临时目录中执行：

1. 在不存在 `.venv` 的状态下直接运行默认 `build.ps1 -SkipTests`。
2. 由构建脚本调用 `setup.ps1`，在快照项目根目录创建 `.venv` 并安装固定开发依赖。
3. 使用快照内 `.venv` 完成图标重新生成和 PyInstaller 单文件构建。
4. 使用同一项目环境执行 Ruff 静态和格式检查。
5. 执行 67 项自动化测试。
6. 执行仓库安全与完整性检查。

全部通过。该检查确认构建不依赖本机被忽略的旧截图、旧图标中间稿或既有 `build`/`dist` 内容。

## 本次发布成品

- EXE：9,193,500 字节。
- EXE SHA-256：`77D37AFDD81D82148228C427CB5B9039DECB45785C5C9CC86C4354DAF664D302`。
- ZIP：8,957,595 字节。
- ZIP SHA-256：`6A3F24F7DB196B46F765772215EF01496BF55F02B194D27999BA2307BBB77031`。
- EXE 内已确认包含两个应用图标和固定的 DeepSeek 模型目录。
- 文件描述、产品名、文件版本、产品版本和原始文件名正确。
- `dist` EXE、发布文件夹 EXE、ZIP 解压 EXE 均通过真实窗口冒烟测试：单主窗口、同级“账号文件”目录创建、退出码 0。
- ZIP CRC、条目白名单、EXE 校验文件和 ZIP 校验文件全部通过。

## 已知边界

- EXE 未使用商业代码签名证书，签名状态为 `NotSigned`。
- PyInstaller 警告仅包含当前平台不使用的可选跨平台模块；已人工审查。
- GitHub Actions 文件已完成本地语法和等价命令验证，但托管流水线只能在推送到 GitHub 后实际运行。
- 未使用真实 DeepSeek API Key 发起可能计费的联网请求。
- 官方 Codex 接入页面在验收日仍仅明确支持 `deepseek-v4-flash`。
