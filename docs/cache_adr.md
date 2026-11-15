# ADR：查询缓存回放与失效策略（E4③）

> 状态：已实现（2026-10-27；§8 单飞 2026-11-16 增补）｜实现：app/cache.py + tests/test_cache.py（25 项）+ scripts/eval_cache.py
> 全量测试 57 项全绿（44 既有 + 13 新增）；缓存正确性评测 PASS（33/33 逐条一致）

## 1. 我为什么做缓存

BI 场景的查询重复率极高——"上月销售额"这类问法每天被问几百次，而 DuckDB 执行一次只要 40ms，真正贵的是上游的意图生成 LLM 调用。缓存命中时**跳过的不只是 SQL 执行，整条查询链路都可以短路**（同 DSL 指纹 → 结果集直接回放）。这是把"成本看板"（ADR-4 预算路由）的思路再往前推一步：最便宜的 LLM 调用是不调用。

## 2. 我考虑过的三个方案

| 方案 | 结论 | 放弃原因 |
|---|---|---|
| 进程内 OrderedDict（近似 LRU） | ✅ 采用 | 单机单写者场景（ADR-2 的 DuckDB 同款边界）下最简单；TTL/版本/计数器/锁全都好做 |
| SQLite 缓存表 | 备选 | 跨进程重启可保留，但当前没有"重启后还要旧结果"的需求，反而引入过期清理的复杂度 |
| DuckDB 物化视图 | 放弃 | 物化视图是"数据变化时刷新"，我的需求是"查询结果按失效策略过期"——语义不同；且物化视图绕过了行级权限的编译期注入路径 |

## 3. 缓存键：为什么用规范化 DSL 指纹而不是原文

这是本设计里我认为最有含金量的决策。两个查询："上月华南毛利率"和"上个月华南地区的毛利率"——SQL 可能完全相同，但 DSL 字段顺序、过滤书写、时间写法（last_n_months=1 vs between 同月）不同。如果拿原文哈希，命中率会低得可笑。

所以 `canonical_dsl`（cache.py:105）做四层归一：①只保留影响 SQL 语义的字段（intent 这类"从不过参与编译"的字段直接丢弃）；②维度排序、过滤条件排序、IN 列表值排序（集合语义与书写顺序无关）；③时间统一解析为绝对月窗口（与编译器共用 _END_MONTH 锚点，避免两处漂移）；④**role 显式参与指纹**——行级权限在编译期注入，boss 和员工同 DSL 结果不同，绝不允许跨角色复用缓存。最后 SHA256 + 指纹格式版本号（规范化规则变了递增，防新旧语义混存）。

配套了 `align_columns`（cache.py:348）：维度归一排序后，命中结果的列序可能与请求方不同——按请求方维度重排列（编译器保证列结构恒为"维度...+value"，可安全重排）。

## 4. 失效三闸（TTL/数据版本/编译 semver，第三闸为对抗性评审 A1 补充，ASK-11 同步）：为什么缺一不可

- **TTL（默认 300s）**：兜住一切"漏失效"的最大错误窗口。数据版本指纹是启发式的（行数/最大日期/明细指纹），理论上存在"数据变了但指纹没变"的碰撞——TTL 保证这种错误的存活时间有上界。
- **数据版本号**：读时计算 duckdb 数据集指纹（cache.py:145），变化即全量失效——数据重生成时不必干等 TTL 过期。读时计算放在锁外做 IO，失败方向被设计成"误失效"（宁可多算一次）而不是"脏命中"。

只有 TTL：数据重生成后最长 5 分钟返回旧数。只有版本号：指纹碰撞无兜底。双闸叠加后，正确性由评测背书：**开/关缓存全量评测 33/33 逐条一致**（scripts/eval_cache.py）。

## 5. 一个必须诚实记录的发现：评测集上缓存零命中

正确性评测里平均耗时 82ms→105ms——**变慢了**。原因：评测集 33 条全是不同查询，单遍评测内没有重复查询，缓存零命中，指纹计算（含每次读写都要跑一遍数据版本查询）变成纯开销。

这个发现我选择如实记录而不是藏起来，因为它划清了缓存的适用边界：**缓存的价值在生产的高重复查询流量里，不在唯一查询的评测集里**。评测验证的是"缓存不改变答案"（正确性），命中率的进程内证据在 tests/test_cache.py 的计数器断言（同 key 两次调用 → hits=1）。生产部署时应开启 CACHE_ENABLED 并盯 stats().hit_rate——命中率低于 20% 就该关掉它省掉这 20ms。

## 6. 接线与开关

- `CACHE_ENABLED` 默认 **False**：关闭时 get/set 直通，执行路径与无缓存版本完全一致——这是"既有 44 项测试一行不改仍然全绿"的保证。
- 接线点在查询节点（nodes.py:282-343）：指纹 → get → 命中回放（带 from_cache 标记）/ 未命中走沙箱 → set。
- 监控点：stats() 的 hit_rate / ttl_expired / version_invalidations / evictions——上线后前三天只观测不决策。

## 7. 我留的坑（接手注意）

- 版本指纹是启发式（内容碰撞理论上可能）——TTL 是兜底，但如果未来数据量大到 COUNT(*) 变慢，版本计算本身会成为每查询的固定开销，要改成写入时维护版本表。
- 跨进程不共享（进程内缓存）——多 worker 部署（uvicorn --workers N）时各 worker 独立缓存，命中率被除以 N；要跨进程就换 SQLite 方案（备选 2）。
- 空结果也缓存（避免缓存穿透重复查询空集）——但错误结果**不**缓存，SQL 失败走修复重试路径，与缓存无关。

## 8. singleflight 并发击穿防护（AB 车道 2026-11-16 增补）

### 8.1 缺口

三闸解决的是"结果过期"，没解决"并发未命中"：高并发下同一查询（同 DSL 指纹 + role + 数据集）同时到达，N 路请求全部 get miss，各自完整跑一遍沙箱执行 + cache.set——压测语境里这正是缓存要挡掉的重复负载反而在最热的 key 上放大 N 倍（缓存穿透的经典"击穿"形态；空结果也缓存的语义同样挡不住这个窗口）。

### 8.2 方案与被否方案

| 方案 | 结论 | 原因 |
|---|---|---|
| claim/publish/fail 三原语（✅ 采用） | QueryCache 只做在飞登记与结果分发，执行方留在事件循环原位 | 见下 |
| get_or_load(key, loader) 一体化 | ❌ 放弃 | loader 需在线程内同步执行，Sandbox.execute 的超时保护是 asyncio.wait_for + con.interrupt()（跨线程打断）——把执行搬进 to_thread 的 loader 等于丢掉 SQL 超时语义，或再造一套线程版 interrupt，得不偿失 |
| asyncio 原语（asyncio.Event/锁） | ❌ 放弃 | QueryCache 是同步 API（脚本/评测/单测都在线程语境用），混入事件循环原语会撕裂接口；threading.Event 在 to_thread 里等待即可，不占事件循环 |

### 8.3 三原语契约（app/cache.py）

- `claim_flight(key)`：快路径 get 命中 → ("hit", result)；登记在飞成为 leader → ("leader", None)；有在飞 → 挂起等待 → ("flight", result)（计 singleflight_coalesced）；等待超上界（`cache_flight_wait_seconds`，默认 5s）→ ("miss", None)（计 singleflight_timeouts）——调用方 fail-open 自行执行，**单飞是优化，绝不成为可用性风险**。
- `publish_flight(key, result)`：leader 执行成功后发布——先挂结果唤醒等待者（等待者读 flight.result，连数据版本探测都省掉），再写缓存、拆 flight；set 失败也保证 flight 必拆（try/finally），不留永久在飞记录。
- `fail_flight(key)`：leader 执行失败拆 flight 唤醒等待者——等待者循环重新竞选，恰一名接棒 leader 重试执行，执行责任不丢。

executor 接线（nodes.py）：leader/超时方执行成功 → publish_flight；执行失败且是 leader → fail_flight。等待者失败不会误挂：超时方误 publish/fail 到原 leader 的 flight 时，结果等价、flight 最终仍被拆除，代价是多跑一次（有界，fail-open 语义）。

### 8.4 禁用态与多 worker

禁用态 claim 恒 ("leader", None)、不起 flight、不计数——与无缓存行为逐字一致（CACHE_ENABLED 默认 False 的零漂移原则不变）。多 worker（uvicorn --workers N）下单飞是进程内的，跨 worker 各自独立在飞——与 §7 跨进程边界同一条缝，属 PRODUCTION_GAP 高可用项。
