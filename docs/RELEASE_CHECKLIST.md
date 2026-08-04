# 发布验收清单

每次正式发布必须从干净的 Git 提交执行本清单。不得使用包含真实账号文件、API Key 或用户配置的目录制作发布包。

## 版本与来源

- [ ] `app.py`、`version_info.txt`、`CHANGELOG.md` 和发布文案的版本一致。
- [ ] DeepSeek 官方 Codex 接入页面仍支持当前字段和开放模型。
- [ ] `assets/deepseek_models.json` 来源、固定 SHA-256 和 `THIRD_PARTY_NOTICES.md` 一致。
- [ ] 固定依赖版本已审查，且仅安装到独立虚拟环境。

## 自动化检查

- [ ] `ruff check .` 通过。
- [ ] `ruff format --check .` 通过。
- [ ] 全部 `unittest` 通过。
- [ ] `tools/repository_check.py` 通过。
- [ ] `build.ps1` 使用预期解释器成功构建。
- [ ] PyInstaller 警告已审查，不包含缺失的必需模块。

## 功能与回滚

- [ ] 从当前 Codex 添加账号。
- [ ] 手动选择 `auth.json` 添加账号。
- [ ] 更新认证、修改备注、移除账号。
- [ ] OpenAI 账号切换与 Store 客户端重启。
- [ ] 切换到 DeepSeek、更新 API Key、切回 OpenAI。
- [ ] DeepSeek 模式下直接切换 OpenAI 账号。
- [ ] 非法 JSON/TOML、文件缺失、权限错误和外部配置冲突提示明确。
- [ ] 写入或启动失败时的回滚与手动恢复文案符合实际状态。

真实 API Key 的联网调用可能计费，不属于默认发布验收。若确需端到端调用，必须使用专用测试密钥并在完成后立即撤销。

## 视觉检查

- [ ] OpenAI 页与 DeepSeek 页在 1080×720 完整显示。
- [ ] OpenAI 页与 DeepSeek 页在 900×600 完整显示。
- [ ] 表格首列、行高、列宽、按钮比例、卡片边距和底部路径对齐。
- [ ] 正常、空列表、缺失文件、处理中和不可用状态可辨识。
- [ ] 成功、确认、警告、备注和 API Key 对话框无截断。
- [ ] 字体使用明确候选字体，图标在窗口、任务栏和文件资源管理器中清晰。
- [ ] 文档截图不包含真实用户名、路径、账号或凭据。

## 成品检查

- [ ] `tools/exe_smoke.py` 在独立临时目录通过。
- [ ] EXE 文件描述、产品名、文件版本、产品版本和图标正确。
- [ ] EXE 内包含应用图标和固定的 DeepSeek 模型目录。
- [ ] 发布文件夹只包含 EXE、说明、校验文件和空的“账号文件”提示目录。
- [ ] 发布 ZIP 可完整解压，CRC 检查通过。
- [ ] EXE 和 ZIP 的 SHA-256 已重新计算并与随附文件一致。
- [ ] 解压后的 EXE 再次通过冒烟测试。

## 安全检查

- [ ] Git 跟踪文件和发布包均不含 `auth.json`、`auth_*.json`、`profiles.json`、`provider_state.dat` 或 `config.toml`。
- [ ] Git 跟踪文件和发布包均不含 API Key、Bearer Token、私钥、个人用户名或本机临时路径。
- [ ] Windows 凭据管理器测试凭据已删除并确认不存在。
- [ ] 临时配置兼容性测试没有修改真实 `%USERPROFILE%\.codex` 文件。
- [ ] 发布说明明确自动重启范围、未代码签名和未执行真实计费请求。

完成后，在 GitHub Release 中记录实际命令、测试数量、EXE/ZIP SHA-256、已知限制和签名状态。
