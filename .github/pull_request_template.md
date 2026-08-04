## 变更说明

说明用户可见变化、实现范围和不在本次处理范围内的内容。

## 安全与回滚

- 是否读取、写入或迁移认证文件/配置文件：
- 失败时的回滚或手动恢复方式：
- 是否新增依赖、进程操作或网络访问：

## 验证

- [ ] `python -m ruff check .`
- [ ] `python -m ruff format --check .`
- [ ] `python -m unittest discover -s tests -v`
- [ ] `python tools/repository_check.py`
- [ ] `build.ps1`
- [ ] 涉及 UI 时已完成 1080×720 与 900×600 视觉检查
- [ ] 涉及打包时已完成 EXE 冒烟测试

## 提交前确认

- [ ] 未提交 `auth.json`、账号副本、API Key、配置、备份或本机用户数据。
- [ ] 未修改或删除与本次变更无关的用户文件。
- [ ] 文档、版本号和用户可见文案与实际行为一致。
