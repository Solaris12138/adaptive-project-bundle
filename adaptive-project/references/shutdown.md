# 两阶段收尾

业务准入必须先于宿主计划关闭。正常完成顺序固定为：验收、导出、`project.complete` 成功提交 → 独立 `shutdown.prepare` → 对原绑定调用原生工具设为 `PAUSED` 并查询核对 → 独立 `shutdown.confirm` → 如原生宿主要求删除已结束计划，再删除原绑定并核对、保存回执。用户暂停则以操作员 `control.pause` 成功提交开始；不得为收尾解除暂停。尚未完成的业务不得为了减少触发而提前暂停宿主。

`scheduler.enabled=false`、唤醒额度耗尽、宿主已暂停都不是全局业务关闭依据，执行代理仍可能持有租约。仅项目完成、用户暂停或整个项目 `expires_at` 到期可以准备收尾。到期允许保留旧凭据记录，因为所有业务请求已受全局期限拒绝；这不表示杀死仍在推理的模型。关闭完成后不得再接受、提交或推进业务；导出应在完成前完成，完成后的只读检查/维护必须明确区分业务推进。

## 业务完成与整体通过分开验收

`project.complete` 要求 `charter.acceptance` 全部真实通过，因此这些条目必须在该事务提交前可验证。不要把该事务自身已提交、随后宿主暂停/确认/删除或辅助调度清理放进它的前置条件，否则会出现“收尾需要先完成，完成又需要先收尾”的循环。初始化前按 [通用引导的验收阶段](bootstrap.md#验收阶段与依赖预检) 持久保存原要求映射与后置协议清单，并绑定到授权来源。

业务报告可以写明“业务验收通过，行政收尾待核验”，不能预写完整成功。完成业务并关闭准入后，继续按本页顺序核对后置清单；原始总要求的任何后置项未完成、失败、未知或缺乏证据时，整体结果仍不得通过。不能以拆分阶段为由降低用户的最终验收标准。

已固化章程有循环时，不可改它或把尚未发生的结果填为 true。保留真实拒绝、已完成成果和失败结论；按原授权进行安全关闭，并明确操作员介入不能算无人工修正的成功试运行。

## 事务契约

操作员或协调员可提交下列独立事务；无需新 wake，不消耗 wakes、claims、dispatches 或探索额度，也不提高预算。请求仍须包含 `actor_id`、唯一 `idempotency_key`、`reason`、单元素 `operations`；保留原授权文件，调用前重新读取状态。

```json
{"op":"shutdown.prepare","id":"final-stop-1","evidence":["outputs/acceptance.json"]}
```

返回并持久保存 `state.shutdown_intents[]` 中的 intent，包含 `status=prepared`、原绑定、关闭依据、准备时间和提交 revision、原 control revision、scheduler fence、整体期限与业务状态。`scheduler_revision` 仅用于审计；普通状态 revision 或日志增长不会使 intent 失效。准备时不得有仍可用的 wake/任务凭据。

```json
{"op":"shutdown.confirm","id":"final-stop-1","evidence":["outputs/native-paused.json"],"host_observation":{"automation_id":"original-id","thread_id":"original-thread","host_id":"local","status":"PAUSED","observed_at_unix":1234567890}}
```

确认需已持久化的 prepare、相同的原绑定、仍关闭的业务、相同 control revision 和 fence、准备之后的 PAUSED 观察以及不可变本地证据。记录 `status=confirmed` 和 confirmation。运行时仅校验调用者提交的证据结构、绑定、时序和哈希，不能认证原生工具真实执行；最终报告必须独立核对工具时间线，不能用新观察洗掉早于 prepare 的宿主暂停。

`host_observation.configuration_updated_at_unix` 可选提供配置更新时间（epoch 秒）；提供时必须不早于 prepare 且不晚于观察时间，否则拒绝 `shutdown_order_unproven`。helper 将原生 `updated_at` 毫秒转为该字段，输出 `host_pause_order=after_prepare|preexisting_host_pause|order_unproven`。pending 的已暂停宿主更新时间早于 prepare 时返回 blocked；缺更新时间时可给出 confirm 候选但顺序仍未证明。配置时间只是辅助证据，`strict_order_proven` 始终为 false，严格完整顺序须独立原生工具时间线审核；不得自动 enable 再 pause 来补造顺序。

新 id 与当前 intent 并存会报 `shutdown_pending`；应继续既有 intent。resume、rebind、fence 改变或整体期限变化会使旧 intent 及旧幂等重放报 `shutdown_stale`，应检查当前业务状态后用新 id 准备，旧记录保留。普通调度 revision 变化不单独造成失效。同一 id 不可复用；相同幂等键可重放但会再次核对 guard、授权及证据哈希。修改已引用的证据会拒绝操作。

## 冷启动与只读检查

```powershell
python -B -X utf8 adaptive-project/scripts/shutdown.py --project <原项目绝对路径>
```

`inspect_shutdown(project, automation_root=None)` 只读检查原 SQLite 状态和原绑定的本机宿主配置。它不创建 wake，不启动或暂停宿主，不修改控制、预算、期限或任何项目状态。现有持久 prepare 可直接继续原宿主的收尾，不需要恢复业务准入。需要 SQLite 显式存储恢复时先按恢复文档处理，不将数据库写入伪装成只读检查。

决策 `business_open` 要求先完成业务关闭；`prepare` 要求提交准备；`pause_host` 要求对原绑定执行原生暂停；`confirm` 表示观察到原宿主已暂停，仍需真实工具证据；`confirmed` 表示账本已有确认。未确认 intent 遇到宿主缺失、绑定不符或不支持时 `blocked`，不得猜测成功或另建计划。已确认后宿主缺失可与后续删除一致，但返回非零并要求核对独立删除回执；配置缺失本身不构成删除证明。

本工具只能观察本机平面 heartbeat 配置文件；它不能证明应用在线、将来会触发或停止了正在推理的模型。必须保存：业务关闭事务 → prepare 事务 → 原生 PAUSED 调用/结果 → 观察 → confirm 事务 → 可选删除及核对。每一箭头均应有实际时间与独立证据，缺一步就不得声称完整协议通过。

## 临时系统辅助任务清理

Codex 原计划仍必须使用原生工具按上述顺序暂停和删除。若项目另有已授权 Windows 辅助任务，按 [宿主证据工具](host-evidence.md) 使用 windows_task.ps1 对创建回执中的精确路径观察/删除，并保存 stdout、stderr、退出码。权限或连接失败属于 unknown，不是已删除；只有成功枚举确认不存在才完成该后置项。
