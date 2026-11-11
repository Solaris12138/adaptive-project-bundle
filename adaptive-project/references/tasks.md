# 职责、任务、记忆与事务

`scripts/runtime.py` 使用 Python 3.8 标准库与本地 SQLite，多进程共享同一项目数据库 `.adaptive-v4/state.sqlite3`。不需要常驻服务器；它不调用 Codex、不联网，也不自行创建聊天。Codex 协调者负责真实会话操作，运行时负责事务、租约、版本和持久状态。

## API 与命令

```text
python -X utf8 runtime.py --project PROJECT init --charter FILE
python -X utf8 runtime.py --project PROJECT status
python -X utf8 runtime.py --project PROJECT apply --request FILE
```

Python 接口：`Runtime(project).init(charter)` 返回初始状态，`read()` 返回当前状态，`transact(request)` 返回 `{revision, results, state, replayed}`。错误为 `RuntimeErrorDetail`，包含 `code`；CLI 错误输出 JSON 并返回非零退出码。

章程字段与完整初始化示例见 [运行时入口](runtime.md)。必须绑定真实授权来源、项目根与写入根；组织、记忆、长期聊天动作检查相应授权。资源限制字段：`max_nonretired_units=8`、`max_active_chats=6`、`max_running_leases=3`、`max_attempts_per_task=3`、`max_unit_depth=3`、`max_lease_seconds=1800`，未设置时使用所示默认值。

初始化使用 [统一章程示例](runtime.md#初始化与运行模式)，先绑定真实授权并指定执行模式、操作员与有限预算。

路径 `.` 相对于 `--project` 指定的项目目录。调用 init 后，状态中的 units、chats、tasks、memories、outbox 均为对象数组；按各对象 id 查找，不能当成以 id 为键的字典。

交易请求：

```json
{"actor_id":"coordinator","idempotency_key":"unique-request","reason":"说明原因","operations":[{"op":"unit.create","id":"design","name":"设计","mission":"形成方案","creation_reason":"独立专业职责","exit_condition":"方案验收"}]}
```

可选 `expected_revision` 检查全局版本，仅适合需要整个快照一致的组织事务。不同工作者的任务操作通常只传 `expected_task_revision`，使互不相关任务能并发提交。相同幂等键与相同请求重放返回原操作结果并标 `replayed=true`；内容不同拒绝。租约类旧请求重放也重新检查租约，不能把已过期的领取结果伪装成有效。

## 协调者操作

除特别说明，由协调者或契约允许的操作员执行；后台协调业务必须携带有效wake，操作员不能与未到期wake并发改组织。coordinator.takeover禁用，身份不会因自报接管而改变。已有单元初始 ID 为 `coordinator`。

| 操作 | 必需字段与行为 |
| --- | --- |
| unit.create | id, name, mission, creation_reason, exit_condition；parent_id 默认 coordinator；创建 active 单元 |
| unit.update | id, expected_unit_revision；可选 name, mission, exit_condition；不修改身份 |
| unit.status | id, expected_unit_revision, status（active/dormant/retired）；不能休眠有 running 任务的单元，退役前转移未完成任务与子单元 |
| unit.merge | source_id, target_id, expected_source_revision, expected_target_revision；迁移未完成任务及聊天绑定；源退役；有运行任务时拒绝 |
| chat.bind | id（逻辑工作者 actor_id）, unit_id, host_id, thread_id；真实工具返回 ID；可选 title；一个真实聊天只允许一个未退役绑定 |
| chat.status | id, expected_chat_revision, status（active/dormant/retired）；有运行租约时拒绝休眠或退役 |
| task.create | id, title, owner_unit_id, acceptance；可选 inputs, dependencies；依赖未完成时 blocked，否则 ready |
| task.reassign | id, expected_task_revision, owner_unit_id；运行任务可传 revoke=true 使旧租约失效，未提交输出仍隔离在旧尝试目录 |
| task.review | id, expected_task_revision, verdict（pass/rework）, evidence, method；检验输入与提交物未变，pass 时复制到独立 accepted 目录并在数据库中保存权威字节 |
| recover | 扫描过期租约并重新排队；可选 task_id 只恢复某任务；不会认定旧进程已经停止 |
| project.complete | evidence, acceptance_results；格式为逐条 `{criterion,passed:true,evidence:[...]}`，所有任务必须结束、探索必须关闭，全部当前业务验收有证据；完整后置验收另按收尾协议核验 |
| artifact.export | blob_id, output；将已验收数据库字节导出为项目授权范围内的新文件；已有文件须字节相同，禁止覆盖状态、任务输入、提交物或证据 |
| artifact.restore | blob_id；只修复该已验收产物原有 accepted 物化路径，数据库字节与登记哈希必须一致，拒绝路径被符号链接重定向 |

`inputs` 可以是 `{"ref":"inputs/brief.md"}` 或 `{"memory_id":"constraint-1","revision":1}`。所有引用本地且位于项目目录；文件与记忆来源在领取、提交、验收时核验哈希。目标及授权在本版不可修改，避免把工作者提案升级为授权。

拆分通过一个事务创建新单元，再 `task.reassign` 转移指定任务；事务内任何一步失败全部回滚。职责绑定的变动不隐含真实聊天已归档；宿主操作和数据库记录应分别核实。

artifact.export/restore 由协调者或操作员调用，项目 completed 后仍可执行这两类维护操作，不会重开已完成工作。文件物化不与数据库跨介质原子提交：进程若在写文件后、数据库 COMMIT 前退出，文件可能已存在，但数据库不会出现半条事件。再次导出相同字节或恢复相同 canonical 路径是安全的；写入不同已有文件仍会拒绝。发生调用结果不明时先核对磁盘哈希与数据库请求记录。

## 工作会话操作

工作者 actor_id 必须是已登记 active 聊天绑定，所属单元必须 active，只能领取本单元任务。协调者也可以领取 coordinator 单元任务。

| 操作 | 必需字段与行为 |
| --- | --- |
| task.claim | id, expected_task_revision；可选 lease_seconds（默认600）；返回 task_id、attempt_id、lease_id、fencing_token、expires_at、task_revision、attempt_dir |
| task.heartbeat | id, lease_id, fencing_token；可选 lease_seconds；续租不改变任务内容版本，已过期租约不能复活 |
| task.checkpoint | id, lease_id, fencing_token, data（JSON对象）；保存工作者恢复资料，数据不是新授权 |
| task.submit | id, lease_id, fencing_token, expected_task_revision, artifacts, summary；只接受当前有效租约及其独立 attempt_dir 内的文件，转为 review |
| memory.propose | id, kind, statement, sources；可选 scope、confidence；由工作者提出 candidate 条目 |

每次 claim 分配新尝试目录 `.adaptive-v4/attempts/TASK/FENCE-UUID/`。工作者只写这个目录。恢复后的领取使用更大的 fencing token 和新目录，旧进程即使继续写旧目录也不能覆盖新成果。

`task.submit` 后租约结束，重复同请求可以确认已提交结果；一旦任务返工、重领或被转移，旧提交重放拒绝。心跳和检查点必须在租约有效期内完成；正常任务需定期续租。到期应停止提交并等待恢复，不能修改数据库时间来绕过检查。

## 记忆与检查点

协调者使用 `memory.put`（字段同 propose）写 active 记忆，或 `memory.accept`（id, expected_memory_revision）将 candidate 核验为 active；`memory.supersede`（old_id, new_id, expected_old_revision, expected_new_revision）保留旧历史并标记使用它的任务受影响。所有记忆均保存来源文件哈希。

`checkpoint`（id, data）供协调者保存项目级检查点。工作者恢复信息保存在每个尝试的 checkpoints 中。`read()` 包含单位、聊天、任务、记忆、事件、outbox、检查点和当前协调身份；它是派生读取视图，不得手工写回数据库。

## 真实会话操作与 outbox

| 操作 | 必需字段与行为 |
| --- | --- |
| outbox.enqueue | id, kind（create_chat/send_message）, payload；创建请求带 unit_id；发送请求带 chat_id；保存待调用工具的内容 |
| outbox.begin | id, expected_outbox_revision；pending → inflight，记录调用即将发生 |
| outbox.resolve | id, expected_outbox_revision, status（delivered/unknown）, result；创建成功结果必须含 host_id/thread_id；不自动建立 chat.bind |
| outbox.reconcile | id, expected_outbox_revision, resolution（delivered/not_sent）, evidence；delivered 要有 result 中真实 host_id/thread_id；仅 confirmed not_sent 可回 pending |

inflight/unknown 请求禁止自动再次 begin，防止接口返回不明时重复创建聊天。协调者先使用宿主列表或状态工具对账，将真实结果记录后再绑定。已投递的同一请求不再重发。日志记录幂等 ID，不承诺宿主工具恰好执行一次。

重复 outbox.begin 的原请求会明确拒绝为 `dispatch_ambiguous`，即使原始请求后来已投递；适配器必须读取状态或对账，而不能把旧响应当成再次调用宿主工具的许可。确认 not_sent 后回 pending，使用新的幂等键发起 begin。创建请求预留聊天额度，已投递但尚未绑定的请求仍占用预留额度，直接绑定聊天不能挤占这些额度。派发前重新核验目标仍为 active。项目不能在仍有 pending、inflight 或 unknown 请求时标记完成。

## 可靠性边界与故障注入

SQLite 使用 BEGIN IMMEDIATE、busy_timeout 和事务提交；事件、任务更新和请求去重记录在同一事务中。`ADAPTIVE_V4_TEST_FAULT=before_commit` 配合 CLI `--allow-test-faults` 可在 COMMIT 前以91退出，用于验证事务回滚和锁释放。测试开关默认关闭。

本实现针对同一机器的本地数据库，不用于网络共享盘上的分布式写入。actor_id 是合作身份，不构成安全认证；拥有原始文件系统写权限的人可以绕过运行时。accepted 字节在数据库内保存，物化文件仍有哈希核验；工作者改变旧尝试文件不会改变已验收的独立副本。运行时不停止旧聊天进程，也不自行运行后台恢复；协调者每次调度前运行 recover，未来可由已授权调度器调用。

数据库事务回滚或进程退出后，文件系统可能留下未被已提交状态引用的尝试目录或 accepted 文件；这些不是已交付结果，恢复时以数据库为准。本版不自动删除这些目录。accepted 文件被直接篡改或移除会被哈希检查发现，需要受控地从数据库权威字节恢复；不能以重做已完成任务代替核验。进程崩溃测试不等于断电持久性测试。
