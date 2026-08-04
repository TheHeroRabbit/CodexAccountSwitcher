# 构建说明

## 1. 环境策略

本项目统一使用仓库根目录的 `.venv`：

```text
CodexAccountSwitcher/
├── .venv/
├── app.py
├── setup.ps1
├── build.ps1
└── release.ps1
```

脚本通过自身位置定位仓库根目录。即使从其他工作目录调用，虚拟环境仍会创建在项目内，不会落到调用者目录，也不会向全局 Python 安装依赖。

`.venv` 已被 `.gitignore` 和仓库安全检查排除，不会进入 Git 提交或发布包。

## 2. 创建项目虚拟环境

在仓库根目录运行：

```powershell
& ".\setup.ps1"
```

默认流程：

1. 使用 Windows Python Launcher 查找 Python 3.8。
2. 在仓库根目录创建 `.venv`。
3. 验证 `sys.prefix` 确实指向该项目 `.venv`。
4. 检查 Python 版本不低于 3.8。
5. 将固定的开发依赖安装到 `.venv`。

如果系统没有 `py.exe` 或需要指定基础解释器：

```powershell
& ".\setup.ps1" -BasePythonPath "D:\Python38\python.exe"
```

`-BasePythonPath` 指向的解释器只负责创建项目虚拟环境；`pip install` 始终由 `.venv\Scripts\python.exe` 执行。

只运行源码、不需要 Ruff 和 PyInstaller时，可使用：

```powershell
& ".\setup.ps1" -RuntimeOnly
```

若 `.venv` 已存在且完整，脚本复用该环境并同步依赖；不会自动删除或重建。若目录存在但缺少 `Scripts\python.exe`，脚本会停止并要求用户显式处理，避免覆盖未知文件。

依赖分层：

- `requirements.txt`：源码运行依赖。
- `requirements-build.txt`：运行依赖与 PyInstaller。
- `requirements-dev.txt`：构建依赖与 Ruff。

## 3. 本地质量检查

```powershell
& ".\.venv\Scripts\python.exe" -m ruff check "."
& ".\.venv\Scripts\python.exe" -m ruff format --check "."
& ".\.venv\Scripts\python.exe" -m unittest discover -s ".\tests" -v
& ".\.venv\Scripts\python.exe" ".\tools\repository_check.py"
```

`repository_check.py` 检查：

- 必需仓库文件是否存在。
- 是否跟踪 `.venv`、运行时账号数据、配置、密钥、构建产物或历史截图。
- 是否重新引入固定磁盘位置的虚拟环境路径。
- 文本是否为 UTF-8、LF、无 BOM、无行尾空格并以换行结束。
- 是否出现疑似 API Key、Bearer Token、GitHub Token 或私钥头。
- 官方 `deepseek_models.json` 的 SHA-256、JSON 结构和必要模型元数据。

检查不会输出检测到的疑似密钥值。

## 4. 构建 EXE

```powershell
& ".\build.ps1"
```

默认使用 `.venv\Scripts\python.exe`。如果 `.venv` 不存在，构建脚本会自动调用 `setup.ps1` 创建并安装开发依赖。

构建步骤：

1. 验证解释器确实属于选定虚拟环境。
2. 确认固定构建依赖。
3. 执行仓库安全与完整性检查。
4. 从规范图标源生成 PNG 与 ICO。
5. 运行全部自动化测试。
6. 使用 PyInstaller 生成单文件、无控制台窗口的 EXE。

输出：`dist\Codex账号切换器.exe`。

高级覆盖参数只用于已有独立环境或 CI 诊断：

```powershell
# 相对路径始终按项目根目录解析
& ".\build.ps1" -VenvPath ".venv-alt"

# 使用明确的虚拟环境解释器
& ".\build.ps1" -PythonPath "D:\venvs\codex-switcher\Scripts\python.exe"

# 已在同一流水线完成测试时跳过重复测试
& ".\build.ps1" -SkipTests
```

`-SkipRepositoryCheck` 仅用于诊断，不应在正式发布时使用。

## 5. 创建发布 ZIP

```powershell
& ".\release.ps1"
```

发布脚本沿用与构建脚本相同的 `.venv` 解析规则，并创建：

```text
release/
├── Codex账号切换器-v1.4.0/
│   ├── Codex账号切换器.exe
│   ├── SHA256.txt
│   ├── 使用说明.txt
│   ├── 更新说明.txt
│   └── 账号文件/请勿分享此文件夹.txt
├── Codex账号切换器-v1.4.0.zip
└── Codex账号切换器-v1.4.0.zip.sha256.txt
```

脚本默认拒绝覆盖同名发布结果。确认目标确实是本项目 `release\` 后，可使用 `-Force` 重建。

## 6. EXE 冒烟测试

```powershell
& ".\.venv\Scripts\python.exe" ".\tools\exe_smoke.py" ".\dist\Codex账号切换器.exe"
```

工具在独立临时目录启动 EXE，验证窗口标题、进程树、同级“账号文件”目录和正常退出，不会使用真实认证文件。

## 7. GitHub Actions

Windows CI 先通过 `actions/setup-python` 提供基础解释器，再调用 `setup.ps1 -BasePythonPath ...` 创建仓库内 `.venv`。后续 Ruff、测试、仓库检查、构建、发布和 ZIP 校验全部使用 `.venv\Scripts\python.exe`，与本地推荐流程一致。
