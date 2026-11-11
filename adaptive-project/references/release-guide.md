# 开始、恢复与核验（本地使用）

需要 Python 3.8+ 标准库和本机普通目录。解压后保留完整 `adaptive-project`，无需安装或写入个人技能目录。下面所有脚本路径均相对于**实际解压位置**；可在任意工作目录使用绝对路径调用。

## 开始：先跑有限本地示例

```powershell
python -B -X utf8 'D:/解压位置/adaptive-project/scripts/demo.py' --project 'D:/自己的实验目录/new-demo'
python -B -X utf8 'D:/解压位置/adaptive-project/scripts/audit.py' --project 'D:/自己的实验目录/new-demo'
```

替换两处路径；目标父目录必须已存在，`new-demo` 必须不存在。示例仅创建一个本地合成任务、写文本并验收；不会创建聊天、后台计划或外部行动，不把示例授权复制成真实项目授权。已经存在的目标会拒绝，失败后的部分新目录保留供检查。示例任务完成不代表整个探索项目已关闭。

真实项目按 [章程与命令](runtime.md) 保存真实目标、授权、有限预算，初始化后查看 `plan`。任务 JSON 字段见 [任务操作](tasks.md)，不要复制示例的合成角色或目标到真实授权。新工具没有改变任何运行时语义。

## 核验：只读审计

停止对项目的写入，等待正在执行的 runtime 命令完成，再执行 `audit.py --project PROJECT`。输出 JSON 到 stdout；如需保存日志，请由调用方重定向到项目外的指定位置。工具没有写报告到项目的选项。

| 退出码 | 含义 |
| --- | --- |
| 0 | 所检查范围内未发现异常；不是完整项目验收或授权验证 |
| 1 | 检出自然过期任务租约、已验收物化文件缺失/哈希失配或数据库权威字节异常 |
| 2 | 检查不完整：项目不存在、格式不支持、路径不安全、文件不可读、数据库变化或 SQLite sidecar 等；不得解释为健康 |

异常含 `task_id`；已验收文件异常还含 `file`、`blob_id`、登记/实测 SHA256。`suggested_operation` 只是现有运行时操作片段，不是已经执行的请求。`automatic_actions` 始终为空。

本版本只检查 **schema 4.0、普通本地目录、无活动写入、DELETE journal mode**。拒绝 `-wal/-shm/-journal` sidecar，以免把未提交或 WAL 数据误判成当前状态；先让原运行时正常关闭或按现有恢复程序处理，不能为了通过审计删除 sidecar。对不存在项目不会创建目录或数据库。

脚本不导入运行时，不调用恢复、暂停或任何写事务；SQLite 以 `mode=ro&immutable=1` 打开，前后比较数据库完整哈希。拒绝路径中的符号链接、Windows junction/reparse point、文件多重硬链接、出界或绝对 accepted 引用；不会顺着这些路径读取成果内容。检查不提供对恶意并发替换路径的操作系统级隔离，数据库也不能证明证据语义真实；请在可信、静止的项目上运行。读访问可能影响操作系统访问时间，保证的是不写项目内容、状态或目录项。

## 恢复：授权角色经原运行时操作

1. 查看审计定位和旧尝试、检查点、当前控制/预算。过期不证明旧模型已停止；paused 项目不得因审计建议自行 resume。
2. 过期任务由当前有权协调者/操作员提交 `{"op":"recover","task_id":"审计中的任务ID"}`。恢复后重新读取任务版本，工作者必须新 claim；旧 lease/fence 不可复活。
3. 已验收物化文件缺失或损坏且权威字节完好时，由有权角色提交 `{"op":"artifact.restore","blob_id":"审计中的blob_id"}`。路径被重定向或权威字节异常时先保留证据并调查，不直接照建议写入。
4. 正常操作完成后再次审计，确认原异常不再出现。

完整请求格式：

```json
{"actor_id":"本项目有权角色","idempotency_key":"本次唯一请求键","reason":"根据已核查证据恢复","operations":[{"op":"recover","task_id":"具体任务ID"}]}
```

```powershell
python -B -X utf8 'D:/解压位置/adaptive-project/scripts/runtime.py' --project 'D:/项目' apply --request 'D:/项目外或授权范围内/recover-request.json'
```

前台协调者使用原身份；后台协调业务仍须有效 `wake_id/wake_fence`，操作员也必须遵守无活跃 wake 等规则，见 [后台控制](scheduling.md)。示例不授予恢复其他项目的权限。默认审计永不自动执行上述动作。

## 核查包与复查测试

包根 `SHA256SUMS.json` 列出除该清单本身以外的全部发布文件和 SHA256。它用于传输完整性比较，不是签名或信任来源。可用 Python `hashlib.sha256` 对每个相对路径文件重算；不要把清单当成执行任意路径的授权。

```powershell
python -B -X utf8 'D:/解压位置/adaptive-project/tests/check_audit.py' --output 'D:/自己的实验目录/new-audit-check'
```

测试仅写新的指定目录，保留原始命令、输出和前后文件清单。测试含合成过期租约、受控物化文件损坏/删除、既有 recover/restore、缺失项目与路径边界；不代表真实用户研究。Windows junction 检查在该平台执行，其他平台尝试符号链接；无法创建链接时明确报告 skipped，不能当通过。测试不会改变包或个人安装。
