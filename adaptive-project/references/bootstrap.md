# 后台项目的通用引导

用于已获授权的后台项目：一个协调聊天及有限长期工作聊天。沿用 [后台调度](background-scheduling.md) 的暂停与预算规则；本流程不授予会话创建、消息派发或未来调度权限。

## 容量与身份

`max_active_chats` 限制活跃工作聊天及尚未完成登记的创建预留。后台协调载体由 `scheduler.binding` 独立登记，不需要伪装成休眠工作聊天，也不能借此扩充工作者额度。只有对应已交付、职责为 `coordinator`、host/thread 精确匹配调度绑定的那一条创建回执，才能解除协调载体的创建预留。

未确认、执行中、指向其他目标或属于其他职责的创建仍占预留。保存真实宿主 ID；标题、计划使用的 ID 和排队创建 ID 都不能替代已就绪的 thread ID。运行时无法核实宿主回执语义，调用者仍必须观察真实工具结果。

## 验收阶段与依赖预检

初始化前逐项拆解用户的总验收，保存原文来源、每项对应的业务证据和收尾证据，不能把总验收整段复制成 `charter.acceptance`。该字段是 `project.complete` 的前置条件，只能包含提交该事务之前已能核实的业务成果，例如报告已验收、权威字节已导出、恢复证据和用量保持已核对。

`project.complete` 本身、业务准入关闭、`shutdown.prepare`、宿主暂停核验、`shutdown.confirm`、删除与临时调度清理都是后续条件，必须放在另行持久保存的整体协议清单，例如 `inputs/protocol-acceptance.json`。这是项目审计文件，不是新增的运行时字段；把它列入章程 `authority.source_refs`，使工作期间原要求及映射不可悄悄改写。把 `project.complete` 的执行结果作为它自身的前置验收，同样会产生循环。

最小清单示意：

```json
{
  "original_requirements": ["交付报告", "完成项目并按顺序停止调度"],
  "business": ["报告已验收并导出"],
  "post_completion": ["project.complete已提交", "prepare后宿主暂停已核验", "shutdown.confirm已提交", "需要的宿主和辅助调度清理已完成"],
  "source_mapping": {
    "交付报告": ["报告已验收并导出"],
    "完成项目并按顺序停止调度": ["project.complete已提交", "prepare后宿主暂停已核验", "shutdown.confirm已提交", "需要的宿主和辅助调度清理已完成"]
  }
}
```

按实际需求替换示意项，混合条目拆成前置与后置子项并保留同一原始来源，不删除其中任何要求。外层整体审计必须核对原要求映射完整、业务与后置证据全部通过、真实时序正确；缺失、未知或无证据不能当作通过。运行时 `project_status=completed` 只证明业务阶段完成，不等于整体协议已通过。

启用宿主前作依赖检查：每一项 `charter.acceptance` 的证据都应能在不调用 `project.complete` 及后续收尾操作的情况下取得；后置清单必须仍覆盖用户要求的完整收尾。若已有不可变章程出现循环，保留失败与原章程，不修改验收或预填成功来补过；停止业务并按授权安全关闭，修正供未来新项目使用的引导。

## 新项目

1. 保存真实目标、授权、业务前置验收与整体后置清单及原需求映射、写入范围、工作聊天数量、探索与后台预算、有效期；确认验收依赖无环后初始化独立 background 项目。区分 operator 和 coordinator 身份。
2. 将协调聊天创建意图 `outbox.enqueue` 为 `create_chat`，`payload.unit_id` 使用 `coordinator`；`outbox.begin` 后调用一次原生创建工具。初始提示只要求读取协议、核查无 wake 无工作权、保存 READY 并结束，不提前开展业务。
3. 用真实工具返回的 host/thread 将创建记录 `outbox.resolve` 为 delivered。等待初始普通轮次完成，保留宿主回执。未知结果先对账，不重复创建。
4. 新建暂停态原生 heartbeat，核对其目标就是该协调聊天，保存真实自动化 ID 与暂停状态。通过 `scheduler.bind` 同时登记调度与协调创建记录，明确传入该 `coordinator_outbox_id`，避免多个相似记录歧义。
5. 重新读取状态与只读 `plan`，核对该协调载体已经登记、其创建预留已解除、剩余工作容量与原预算一致；不能通过调大限额使检查通过。
6. 提交 `scheduler.enable`，再启用同一原生计划。第一轮业务由原生 heartbeat 进入后申请 wake；在正常工作容量内自主创建、绑定和派发职责聊天。

调度绑定操作示例，所有占位值必须换成真实已核验回执：

```json
{"op":"scheduler.bind","expected_scheduler_revision":1,"automation_id":"真实自动化ID","host_id":"真实宿主ID","thread_id":"真实就绪协调聊天ID","coordinator_outbox_id":"协调创建意图ID","evidence":["control/native-paused-receipt.json"]}
```

按当前状态填写实体版本，外层事务使用该项目 operator、唯一幂等键和具体原因。绑定和启用不是预算重置操作。自动化启用前的配置是正常引导；启用后再手工补登、改额度或发送“继续”不能计为无人工修正试运行。

`plan.chat_capacity` 返回 `limit, active_workers, creation_reservations, available_workers` 和 `coordinator_registration: {status, outbox_id}`。新建协调聊天后应观察到 `status=registered`、outbox_id 等于真实创建意图、该条预留已结算；尚无工作聊天时 available_workers 应等于原 limit。`unregistered/unresolved` 会阻止后台计划准入；已有宿主而无本项目创建记录可标为 external，旧版本已有聊天登记可标为 legacy_chat_registered，二者不是本次新建登记的验收替代。

省略 coordinator_outbox_id 时，唯一精确匹配可自动登记；多条匹配则拒绝歧义，应核对后明确选取真实记录，其他创建仍保留预留。重复绑定同一目标保留原固定记录。未确认登记或歧义常见错误为 dispatch_ambiguous，显式 ID/目标不符为 invalid_request，容量不足仍为 resource_limit。

## 中断与恢复

退出应用或重启电脑后，复用原生计划及原线程。将 [重开与中断恢复](restart-recovery.md) 中的持久恢复入口写入该计划提示；每次触发先运行 `recovery.py --project PROJECT`。它检查当前数据库、原绑定和本机计划配置，不把重开视为 resume 指令，不补造聊天或重置预算。宿主不可用期间不工作；恢复触发时间以原生计划实际执行为准。

每个宿主调用前记意图，调用后先保存原始结果，再提交状态登记。恢复时先读已有 scheduler binding、outbox、宿主状态和检查点；查询确认已有聊天/计划后复用它们。绑定已完成时读取已有登记，不为同一协调载体再加工作聊天绑定；重复同一幂等请求不会再增加派发消耗。

创建已返回但状态尚未确认时，以真实宿主查询补全对应 outbox；绑定未完成则按同一原生目标完成登记。存在歧义、unknown 或不匹配时保持原生计划暂停，说明具体缺口。切换目标须明确重新核验；旧的未登记创建不能悄悄从容量账本中消失。

运行后每轮先 plan、再申请 wake，恢复正式记忆、任务、检查点与未确认派发。没有新结果时结束轮次，按响应需要调整同一计划间隔或有界退避；不能自建替代计划或加配额。将 [收尾协议](shutdown.md) 同样写入持久提示：完成验收与导出后 project.complete，shutdown.prepare 登记关闭依据，再暂停并核对原生计划，shutdown.confirm 后才按宿主要求删除。启动前核查提示中没有“先暂停以便继续收尾”的步骤。原生调用失败保留 pending 意图，恢复时只继续管理收尾，不重新领取业务。

## 验证边界

确定性验证应包含：原故障复现、恰好两个工作聊天可创建、第三个被拒绝、未知结果仍预留、其他 host/thread 不豁免、重复绑定/进程重建不重复扣费、并发竞争不超额。真实试运行另需普通引导回合结束、真实 heartbeat、两个工作聊天实际领取并行、后继任务复用记忆、交付和宿主停止的证据。

宿主执行、本地事务和模型推理不是一个原子系统。通过本流程不证明强杀进程、关机或应用退出后的恢复；预算也不是精确 token 或金额上限。

## 宿主证据能力预检

涉及跨宿主时间比较或 Windows 辅助任务清理时，启用前按 [宿主证据工具](host-evidence.md) 核对公共脚本与观察权限。无需临时修正的试运行应固定已测试的实现、完整验收与有限预算；启用后若仍需改解析或清理逻辑，保留证据并将该项判为未通过。
