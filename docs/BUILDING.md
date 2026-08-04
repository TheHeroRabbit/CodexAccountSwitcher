# 构建说明

## 1. 准备独立环境

项目维护默认使用 `C:\venv`。如果该环境已存在，请直接使用，不要重建或向全局 Python 安装依赖。

首次准备环境时，可使用已安装的 Python 3.8 创建：

```powershell
py -3.8 -m venv "C:\venv"
& "C:\venv\Scripts\python.exe" -m pip install -r ".\requirements-dev.txt"
```

依赖分层如下：

- `requirements.txt`：源码运行依赖。
- `requirements-build.txt`：运行依赖与 PyInstaller。
- `requirements-dev.txt`：构建依赖与 Ruff。

所有依赖均固定版本。更新依赖时必须重新运行完整测试和 EXE 验收。

## 2. 本地质量检查

```powershell
& "C:\venv\Scripts\python.exe" -m ruff check "."
& "C:\venv\Scripts\python.exe" -m ruff format --check "."
& "C:\venv\Scripts\python.exe" -m unittest discover -s ".\tests" -v
& "C:\venv\Scripts\python.exe" ".\tools\repository_check.py"
```

`repository_check.py` 检查：

- 必需仓库文件是否存在。
- 是否跟踪运行时账号数据、配置、密钥、构建产物或历史截图。
- 文本文件是否为 UTF-8、LF、无 BOM、无行尾空格并以换行结束。
- 是否出现疑似 API Key、Bearer Token、GitHub Token 或私钥头。
- 官方 `deepseek_models.json` 的 SHA-256、JSON 结构和必要模型元数据。

该检查不会输出检测到的疑似密钥值。

## 3. 构建 EXE

```powershell
& ".\build.ps1"
```

默认行为：

1. 确认解释器和固定构建依赖。
2. 执行仓库检查。
3. 从规范图标源生成 PNG 与 ICO。
4. 运行全部自动化测试。
5. 使用 PyInstaller 生成单文件、无控制台窗口的 EXE。

输出：`dist\Codex账号切换器.exe`。

可选参数：

```powershell
# 使用另一个独立虚拟环境
& ".\build.ps1" -VenvPath "D:\venvs\codex-switcher"

# CI 或已知解释器路径
& ".\build.ps1" -PythonPath "C:\path\to\python.exe"

# 已在同一流水线完成测试时跳过重复测试
& ".\build.ps1" -SkipTests
```

`-SkipRepositoryCheck` 仅用于诊断尚未初始化 Git 的本地副本，不应在正式发布时使用。

## 4. 创建发布 ZIP

```powershell
& ".\release.ps1"
```

发布脚本调用正式构建，并创建：

```text
release/
├── Codex账号切换器-v1.3.0/
│   ├── Codex账号切换器.exe
│   ├── SHA256.txt
│   ├── 使用说明.txt
│   ├── 更新说明.txt
│   └── 账号文件/请勿分享此文件夹.txt
├── Codex账号切换器-v1.3.0.zip
└── Codex账号切换器-v1.3.0.zip.sha256.txt
```

脚本默认拒绝覆盖同名发布结果。确认目标确实是本项目 `release\` 目录后，可使用 `-Force` 重建。

## 5. EXE 冒烟测试

```powershell
& "C:\venv\Scripts\python.exe" ".\tools\exe_smoke.py" ".\dist\Codex账号切换器.exe"
```

该工具会在独立临时目录启动 EXE，验证窗口标题、进程树、同级“账号文件”目录和正常退出。它不会使用真实认证文件。

## 6. GitHub Actions

`.github/workflows/ci.yml` 在 `push` 和 `pull_request` 时使用 Windows runner 执行格式、静态、测试、安全和打包检查，并上传本次 CI 生成的发布 ZIP。CI 的构建产物不包含代码签名。
