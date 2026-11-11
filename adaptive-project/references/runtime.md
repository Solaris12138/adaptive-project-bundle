# 统一运行时、初始化与探索契约

scripts/runtime.py 提供Python标准库SQLite运行时，接口为 Runtime(project).init/read/plan/transact。任务与记忆见 [任务契约](tasks.md)，wake、operator与资源控制见 [调度](scheduling.md)。存储沿用 .adaptive-v4/state.sqlite3 与schema 4.0，避免更名影响既有项目；本包不提供早期格式的自动迁移。

## 初始化与运行模式

最小完整章程示例；先保存真实目标与授权文件，示例不赋予外部行动权限：

```json
{
  "project_id": "explore-example",
  "objective": "填写用户的原始模糊目标，不预定最终方案",
  "acceptance": ["给出有来源的候选比较、验证记录和最终可使用交付，并如实说明限制"],
  "scope": {"project_root": ".", "write_roots": ["."]},
  "authority": {
    "status": "bound", "source_refs": ["inputs/authorization.md"],
    "adjust_units_within_scope": true, "maintain_project_memory": true,
    "create_long_lived_chats": false, "message_long_lived_chats": false,
    "configure_future_triggers": false
  },
  "execution": {"state_writer_id": "coordinator", "operator_id": "operator", "mode": "foreground"},
  "exploration": {
    "budgets": {"max_candidates": 5, "max_validations": 6, "max_cost": 12},
    "budget_ceiling": {"max_candidates": 5, "max_validations": 6, "max_cost": 12}
  },
  "scheduler": {"budgets": {"max_claims": 12, "max_dispatches": 6}}
}
```

`execution.mode` 默认 `foreground`：协调者在当前会话直接推进，不申请 wake，不绑定或启用自动化；`wake.begin/end` 被拒绝。仍检查项目暂停、章程期限、开始时间、任务领取与派发累计额度。`background` 明确启用调度机制的独占 wake 和单轮动作额度；新建后台计划仍需真实用户授权及宿主工具。模式在初始化后不可变。

`scheduler.not_before/expires_at` 沿用调度机制；未提供 expires_at 时为初始化后24小时。foreground 不计 wake 次数，也没有 wake 动作/时长窗口，不应宣称其模型轮次受到这些后台额度限制。两种模式均保留任务租约与并发限制。

探索预算均为正整数。默认 `max_candidates=6,max_validations=8,max_cost=20`；未提供 ceiling 则等于初始预算。`cost` 是项目约定的工作点数，不是 token、钱或真实CPU计量。章程应描述点数含义，并在执行昂贵调查/试验前登记和预留；运行时不能核实代理是否绕过协议先做了工作。

```text
python -X utf8 runtime.py --project PROJECT init --charter FILE
python -X utf8 runtime.py --project PROJECT status
python -X utf8 runtime.py --project PROJECT plan
python -X utf8 runtime.py --project PROJECT apply --request FILE
python -X utf8 runtime.py --project PROJECT pause --actor operator --reason "用户暂停"
python -X utf8 runtime.py --project PROJECT resume --actor operator --reason "按原预算继续"
```

事务格式：`{actor_id,idempotency_key,reason,operations:[...]}`。background 的协调业务另带当前 `wake_id/wake_fence`。探索操作只能由协调者或有权操作员提交；工作者可贡献证据、候选记忆和提案，不能直接改变探索决策。paused 拒绝探索写入，操作员预算设置除外；恢复不清零消耗。

## 状态与版本

`state.exploration` 包含 `revision,status(open|closed),budgets,budget_ceiling,usage,active_candidate_id,questions,candidates,validations,decisions`。最后四项为数组，按 id 查找；不在 state 顶层。`usage={candidates,validations,cost}` 是累计值，取消、失败、暂停与恢复都不退款。

问题、候选、验证各有实体 revision；方向决策使用 `expected_exploration_revision`。事务全失败或全成功，竞争 start 在 SQLite 写事务内核算额度。不同验证可独立按实体版本推进，不因无关问题更新而覆盖彼此。

`plan()` 只读；保留调度机制字段并增加 `execution_mode` 与 `exploration`，后者包含剩余额度、当前方向、未完成验证、开放问题及建议动作 `evaluate_next_probe/collect_results/decide_or_close/deliver`。建议不是自动执行承诺，也不自动证明哪种方向最好。

退出应用后的统一入口见 [重开恢复](restart-recovery.md)。`recovery.py` 只读核对持久状态与原宿主计划。若中断留下 SQLite 日志，需要在确认原写者停止后显式运行 `runtime.py --project PROJECT storage-recover --acknowledge-stopped-writers`；这是物理存储维护，不是工作准入、用户 resume 或预算重置。由 SQLite 完成原 DELETE 日志的恢复与清理，并核对项目原文、请求、产物及逻辑元数据指纹不变；物理数据库字节可能变化，不能称为只读。缺项目、未知格式、WAL、并发写者或完整性失败时拒绝，不创建新数据库、不手删日志。

后台引导还返回 `chat_capacity`：工作上限、活跃工作聊天、创建预留、可用工作名额和协调登记状态。`scheduler.bind` 可带 `coordinator_outbox_id`，将精确匹配的已交付协调创建回执原子登记为控制载体，只结算该一条创建预留；无需额外休眠工作聊天。启用前核对登记，未知/不完整回执先对账。完整顺序、兼容状态和字段见 [通用引导](bootstrap.md)。这是 schema 4.0 内的可选绑定字段，旧项目不自动迁移或重写。

## 问题与候选

| 操作 | 字段与约束 |
| --- | --- |
| question.add | `id,question,importance`；创建 open 问题，说明为什么值得查 |
| question.resolve | `id,expected_question_revision,status,answer,evidence`；status 为 answered 或 deferred，仅可处理 open 问题；证据为非空本地引用列表 |
| candidate.add | `id,title,proposal,assumptions,scope_fit,question_ids`；assumptions、question_ids 为非空文本列表，引用现存问题；累计消耗1个候选额度 |

候选初始 `status=candidate,evidence_status=unvalidated`。只能通过方向决策成为 selected、退回 candidate 或 retired；创建时不能自称验证成功。`scope_fit` 是 agent 给出的目标适配理由，不是运行时对语义授权的证明。

候选 `evidence_status` 根据其已完成验证计算：positive→supported，negative→refuted，两者并存→mixed，仅 inconclusive→inconclusive。它只总结登记结果，不代表目标普遍成立或市场/科学有效性已经验证。所有原始结果保留。

## 预注册验证

| 操作 | 字段与约束 |
| --- | --- |
| validation.plan | `id,candidate_id,hypothesis,method,criteria,cost`，可选 `inputs` 本地文件引用列表；criteria 必须恰有 positive、negative、inconclusive 三项非空文本；cost 为正整数；创建 planned 验证 |
| validation.start | `id,expected_validation_revision`；planned→running，原子预扣1次 validations 和计划 cost；额度不足整笔失败 |
| validation.finish | `id,expected_validation_revision,outcome,summary,limitations,criteria_assessment,evidence`；只能 running→completed；outcome 为 positive/negative/inconclusive；limitations 为可空文本列表；criteria_assessment 必须逐项说明三个预注册判据；evidence 非空 |
| validation.cancel | `id,expected_validation_revision,reason,evidence`；planned/running→cancelled，已经开始的工作不退款 |

同一验证的 plan、start、finish/cancel 必须分开事务持久化；不能在一个提交中补造“先有计划”的历史。plan 从创建后不可编辑，方法、判据、成本、输入和候选形成固定 `plan_hash`，结果关联该哈希。未知字段被拒绝，finish 不能携带 method 或 criteria 覆盖原计划。需改变设计时取消旧验证、建立新 ID；旧记录和消耗保留。

示例操作按三次事务依次提交：

```json
{"op":"validation.plan","id":"probe-1","candidate_id":"direction-a","hypothesis":"填写可被推翻的判断","method":"填写取证/执行方法","criteria":{"positive":"何种观察支持","negative":"何种观察反对","inconclusive":"何时不足以判断"},"cost":2,"inputs":["inputs/brief.md"]}
```

```json
{"op":"validation.start","id":"probe-1","expected_validation_revision":1}
```

```json
{"op":"validation.finish","id":"probe-1","expected_validation_revision":2,"outcome":"negative","summary":"实际观察及结论","limitations":["仅限当前样本"],"criteria_assessment":{"positive":"未达到，说明证据","negative":"达到，说明证据","inconclusive":"未归入此类的理由"},"evidence":["evidence/probe-1.json"]}
```

`inputs` 在 start、finish 时核验哈希；结果和决策证据在后续决策及项目完成时核验。计划时间顺序与文件哈希能约束记录一致性，不能证明实际观察真实、来源可靠或方法科学，仍需独立审查。预算耗尽禁止新计划/启动，但允许收录已开始工作的结果、取消、做决定和交付。

## 方向决策与任务影响

`decision.record` 字段：

```json
{
  "op":"decision.record", "id":"decision-2", "expected_exploration_revision":10,
  "action":"pivot", "candidate_id":"direction-b", "alternatives":["direction-a"],
  "result_ids":["probe-1"], "rationale":"为什么这些观察足以调整方向，仍有什么不确定性",
  "evidence":["evidence/comparison.md"], "task_dispositions":[], "residual_question_ids":[]
}
```

| action | 规则 |
| --- | --- |
| select | 尚无选中方向时暂选；可无验证结果，必须有理由与证据，不宣称已验证 |
| pivot | 必须已有方向、目标候选不同，并引用至少一个已完成验证；至少一个结果与新旧方向相关 |
| retire | 退役一个候选；其计划中/运行中验证必须先结束或取消 |
| deliver | 必须是当前选中方向，引用至少一个属于它的 positive 验证；关闭探索，为后续成果交付收尾 |
| stop | 无须 candidate_id 或正结果；说明不可行、预算耗尽等理由后关闭探索，可交付否定/不确定报告 |

`alternatives`、`result_ids` 为可空且不可重复的 ID 列表；result_ids 只能引用 completed 验证。引用结果的证据以及既有决策证据变更时拒绝新决策，不用旧摘要掩盖源文件变化。决策记录只追加，不可编辑；选择方法不等于修改原始用户目标或验收。

`task.create` 新增可选 `candidate_id`，绑定现存非退役候选。pivot、retire、stop 必须明确处理受影响的旧方向未完成任务，以及这些任务的未完成传递下游。`task_dispositions` 必须精确覆盖影响集合；每项为 `{id,expected_task_revision,action:cancel|retain,reason}`。取消会撤销租约、使旧提交失效；保留记录原因并递增任务版本。已完成任务和成果仍保留，不因转向自动删除。

保留某个依赖已取消任务的下游任务，并不会让其依赖自动满足；需明确取消并新建合适的替代任务。当前实现另提供协调操作 `task.cancel {id,expected_task_revision,reason,evidence}`，可处理无方向绑定的废弃任务，保留历史并撤销旧凭据。不能取消已验收的 done 任务。

deliver/stop 要求无 planned/running 验证，并用 `residual_question_ids` 精确列出仍 open 的问题。answered/deferred 的问题已经单独保存处理说明。关闭探索后禁止新问题、候选、验证和方向决策，但允许任务提交、验收、记忆维护及报告交付。`project.complete` 额外要求探索已关闭，同时仍满足原章程全部验收、任务完成/明确取消、outbox 已对账等条件。正结果不是完成项目的必要条件；如原目标允许，stop 后可交付有证据的否定报告。

## 预算、恢复与边界

操作员独立交易 `exploration.budget.set {expected_exploration_revision,budgets}` 可在不可变 ceiling 内调整部分额度，不能降到累计使用量以下；活跃 background wake 存在时先暂停或等待其结束。协调者不可自行增配。探索关闭后调整预算也不能重新打开项目探索。

暂停撤销任务和后台 wake 凭据，验证记录仍保存 running 状态作为待恢复工作；恢复后先确认实际执行证据与任务状态，再记录结果或取消，不因它仍标 running 就假定某个模型/进程还在运行。重新打开 Python 对象即可读取相同持久状态。旧 start 回执在验证已完成/取消后不可重放为新执行许可。

记忆继续使用 `memory.put/propose/accept/supersede`；探索实体是问题、假设、观察与决策账本，正式项目记忆只收录经核验且对后续有用的部分。不能通过候选、结果或记忆改变用户授权。协议内身份不是操作系统安全隔离，运行时不会自动调用模型或执行验证。精确token/金额限制、真实证据语义核验、任意外部行动许可仍由宿主和授权流程负责。

新增常见错误：`exploration_closed,exploration_open,validation_stage_boundary,validation_pending,invalid_transition,insufficient_evidence,task_impact_unresolved`；版本、证据与预算错误仍为 `stale_revision,stale_input,resource_limit`。

## 持久收尾

后台项目使用 [两阶段收尾](shutdown.md)：先完成或由 operator 暂停项目，再通过独立事务 shutdown.prepare 记录关闭依据和原绑定；原生宿主暂停并实际核对后 shutdown.confirm。仅 scheduler.enabled=false 或局部预算耗尽不是关闭所有工作者准入的证明。恢复先核对未确认收尾意图，不需要业务 wake，也不重置预算。
