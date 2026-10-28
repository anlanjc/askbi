# ADR：澄清挂起/恢复（E4②——LangGraph interrupt + checkpointer）

> 状态：已实现（2026-10-28）｜实现：graph.py（flag 感知装配）+ nodes.py clarify_suspend_node + **API 端到端（thread_id/resume/clarify_pending SSE）**
> 全量测试 77 项全绿（67 既有 + 4 图级 + 1 API 端到端 + 5 指代消解等后续）；`CLARIFY_SUSPEND_ENABLED` 默认 False，关闭时逐字零行为变化

## 1. 我为什么把"澄清"从终结态升级为挂起点

原行为：澄清 = 图跑完输出一个反问（"你想看哪个指标？"），用户回答后**当作全新问题重跑全图**——上一轮的执行上下文全丢。E4②用 LangGraph 的 `interrupt()` + checkpointer 把它升级为：**图在澄清点落盘挂起，用户答案通过 `Command(resume=...)` 注入，从断点续跑到出数**。这是 human-in-the-loop 的标准 LangGraph 模式，与 miniagent 权限确认（同步回调）形成"两种 HITL 挂起语义"的对照。

## 2. 设计决策

### 2.1 挂起做成独立节点，不在 clarifier 里内联
- **决策**：新增 `clarify_suspend_node`（唯一职责：interrupt → 合并澄清进问题 → 清空 clarify），clarifier 的条件边按 flag 路由到它或 finalizer。
- **理由**：interrupt 的恢复语义是"**节点从头重执行**"——如果 interrupt 内联在 clarifier 的 LLM 调用之后，恢复时 clarifier 会重跑一次 LLM（多一次调用且可能给出不同判定）。独立挂起节点重入成本≈0。
- **备选与放弃**：在 clarifier 内联 interrupt——省一个节点但引入"恢复时 LLM 重跑"的非确定性。

### 2.2 恢复后把澄清并入问题文本，而不是注入状态槽位
- **决策**：`question += "（用户澄清：{answer}）"` → sql_agent 的意图生成自然消化。
- **备选与放弃**：直接把 answer 写进 DSL 槽位——需要理解答案的语义结构（"销售额"是指标、"华南"是维度），这本来就是 LLM 的活。
- **后果**：merged question 对下游可读（sql_agent 实测正确生成 revenue 意图并出数）。

### 2.3 flag 感知装配的三个坑（全部踩过并修复）
1. **条件边的 Literal 注解会被 LangGraph 静态校验**——flag 关时 clarify_suspend 节点未注册，但注解里写了目标 → 编译报"unknown target"。修：闭包去注解 + 显式 path_map。
2. **interrupt 需要 checkpointer**——不传则中断无法落盘。修：flag 开时默认 InMemorySaver，测试可注入。
3. **测试共享模块级 config dict 会被 LangGraph 原地改写**（注入 checkpoint_ns）——第二个测试复用被污染的 config，恢复流零输出。修：config 工厂函数每次新建。**这三条都是测试驱动暴露的**。

## 3. API 契约（已实现，2026-10-28）

- `POST /api/ask` 增加 `thread_id`（不传自动用 task id）与 `resume` 字段；`CLARIFY_SUSPEND_ENABLED=1` 时：图挂起 → SSE 推 `clarify_pending`（带 thread_id 与反问负载）→ 客户端带同 thread_id + `resume=true` 重发（question=用户答案）→ `Command(resume)` 续跑至 final。端到端测试：挂起推 clarify_pending → 恢复出 answer（tests/test_clarify_suspend.py::test_api_clarify_pending_and_resume）。
- 装配细节：挂起拓扑用**独立图实例**（带 InMemorySaver），与默认单例隔离——flag 关闭时连构建都不发生。
- 线程隔离有测试：t1 恢复不影响 t2 的挂起状态（aget_state 验证）。

## 4. 边界

- checkpointer 是 InMemorySaver（进程内）——生产换 SqliteSaver/PostgresSaver 即可跨重启恢复（接口不变）。
- 恢复时 clarify_suspend 节点重执行但零 LLM 成本；sql_agent 起点是合并后的问题——意图生成消耗一次 LLM 调用（与原"重跑全图"相比仍是净省）。
- 与指代消解（app/context.py）的关系：两者都是多轮能力——指代消解改写输入文本，澄清挂起暂停等输入；组合使用时消解在前、挂起在后，互不冲突（开关独立）。
