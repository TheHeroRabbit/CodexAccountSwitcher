# 贡献指南

感谢你帮助改进 Codex 切换器。提交改动前，请先确认变更不会泄露本机认证数据，也不会扩大对 Codex 配置或进程的操作范围。

## 开发环境

项目主要面向 Windows 10/11，并以 Python 3.8 为兼容基线。项目维护时默认使用独立虚拟环境 `C:\venv`：

```powershell
& "C:\venv\Scripts\python.exe" -m pip install -r ".\requirements-dev.txt"
```

如需使用其他虚拟环境，可向构建脚本传入 `-VenvPath`；CI 可使用 `-PythonPath` 指定解释器。请勿在测试、截图或文档中使用真实 `auth.json`、API Key、用户名或本机配置。

## 变更原则

- 保持 Windows 本地运行，不引入不必要的网络请求或后台服务。
- 保持已有账号数据格式和旧版本数据兼容，除非变更包含经过测试的迁移方案。
- 对认证文件、配置写入和客户端重启继续采用最小权限、明确目标和失败回滚。
- UI 文案保持简洁、正式；布局至少覆盖 1080×720 与 900×600。
- 依赖必须写入相应的固定版本清单，不得依赖全局 Python 包。
- 不修改或删除与当前变更无关的用户数据。

## 提交前检查

```powershell
& "C:\venv\Scripts\python.exe" -m ruff check "."
& "C:\venv\Scripts\python.exe" -m ruff format --check "."
& "C:\venv\Scripts\python.exe" -m unittest discover -s ".\tests" -v
& "C:\venv\Scripts\python.exe" ".\tools\repository_check.py"
& ".\build.ps1"
```

涉及 UI 时，还应运行 `tools\ui_smoke.py` 和 `tools\dialog_smoke.py`，并人工检查生成的图片。涉及打包时，应运行 `tools\exe_smoke.py` 验证真实 EXE 窗口和退出行为。

## Pull Request

Pull Request 应说明：

- 用户可见的变化及设计理由。
- 涉及的配置、认证和回滚边界。
- 已实际运行的检查及结果。
- 未验证的情况和剩余风险。

不要在 Issue、Pull Request、日志或截图中粘贴真实凭据。安全问题请遵循 [SECURITY.md](SECURITY.md) 的私密报告方式。
