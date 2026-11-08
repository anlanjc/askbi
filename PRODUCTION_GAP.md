# AskBI 生产就绪差距分析（Production Gap Analysis）

> 用途：面试被问"上过线吗/多少并发/这不就是玩具吗"时，这份文档就是"我清楚差距在哪"的书面证据。
> 口径：现状证据全部来自 EVIDENCE.md/evidence/ 实测；差距不回避；每项给补齐方案与预估工作量。
> 更新：2026-10-29。

## 一、现状盘点（已具备的生产要素）

| 要素 | 现状 | 证据 |
|---|---|---|
| 测试 | 119 项 pytest 全绿（单元/集成/API roundtrip） | `pytest -q` |
| 评测 | 单轮 116 条（mock 100%/real 94.8%+holdout 盲测）+ 多轮 64 轮（mock 100%/real 89.1%）+ no_qa 消融（QA 价值 +58pp） | eval/report_*.md |
| 可靠性 | 故障注入 6 场景×10 零崩溃；LLMError 重试+四节点降级；SQL 超时 interrupt | evidence/fault_injection.md |
| 性能 | 编排上限 110-124 QPS（0 延迟）；300ms 模拟 LLM 延迟下 P50≈1.2s；瓶颈归因=LLM 串行调用（light 2 次/full 4 次），检索占比个位数 | evidence/load_test.md |
| 安全 | DuckDB 只读沙箱+语句守卫+维度白名单+LIMIT 钳制+行级权限编译期注入+API key 中间件+MASKED_FIELDS 纵深 | app/data/sandbox.py、schema.py |
| 成本 | token 分节点记账、预算路由 4→2 次调用、查询缓存（修复后 +12% 吞吐）、评测成本账本（全量 ≈¥1.7/遍） | evidence/load_test.md、eval/ |
| 工程化 | Docker/docker-compose、CI（test+eval-gate 双 job，push 即生效）、分模块 git 历史、EVIDENCE 证据链 | .github/workflows/ci.yml |
| 语义层解耦 | DatasetSpec 注册表+双数据集（电商/咖啡）验证"换源上层不改" | app/data/registry.py、dataset_b.py |

## 二、生产差距清单（诚实版，每项含补齐方案）

| # | 差距 | 为什么生产需要 | 补齐方案 | 预估工作量 |
|---|---|---|---|---|
| 1 | **真实流量与用户**：无 DAU/QPS 生产数据 | 没有真实查询分布，评测集覆盖不到长尾问法 | 公网部署（DEPLOY.md 已就绪）+ PILOT_TEMPLATE 5-10 人试用 + 试用问题回流评测集 | 部署半天 + 试点 1 周（零代码） |
| 2 | **多租户**：仅单层 API key | 不同部门/客户的数据权限与配额要隔离 | role→tenant 映射进 DatasetSpec、per-key 配额中间件、审计日志（谁在何时查了什么） | 3-5 天 |
| 2b | **任务接口无属主隔离**（评审 2.0 ASK-6）：/api/tasks、/api/tasks/{id}（含 answer/events）、/api/export、/api/feedback 不区分调用方——行级权限只约束编译期执行不约束结果分发，部署后任意调用方可读他人全量答案。过渡口径：公开演示仅在有 API_KEY 时可接受；正式多租户前须把任务绑定调用方身份并按属主过滤。**【2026-11-09 AB-HR2：第一块拼图已落地】`OWNER_CHECK_ENABLED` 开关（默认关零漂移）：thread 创建绑定 X-Owner-Token（SHA-256 落盘，明文不落盘），resume/明细/反馈/导出/列表过滤全路径校验，非属主 403 fail-closed，绑定劫持防护；设计记录 docs/owner_isolation_adr.md（含被否方案）。剩余：正式多租户身份体系、/api/metrics 聚合口径、前端接线 token** | role→任务绑定+属主过滤+admin 面板豁免 | 1-2 天 |
| 3 | **可观测性**：trace 落盘 JSONL+进程内计数器 | 生产要实时告警与跨服务追踪 | OpenTelemetry exporter + Prometheus 指标（错误率/P95/每 token 成本）+ SLO 告警规则 | 3-4 天 |
| 4 | **数据层规模**：DuckDB 单文件只读 | 亿级行/高并发写需要列存集群；当前分析负载（只读聚合）单机够用 | 触发条件见下表；到量迁 ClickHouse/StarRocks，语义层 registry 已把换源成本压到"换 spec" | 视数据量 1-2 周 |
| 5 | **高可用**：单进程 uvicorn | 单点故障、发布即停服 | 多副本+负载均衡、healthz 就绪探针、优雅重启（drain SSE 连接）、LLM 供应商双活（DashScope 主+DeepSeek 备） | 4-5 天 |
| 6 | **供应商成本与限流治理**：重试/预算路由有 | 真实流量下的成本失控与 429 风暴 | 模型分级路由（澄清/评分用小模型）、供应商配额看板、批量调用合并 | 2-3 天 |
| 7 | **数据质量监控**：合成数据无脏数据 | 真实数仓有迟到/重复/口径漂移 | 数据版本指纹已有（缓存双闸）；补：新鲜度告警+行数突降告警+口径变更评审流 | 2-3 天 |
| 8 | **安全纵深**：白名单+沙箱已实测 | 生产要红队验证与合规 | 注入红队用例集（已有 6 场景可扩）、PII 脱敏审计、操作审计留痕 | 3 天 |

## 三、"什么时候需要 X"触发条件表（把单机判断变成有依据的决策）

| 组件 | 触发条件（当前状态→需要时的信号） |
|---|---|
| Redis | 多副本部署需要跨进程共享缓存/会话时（当前单进程内存 LRU 够用；压测证明编排不是瓶颈） |
| 消息队列 | 出现异步长任务（批量报表、回填写数仓）或削峰需求时；当前同步 SSE 问答链路无此形态 |
| ClickHouse/Postgres | 数据量 > 数千万行或出现并发写与分析师直连需求时 |
| K8s | 多副本+弹性伸缩+多环境成为日常运维负担时（Docker compose 是当前合理形态） |
| 小模型分级 | 真实流量下 token 成本成为 P0 成本项时（当前预算路由已做第一步） |

**一句话**：压测证明编排层 110+ QPS 上限远未触顶、瓶颈在 LLM 供应商侧——所以"上 Redis/K8s"在当前规模是负债不是资产，把这份判断讲清楚比堆组件更加分。

## 四、离生产最近的三个动作（按顺序）

1. **公网部署**：DEPLOY.md（Render/Docker，mock 版零成本）→ 拿到 URL 写进 README/简历 → 部署形态再跑一次 `scripts/load_test.py`（把压测数字从 mock 进程内升级为真实 HTTP 链路）。
2. **PILOT 试点**：PILOT_TEMPLATE.md，5-10 人×10 问 → 产出真实问题清单 → 回流评测集 v3。
3. **差距复盘**：试点后回填本文档 §1 表格，把"无真实流量"行改成真实数字。
