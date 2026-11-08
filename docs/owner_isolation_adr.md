# ADR：thread 属主隔离（AB-HR2，2026-11-09）

> 状态：已实现（`OWNER_CHECK_ENABLED` 开关，默认关）。
> 背景：评审 2.0 ASK-6 / PRODUCTION_GAP 2b——行级权限（ROLE_SCOPES）只约束**编译期执行**，
> 不约束**结果分发**：`/api/tasks/{id}`（含全量 events/答案）、`/api/export`、`/api/feedback`
> 不区分调用方，公开部署后任意调用方可读他人全部问答记录。这是多租户的第一块拼图。

## 决策

**开关式属主绑定**：`OWNER_CHECK_ENABLED`（默认关 = 存量行为逐字零变化）。

- **身份形态（最小实现）**：`X-Owner-Token` 请求头 → 服务端派生 SHA-256 存任务记录与
  thread 注册表。明文 token 不落盘（tasks.jsonl 可回放可导出）。
  现有鉴权形态盘点：`api_key` 是全局单值（非身份）、`admin_token` 是管理面专用、
  `session_id` 客户端可伪造——三者都不能当属主标识，故新增最小请求头。
- **绑定面**：
  - 任务级：`TaskStore.create(owner=sha256)` → 任务记录 `owner` 键（开关关时不出现该键）。
  - thread 级：挂起拓扑建 thread 时登记 `thread_id → owner`（内存注册表，FIFO 上限 4096）。
    thread_id 可由客户端自供、与任务 id 不同命名空间——注册表是 resume 校验的权威源，
    与 InMemorySaver checkpointer 同生命周期（挂起 thread 本就不跨重启，不引入新持久化面）。
    **setdefault 语义**：已有绑定不覆盖——防攻击者以同 thread_id 重新提问改写属主（绑定劫持）。
- **校验面（开启时）**：
  | 端点 | 非属主 | 未出示 token |
  |---|---|---|
  | POST /api/ask（新问题） | —（创建即绑定） | 401 |
  | POST /api/ask（resume） | 403 | 401 |
  | GET /api/tasks/{id} | 403 | 401 |
  | POST /api/feedback | 403 | 401 |
  | GET /api/export | 403（在暴露答案存在性之前） | 401 |
  | GET /api/tasks（列表） | 按属主过滤 | 401 |
- **fail-closed 三条**：开关开启前创建的无绑定任务 → 403（任何调用方含任意 token）；
  未登记/无绑定的 thread resume → 403（先于 ASK-7 的 409 语义）；哈希比较走
  `hmac.compare_digest`（常数时间）。

## 被否方案

1. **全局 thread 锁**——防的是并发写冲突不是越权读取，问题错位；且锁不阻止"读他人任务 id"。
2. **纯前端隔离**（sessionStorage 存 token 只查自己的）——客户端可清可改，接口裸奔等于不做。
3. **不做**（等正式多租户身份体系）——公开部署的最低门槛缺口，且面经真题"多租户怎么做隔离"
   要求能讲机制；开关式落地让"部署日"只需配一个环境变量。

## 剩余面（诚实交底）

- `/api/metrics` 仍是全局聚合（token 用量汇总）——多租户下需按属主聚合或限管理面。
- 管理面（/api/admin/*）豁免属主校验（本就 admin_token 门禁 + 全审计）。
- 前端未接线 X-Owner-Token（开启属主校验时需客户端带头，属部署期配置）。
- 正式多租户身份体系（per-tenant API key、配额、账单）见 PRODUCTION_GAP 2b 后续批次。

## 测试锚点

`tests/test_owner.py`（11 项）：开关关全链零变化 ×2 / 绑定+SHA-256+属主全通 / 越权 403+未出示 401
全路径 / 列表过滤 / 无绑定 fail-closed / resume 允许-拒绝-401 / 无绑定 thread resume fail-closed /
未知 thread fail-closed / 绑定劫持防护。
