# 发布验收清单

每次正式发布必须从干净的 Git 提交执行本清单。不得使用包含真实账号文件、API Key 或用户配置的目录制作发布包。

## 版本与来源

- [ ] `app.py`、`version_info.txt`、`CHANGELOG.md`、`SECURITY.md`、随包说明和发布文案的版本/维护范围一致。
- [ ] DeepSeek 官方 Codex 接入页面仍明确支持精确 slug `deepseek-v4-flash` 与 `deepseek-v4-pro`、Responses 配置字段和 Codex 0.144.0+ 要求。
- [ ] 所有文档都把 `model_reasoning_effort = "max"` 明确标为本程序对 Flash/Pro 的统一策略，而不是 DeepSeek 官方强制要求。
- [ ] `assets/deepseek_models.json` 来源、固定 SHA-256 和 `THIRD_PARTY_NOTICES.md` 一致，且同时包含两个开放模型的必要元数据。
- [ ] 发布目录使用 `Codex账号切换器-v1.5.0`，ZIP 与 ZIP 校验文件分别使用 ASCII 名 `CodexAccountSwitcher-v1.5.0.zip` 和 `CodexAccountSwitcher-v1.5.0.zip.sha256.txt`。
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
- [ ] Codex 刷新同一 ChatGPT 账号后，列表仍标记当前账号，切换前自动保存新认证并生成更新备份。
- [ ] 当前账号就是目标账号时，不会用旧副本覆盖 Codex 已刷新的文件。
- [ ] 同一 ChatGPT 身份重复、账号身份错配、跨 ChatGPT/API Key 模式更新和副本外部修改均安全停止。
- [ ] 自动同步的预期旧哈希比较能阻止较新副本被旧认证覆盖；备份后外部改写也会中止并保留最新文件。
- [ ] 两个程序进程不能同时进入账号或模型服务切换事务；两类切换共用目标锁，锁超时发生在停止 Codex 或修改文件之前。
- [ ] 自动同步期间源文件变化、清单写入失败和目标切换失败均恢复正确状态，提示不包含账号 ID 或令牌；同步成功但后续失败时列表和提示反映部分成功。
- [ ] 账号导入、更新、移除、重命名与清单修复使用 DPAPI 加密的 `.codex-account-switcher-account-transaction.dat`；共享事务引擎的首写前、部分写入、最后写入后和恢复再次中断边界均能确认提交或回滚，第三态不覆盖，日志明文不含认证内容。
- [ ] 账号事务日志中途被删除时，只补偿仍严格等于本次 replacement 的自有写入；外部重建的认证副本不被旧内容覆盖。损坏/超限/错误目录日志失败关闭，旧 `.remove` 墓碑按有效清单安全收敛。
- [ ] 旧认证结构和 API Key 账号保持读取、添加和切换兼容，不错误进入 ChatGPT 自动同步；无法确认身份的旧 ChatGPT 记录拒绝跨格式手动覆盖并提示重新添加。
- [ ] 目标认证在备份窗口发生变化时不被覆盖；回滚前目标再次变化时拒绝回滚并保留新登录。
- [ ] OpenAI 账号切换与 Store 客户端重启。
- [ ] 从 OpenAI 分别切换到 V4 Flash 和 V4 Pro；生成配置使用精确目标 slug、固定模型目录、`wire_api = "responses"`，并对两个模型统一设置 `model_reasoning_effort = "max"`。
- [ ] V4 Flash → V4 Pro → V4 Flash 可直接双向切换，当前模型、所选模型、按钮文案和成功提示始终一致，原 OpenAI 恢复基线不变。
- [ ] 当前模型与所选模型相同时不执行无意义的配置重写或客户端重启。
- [ ] 在 V4 Flash 和 V4 Pro 下分别更新 API Key；操作在锁内保持当前实际模型，不受尚未应用的界面选项影响，且不改变 OpenAI 恢复基线。
- [ ] V4 Flash 和 V4 Pro 均可安全切回 OpenAI；两种 DeepSeek 模式下均可直接选择 OpenAI 账号并完成恢复、认证替换和重启。
- [ ] 非白名单 slug 在停止 Codex、保存凭据或写配置前被拒绝；当前 DeepSeek 模型未知或缺失时禁止切换模型/更新密钥，只在存在可信 OpenAI 备份时允许恢复。
- [ ] `provider_state.dat`、`models.json` 和 `config.toml` 的预期旧内容比较覆盖写入前、逐文件写入和写后变化；普通文件 API 的最终 TOCTOU 残余已记录，冲突时停止覆盖，回滚不会覆盖较新的外部内容。
- [ ] provider 事务在目标文件变更前把 DPAPI 加密的全局日志写入 `CODEX_HOME`；日志不泄露明文 API Key，只接受 config/models/state 固定目标并绑定原始 state 路径。
- [ ] provider 共享事务引擎覆盖 journal 首写前、部分写入、最后写入后、恢复再次中断和日志中途消失等关键边界；OpenAI → Flash/Pro、Flash ↔ Pro、更新 API Key、DeepSeek → OpenAI 分别验证业务不变量，新实例不会丢失 OpenAI 基线或留下无法解释的半提交文件。
- [ ] 在最后一个目标已写入但日志尚未删除时，恢复器识别 all-after 并确认提交；事务前/事务后混合时逆序回滚；全部事务前状态只清理日志。
- [ ] 在恢复第一个或中间目标后再次硬退出，后续实例仍可幂等完成恢复；连续恢复不会重复降级或改写已确认状态。
- [ ] changed target 和 no-op guard 出现第三态时不覆盖外部内容；仍有未收敛实际变更时日志保留且状态读取/激活/恢复失败关闭，所有 changed targets 已回到事务前状态时允许不覆盖 guard 地安全清理日志。
- [ ] 日志信任边界的代表场景均有明确失败语义：密文/DPAPI、版本或操作、目标集合、原始内容编码、摘要和容量；损坏日志不会被静默删除。
- [ ] 通用有限读取器验证恰好上限、超限一字节和读取期间增长；replacement 与受保护 state/journal 在第一个目标写入前预检容量，最大合法 baseline 不截断。
- [ ] 不同便携副本共享同一 `CODEX_HOME`、但 `provider_state.dat` 路径不同时，非原始副本拒绝自动恢复；返回原始副本可恢复，相关提示明确要求先备份且禁止手删日志/state。
- [ ] DeepSeek 模型切换或密钥更新失败时，配置、模型目录、恢复状态和已变化的 Windows 凭据按实际完成阶段安全补偿；仅 Codex 自动启动失败时保留已成功提交的配置并提示手动启动。
- [ ] v1.4.0 已有受管 Flash/`high` 恢复状态保持兼容，可迁移到 Pro/`max`、更新密钥并恢复 OpenAI，不丢失原基线；固定 legacy fixture 的指纹不被无意改写。
- [ ] v1.4 全局 Windows 凭据只由规范默认 `%USERPROFILE%\.codex` 复制到 home-scoped 目标；复制与复读验证后写入独立迁移标记，旧目标保留且不会被并行 v1.4 写入竞态误删，删除 scoped 值后也不会从旧值复活。
- [ ] 自定义 `CODEX_HOME` 不认领或删除 v1.4 全局凭据，只在活动 DeepSeek 配置受管且无冲突时从实际配置对账；OpenAI 模式下保存值保持为下一次激活偏好。
- [ ] 在“写入 Windows 凭据后、创建 provider journal 前”及 provider 各阶段硬退出后，重新启动会以受管、无冲突的活动 DeepSeek 配置为实际 key 权威收敛凭据；不会让界面保存值与 Codex 活动值长期分叉。
- [ ] 非法 JSON/TOML、文件缺失、权限错误和外部配置冲突提示明确。
- [ ] 写入或启动失败时的回滚与手动恢复文案符合实际状态。

真实 API Key 的联网调用可能计费，不属于默认发布验收。若确需端到端调用，必须使用专用测试密钥并在完成后立即撤销。

## 视觉检查

- [ ] OpenAI 页与 DeepSeek 页在 1080×720 完整显示。
- [ ] OpenAI 页与 DeepSeek 页在 900×600 完整显示。
- [ ] V4 Flash / V4 Pro 选择态、当前实际模型、OpenAI 状态和处理中状态由两种窗口尺寸截图验证；未知模型、配置冲突、外部备份和凭据错误状态由组件测试及人工构造状态检查验证，均准确且可辨识。
- [ ] 选择模型、同服务切换、更新密钥和切回 OpenAI 的确认/成功/失败文案无截断，并显示正确目标模型。
- [ ] 表格首列、行高、列宽、按钮比例、卡片边距和底部路径对齐。
- [ ] 正常、空列表、缺失文件、处理中和不可用状态可辨识。
- [ ] 成功、确认、警告、备注和 API Key 对话框无截断。
- [ ] 字体使用明确候选字体，图标在窗口、任务栏和文件资源管理器中清晰。
- [ ] 文档截图不包含真实用户名、路径、账号或凭据。

## 成品检查

- [ ] `tools/exe_smoke.py` 在独立临时目录通过。
- [ ] EXE 文件描述、产品名、文件版本、产品版本和图标正确。
- [ ] EXE 内包含应用图标和固定的 DeepSeek 模型目录；目录同时包含 `deepseek-v4-flash` 与 `deepseek-v4-pro`。
- [ ] 发布文件夹只包含 EXE、说明、校验文件和空的“账号文件”提示目录。
- [ ] 发布 ZIP 可完整解压，CRC 检查通过。
- [ ] EXE 和 ZIP 的 SHA-256 已重新计算并与随附文件一致。
- [ ] ASCII ZIP 文件名、ZIP 校验文件中的文件名和 GitHub Release 上传资产名完全一致。
- [ ] 解压后的 EXE 再次通过冒烟测试。

## 安全检查

- [ ] Git 跟踪文件和发布包均不含 `auth.json`、`auth_*.json`、`profiles.json`、`provider_state.dat`、`.codex-account-switcher-provider-transaction.dat`、`.codex-account-switcher-account-transaction.dat` 或 `config.toml`。
- [ ] Git 跟踪文件和发布包均不含 API Key、Bearer Token、私钥、个人用户名或本机临时路径。
- [ ] Windows 凭据管理器的旧全局测试目标和所有 home-scoped 测试目标均已删除并确认不存在。
- [ ] 临时配置兼容性测试没有修改真实 `%USERPROFILE%\.codex` 文件。
- [ ] 发布说明明确 Codex 0.144.0+ 要求、`max` 属于本程序策略、自动重启范围、provider/账号日志及跨资源恢复边界、不同便携副本限制、未代码签名和未执行真实计费请求。

完成后，在 GitHub Release 中记录实际命令、测试数量、EXE/ZIP SHA-256、两个开放模型的官方复核结果、已知限制和签名状态。
