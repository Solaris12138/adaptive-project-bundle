# 宿主时间和辅助任务证据

跨宿主恢复需要区分原始观察、可核实结论和未知结果。本页只适用于时间比较或已授权的 Windows 辅助任务核验，不要求普通项目创建系统任务。

## 时间戳

Windows 的 `o` 格式可含七位小数秒，Python 3.8 的直接 `datetime.fromisoformat` 不能解析所有这类值。需要比较原生观察时间时使用安装目录 `scripts/evidence_time.py`，不要在项目中复制截断或替换时间戳的临时解析器。

```text
python -B -X utf8 SKILL/scripts/evidence_time.py interval --start 2026-11-11T16:25:49.2035066Z --end 2026-11-11T16:28:49.2201725+00:00 --minimum 180
```

公共 API `timestamp_epoch(text)` 和 `elapsed_seconds(start, end)` 返回 Decimal 秒。支持带时区的 ISO 时间和纳秒以内的小数；按精确值判断最小间隔，不能先四舍五入再宣称达到阈值。保留原始时间字符串和工具完整输出。没有时区、非法日期/偏移、倒序或非有限阈值均不能通过。epoch 毫秒配置字段仍按原宿主单位换算，不把它们误作 ISO 字符串。

## 精确核验 Windows 辅助任务

只在用户授权具体辅助任务时使用 `scripts/windows_task.ps1`。调用前从本项目真实创建回执取完整任务路径；不搜索删除相似名称，不用此工具替代 Codex 原生 heartbeat 暂停/删除接口。

```powershell
powershell.exe -NoProfile -NonInteractive -File SKILL/scripts/windows_task.ps1 -Action Observe -TaskPath '\ExactTaskName'
powershell.exe -NoProfile -NonInteractive -File SKILL/scripts/windows_task.ps1 -Action Delete -TaskPath '\ExactTaskName'
```

结果区分 present、absent、unknown。只有成功连接任务服务、成功枚举正确父文件夹、精确匹配数量为零，才能得到 `verified_absent=true`。权限错误、连接失败、文件夹或枚举失败、删除失败均不是不存在；保留 unknown、错误阶段与退出码。运行中的任务不得删除；先核对它已结束。删除后必须重新成功枚举，不能用“DeleteTask 没抛错”作为清理完成证明。

捕获每次调用的 stdout、stderr 和退出码，再按脚本的明确状态判定。不要把 PowerShell 非终止错误后的 exit 0、null 对象的方法异常或任意 catch 当作“未找到”。需要既有授权内的权限升级时使用宿主审批，重试同一精确目标并保留前次 unknown；不可改写失败成成功或另建任务绕过。工具不会自行提权、创建任务、修改项目预算或解除暂停。非 Windows 宿主不调用此脚本，应使用其原生可核实接口。

## 试运行引导

若验收要求无需临时修正，正常引导时核对已安装两个公共工具的哈希、当前 Python/PowerShell 和真实观察权限；对无副作用的时间样本与已知精确任务运行前置检查。授权、脚本实现和验收在启用前固定。启用后业务轮次应调用安装工具，不重新发明解析或清理脚本；实际能力缺失保持未知并如实判定未通过，不能事后弱化标准。
