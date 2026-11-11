# 后台唤醒、暂停与资源预算

统一 runtime.py 管理本地SQLite项目的调度、独占wake、operator控制和累计预算。任务与记忆见 [任务契约](tasks.md)，完整章程见 [运行时](runtime.md)。

运行时本身不创建系统定时器、不调用模型或 Codex 工具。原生 Automation 负责唤醒聊天，协调 agent 先进入本运行时的准入门，再推进获授权工作。以下预算限制**获准的工作**；原生自动化触发时模型已经开始一轮，运行时不能硬限制实际 token、账单或强行中止桌面聊天。暂停宿主自动化和记录真实回执由适配工作流处理。

## 初始化

章程包含 `project_id/objective/acceptance/scope/authority/limits/execution`。后台补充字段示例：

```json
{
  "execution": {"state_writer_id": "coordinator", "operator_id": "operator", "mode": "background"},
  "authority": {"configure_future_triggers": true},
  "scheduler": {
    "budgets": {
      "max_wakes": 6, "max_claims": 2, "max_dispatches": 4,
      "max_actions_per_wake": 20, "max_wake_seconds": 300
    },
    "budget_ceiling": {"max_wakes": 8, "max_claims": 4},
    "cooldown_seconds": 60,
    "max_backoff_seconds": 3600,
    "not_before": 0,
    "expires_at": 1794400000
  }
}
```

这是新增字段示例，不是完整章程或授权。`authority.source_refs` 仍须绑定实际用户指令本地证据；完整 `scope`、目标、验收与其他授权字段按统一运行时章程填写。示例时间戳需要替换为本项目真正期限。

`operator_id` 必须与协调者身份不同。协调者负责自动推进，操作员负责用户发起的暂停、恢复、绑定和预算调整；工作聊天不能占用这两个身份。身份是合作协议，不是操作系统认证，拥有项目文件权限的进程并未被安全隔离。

预算字段全部为正整数。未指定时默认为：唤醒20次、领取30次、外部派发30次、每次唤醒20个业务操作、每次唤醒最长300秒。`budget_ceiling` 默认等于初始预算，允许章程事先授权较高上限；运行期间只能在该不可变上限内调整预算，不能修改上限或清零累计消耗。

`not_before/expires_at` 为 Unix 秒；期限必须有限，省略 expires_at 默认初始化后24小时，不接受 null 或无穷大。cooldown_seconds 可为0；max_backoff_seconds 至少为 max(cooldown_seconds,1)。这些时间策略在本版章程初始化后不可修改。到期后按收尾协议持久prepare再暂停核验宿主；不得自动延长期限。

## API 与 CLI

```text
python -X utf8 runtime.py --project PROJECT init --charter FILE
python -X utf8 runtime.py --project PROJECT status
python -X utf8 runtime.py --project PROJECT plan
python -X utf8 runtime.py --project PROJECT apply --request FILE
python -X utf8 runtime.py --project PROJECT pause --actor OPERATOR --reason "用户要求暂停"
python -X utf8 runtime.py --project PROJECT resume --actor OPERATOR --reason "用户要求恢复"
```

pause/resume 可传 `--key` 指定幂等键。它们读取当前 control 实体版本后提交独立交易，若另一操作员并发改变状态则明确失败，不强行覆盖。status/plan 只读，不计消耗、不产生事件。

Python：`Runtime(project).init(charter)`、`read()`、`plan()`、`transact(request)`。交易返回 `{revision,results,state,replayed}`。失败抛出 `RuntimeErrorDetail`，包含 `code`；CLI 错误为 JSON 与非零退出码。

请求基础格式仍为：

```json
{"actor_id":"operator","idempotency_key":"setup-1","reason":"设置获授权任务","operations":[...]}
```

units/chats/tasks/memories/outbox 均为对象数组，按 id 查找。`control`、`scheduler` 是对象；各实体版本与全局 revision 分开。

## 操作员控制

这些操作必须由 operator_id 执行且各自单独交易：

| 操作 | 字段与效果 |
| --- | --- |
| control.pause | expected_control_revision；立即将 control.status 设 paused，撤销活跃 wake 及全部任务租约，递增令牌，保留检查点和旧尝试 |
| control.resume | expected_control_revision；恢复 running，不恢复旧租约、不清空累计预算、不跳过期限或退避；completed 项目不可恢复 |
| scheduler.bind | expected_scheduler_revision、automation_id、thread_id、host_id、evidence；可选coordinator_outbox_id登记真实协调创建回执，见bootstrap.md；绑定真实宿主回执，可选 enabled 默认为 false；存在活跃 wake 时拒绝替换 |
| scheduler.enable | expected_scheduler_revision、enabled 布尔值；启用前须绑定，禁用时撤销协调 wake，但不等同于暂停所有工作者 |
| budget.set | expected_scheduler_revision、budgets 部分字段；只能在章程 budget_ceiling 内调整，有未到期 wake 时须先暂停或等其结束 |

暂停是项目的持久控制状态，恢复后保留全部累计计数。执行中的代理可能仍在自己旧目录写文件，但旧凭据不能提交；运行时不声称已经终止它的模型推理或进程。操作员接到暂停要求后应先control.pause，再shutdown.prepare、暂停宿主并核验、shutdown.confirm；宿主暂停失败时项目业务门仍关闭，应记录失败并修复宿主状态。

scheduler.enabled 与 control.status 分开：前者控制协调者调度准入，后者控制所有业务操作。仅关闭 scheduler 允许已有工作者在自身有效租约内提交；需要停止整个项目时使用 control.pause。

操作员还可以在**没有未到期 wake**时执行组织设置、验收和检查点等业务操作，但领取和派发仍受累计资源上限限制。操作员不能与活跃自动协调轮次同时改动组织。`coordinator.takeover` 被禁用；协调身份稳定，执行轮次通过 wake 租约与 fence 更替，不把任意 actor 自报身份升级为协调权。

## 自动唤醒准入与退出

协调者首先调用独立交易：

```json
{"actor_id":"coordinator","idempotency_key":"wake-request-unique","reason":"原生自动唤醒","operations":[{"op":"wake.begin","id":"wake-unique"}]}
```

可选 `lease_seconds` 为有限正数，不超过 max_wake_seconds。成功返回 `admitted:true,wake_id,wake_fence,expires_at`。失败返回 `admitted:false,reason,plan`，并持久保存 blocked_reason；不消耗获准唤醒次数。wake.begin 必须单独交易，不能将“准入失败”与后续业务动作塞进同一事务。

只允许一个未到期 wake。过期 wake 在下一次成功准入时归档为 expired，新 wake 获得更大令牌；不撤销仍有效的其他工作者任务租约。每个 wake ID 永久唯一，过期 ID 也不可复用。旧 wake 的成功准入回执重放会重新验证有效性，不能复活过期轮次。

background模式协调者的每笔业务交易必须携带顶层凭据：

```json
{"actor_id":"coordinator","idempotency_key":"work-unique","reason":"推进当前任务","wake_id":"wake-unique","wake_fence":1,"operations":[...]}
```

协调者每个业务操作计入本 wake.actions；整笔交易超限时全部回滚。wake.begin/end、outbox.resolve/reconcile 不计业务动作额度，以便退出和记录已发生的副作用。工作者仍以各自 actor_id 与任务租约调用原任务操作，新增领取计入全项目 claims，正常提交不再次收费。

结束轮次调用独立 `wake.end {outcome:progress|idle|error}`，请求仍带当前 wake 凭据。progress 重置退避连击并使用 cooldown；idle/error 分别进行有上限的指数退避，基数 max(cooldown_seconds,1)，不会无限增长。结束轮次后旧 wake 凭据立即失效。到期后无法续活 wake；读取状态，以新 wake 恢复。

`project.complete` 成功时自动关闭当前 wake、禁用逻辑 scheduler；不需要再 wake.end。协调者随后先shutdown.prepare，再暂停原宿主并核验、shutdown.confirm，最后必要时删除，见 [收尾协议](shutdown.md)。权威成果导出/修复在 completed 后仍允许操作员或协调者执行，无须新 wake。

## 预算含义与只读计划

- wakes：累计成功准入次数，发生错误、空转、暂停或进程退出均不退款；重复同一有效请求不重复收费。
- claims：所有 actor 成功领取尝试的累计次数，恢复重领也计一次；失败的领取事务不收费。
- dispatches：outbox.begin 成功次数，在调用宿主工具前收费。结果不明须对账，不能重放 begin 再调用工具；确认未投递后重新 begin 仍消耗新的派发额度。
- actions：单个 wake 内协调者成功提交的业务操作数；批量请求整体超限回滚。
- max_wake_seconds 与 expires_at：控制后续事务的时间准入，无法强制结束一个已经发出的宿主请求。

`plan()` 返回 decision、reason、remaining（三类累计额度剩余）、not_before、expires_at、active_wake、ready_tasks、review_tasks、unresolved_outbox 和 should_pause_host。decision 可能为 run、paused、completed、unbound、disabled、expired、live、budget、backoff。

全局 wake 额度耗尽时不再准入；claims/dispatches 用尽只禁止相应新动作，仍允许已领取工作者提交、协调者验收和关闭项目，以及 outbox 对账。否则最后一次获准任务将无法结束。适配器应结合 ready/review/outbox 和 remaining 判断是推进收尾、等待现有工作，还是停止新业务并请求操作员处理；暂停宿主前仍须完成全局关闭与prepare，不能反复触发无可执行工作的模型轮次。

should_pause_host仅在存在仍有效的prepared收尾意图时为true。shutdown_protocol_required提示需要处理收尾，不是直接暂停宿主的许可。disabled或预算用尽不证明全局关闭；先检查shutdown_closure_basis，必要时由operator关闭业务准入，再prepare。backoff仅控制事务准入，宿主触发仍可能消耗模型资源。

## 恢复、并发与外部动作

任务与记忆、过期任务 recover、已验收字节、隔离尝试、组织调整和 outbox 沿用 [任务契约](tasks.md)，使用同一项目数据库和路径。过期协调 wake 的接管与过期任务租约恢复是两件事；不要仅因协调者换轮次就抹去仍有效的任务租约。

control.pause 会把尚在途外部派发留在 outbox，不能假定它们从未发生。operator/coordinator 在暂停或没有 wake 时仍可调用 outbox.resolve/reconcile 记录和对账；新 enqueue/begin 被业务门挡住。适配器在真正调用宿主前再次检查项目控制与 wake；检查和外部调用间仍存在不可消除的跨系统竞态，必须依靠 outbox 对账，不能承诺外部动作恰好一次。

所有状态、事件、请求去重记录在 SQLite BEGIN IMMEDIATE 事务中提交。`ADAPTIVE_V4_TEST_FAULT=before_commit` 配合 CLI `--allow-test-faults` 可在提交前真实退出91用于回滚测试，默认关闭。无引用尝试目录或产物可能残留，数据库已提交状态为准，不自动删除。

关键错误码：unauthorized、paused、wake_active、stale_wake、wake_expired、schedule_expired、resource_limit、stale_revision、stale_lease、lease_expired、dispatch_ambiguous。旧回执被拒绝时读取当前状态，不换幂等键强行重复外部动作。授权来源哈希变动会阻止常规变更；紧急操作员 pause 仍可执行。
