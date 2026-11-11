# Adaptive Project

让 Codex 围绕可检验的目标组织自主探索、职责协作、项目记忆和有预算的恢复。

这是可独立安装的 skill。它提供本地事务运行时和协作协议；模型、长期聊天与后台唤醒由当前 Codex 宿主提供，安装不会自动创建聊天或计划。

## 能力

- 将模糊目标拆成关键问题、候选方向、预先登记的验证和证据决策。
- 按工作需要创建、调整和复用职责单元，支持任务领取、租约、版本冲突和旧凭据拒绝。
- 持久保存项目记忆、检查点、授权、资源预算与真实宿主绑定。
- 在宿主支持且用户授权时，通过原生计划协调长期聊天；应用重开后沿原状态恢复。
- 按业务关闭、准备收尾、宿主暂停核验、确认、删除的顺序完成收尾。

## 安装

需要 Python 3.8+ 标准库和支持本地 skill 的 Codex。克隆或下载本仓库后，在仓库根目录构建到一个尚不存在的目录：

```sh
python -B -X utf8 packaging/build_portable.py --source adaptive-project --output dist/release
python -B -X utf8 dist/release/adaptive-project-bundle/install.py verify
python -B -X utf8 dist/release/adaptive-project-bundle/install.py install
```

安装默认位置是 `~/.agents/skills/adaptive-project`。若已有不同内容，安装器会拒绝覆盖；先将旧技能备份到技能发现目录之外，再安装。macOS/Linux 可以将 `python` 替换为 `python3`。

在 Codex 新聊天中输入：

> 使用 $adaptive-project。先定位安装目录，运行 scripts/doctor.py，核对当前宿主的子代理、长期聊天和原生调度能力。先不要创建后台计划。

完整安装、迁移和项目启动示例见 [安装指南](packaging/README.md)。

## 使用示例

> 使用 $adaptive-project 完成这个项目：[目标]。交付标准：[验收要求]。允许在项目范围内自主探索、调整职责、委派子代理和维护记忆。最多探索 3 个候选、进行 4 次验证，探索成本上限 6，任务领取上限 8。先明确成本单位和具体截止时间、登记授权与章程，再执行并交付证据和未解决问题。

若需要后台推进，另行明确允许的长期聊天数量、唤醒及派发额度和截止时间。技能会核对实际宿主能力，不将前台执行描述为后台运行。

## 验证

在仓库根目录运行：

```sh
python -B -X utf8 -m unittest discover -s tests -p "test_*.py"
python -B -X utf8 packaging/check_portable.py --archive dist/release/adaptive-project.zip --output work/portable-check
```

Windows 的任务观察/清理工具可进行纯模拟测试，不操作真实系统任务：

```powershell
powershell.exe -NoProfile -NonInteractive -File tests/test_windows_task.ps1
```

检查输出目录必须尚不存在。实测结果及边界见 [验证说明](VALIDATION.md)。

## 目录

| 目录 | 内容 |
| --- | --- |
| `adaptive-project/` | 可安装的技能、协议、运行时和只读核验工具 |
| `packaging/` | 可迁移包构建、完整性校验和安装器 |
| `tests/` | 开发回归测试与合成夹具 |

## 范围

核心 Python 代码只依赖标准库。可选 `windows_task.ps1` 需要 Windows、PowerShell 5.1+ 和对应任务访问权限；它只操作明确授权的精确辅助任务，不替代 Codex 原生计划工具。

工作次数预算不等于精确 token 或金额上限。安装不迁移活动项目、账户、聊天或计划绑定，不解除已有暂停。当前真实中断验证覆盖应用退出和重开；没有验证真实关机、断电或多设备同时写入同一数据库。宿主功能是否可用须在目标设备核验。
