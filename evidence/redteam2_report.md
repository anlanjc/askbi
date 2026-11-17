# 红队 round 2 报告（[1B4-AB] 任务三，2026-11-18）

> 攻击面针对**本批新增面与既有并发/审计/参数面**（round 1 = 经典注入/绕过/提示注入，
> 见 redteam_report.md）。测试载体 `tests/test_redteam2.py`（41 项，全程 mock）。
> 逐条给出 payload → 实际行为 → 判定与机制归因。**抓到真缺陷 2 个，当日修（带编号提交）**。

## 一、攻击面 1：singleflight 击穿对抗（10 项）

| # | payload / 姿势 | 实际行为 | 判定 | 机制 |
|---|---|---|---|---|
| 1.1 | 8 路线程同 key 风暴（leader 挂闸门） | 7 等待者全部挂起在 flight 上，publish 后共享同一结果 | PASS | `claim_flight` 同 key 单 leader；`singleflight_coalesced==8`（含主 leader 命中前 8 次 claim 中 1 leader + 7 共享，计数口径见 stats()） |
| 1.2 | 同 key 风暴 + 异 key 并发混合 | A 风暴内部合流；B 首个 claim **立即**取得自己的 leader，不被 A 阻塞 | PASS | flight 表按 key 隔离；B 内部再自合流（flight+leader 各一） |
| 1.3 | 同 DSL 不同 role 指纹 | 指纹必异（role 参与键）→ 两个 key 各自 leader，绝不跨角色合流 | PASS | `canonical_dsl` 显式携带 role——行级权限不同结果集不可共享 |
| 1.4 | 同 DSL 不同 dataset 指纹 | 同上，跨源不合流（hotel vs ecommerce） | PASS | `ds` 参与指纹（跨源隔离第一道闸） |
| 1.5 | leader 连续失败两轮（fail→fail→publish） | 每轮恰一名等待者接棒 leader，其余共享新结果；执行责任逐轮交接不丢失 | PASS | `fail_flight` 拆 flight 唤醒等待者重新竞选；coalesced==n-2 精确 |
| 1.6 | fail-open 等待者自行执行并 publish（原 leader 仍在飞） | 不崩溃；fail-open 方写缓存生效；leader 事后 publish 覆盖为权威结果 | PASS | publish 为幂等写缓存；`singleflight_timeouts==1` 如实记账 |
| 1.7 | store 失败对抗（版本探测抛错 → set 抛错） | flight 仍被 try/finally 拆除、等待者拿到共享结果不被挂死；异常向上传播 leader 可感知 | PASS | publish_flight 的 finally 拆除语义——缓存写失败不得把单飞变成可用性风险 |
| 1.8 | 无在飞记录时 fail/publish（幽灵 key） | 安全 no-op；误拆他人 flight 后原 leader 事后 publish 为纯写缓存、可重新竞选 | PASS | `fail_flight` pop(None) 幂等；fail-open 竞态的诚实代价=多跑一次 |
| 1.9 | e2e：6 路并发同问（mock 全图 + 沙箱闸门） | 沙箱**只执行一次**，5 路共享结果（from_cache=true + cache_hit(shared=true)），coalesced==5/timeouts==0 | PASS | executor 单飞接线（to_thread claim，执行留事件循环原位） |
| 1.10 | 禁用态 | 恒 leader、不起 flight、不计数（对照锚） | PASS | 与无缓存行为逐字一致（CACHE_ENABLED 默认关零漂移） |

## 二、攻击面 2：审计日志注入（11 项）

| # | payload | 实际行为 | 判定 | 机制 |
|---|---|---|---|---|
| 2.1 | `line1\nline2-forged-record` 入 actor/target_id/detail | 每事件恒一行、逐行合法 JSON、roundtrip 无损 | PASS | json.dumps 转义 `\n` → `\\n`，结构上无法伪造第二条审计行 |
| 2.2 | `x\r\nDROP TABLE audit;--` | 同上；载荷只作数据 | PASS | 同上 |
| 2.3 | 引号混淆 `quote'"double"quote` | JSON 转义保真 | PASS | json 序列化 |
| 2.4 | `</script><script>alert(1)</script>` | 原样存储为数据；回显端点 content-type 恒 application/json | PASS | 审计回显是 JSON API 不是 HTML 渲染面 |
| 2.5 | RLO/PDF 双向控制符（`\u202e\u202d`） | 原样数据往返，不解释 | PASS | 同上 |
| 2.6 | `{"injected": true}` 伪装 JSON 对象 | 字符串原样入库，不解析为结构 | PASS | 只有顶层 event 是 dict，字段值恒为字符串 |
| 2.7 | 10KB 超长字段 | 单行落盘无损 | PASS | JSONL 无字段长度限制（记录面），行完整性由转义保证 |
| 2.8 | 50 次风暴注入 | 恰 50 行 | PASS | `_lock` 内单行写入原子性 |
| 2.9 | 攻击者直写损坏行（非 JSON/截断 JSON/伪造完整事件） | 损坏行跳过不崩溃；伪造事件可被读出但**不可执行**（审计是只读追加日志） | PASS（口径注记） | list() 容错跳过损坏行（与 TaskStore 同策略）；"日志不可执行"是机制属性，伪造行可读属追加日志诚实边界 |
| 2.10 | GET /api/admin/audit 的 POST/PUT/DELETE | 一律 405（只读面无变更方法） | PASS | FastAPI 仅注册 GET |
| 2.11 | admin_token 配置后无/错 token 访问审计 | 401（常量时间比较）；正确 token 放行 | PASS | `_require_admin` |

## 三、攻击面 3：resume 竞态新姿势（8 项）

| # | payload / 姿势 | 实际行为 | 判定 | 机制 |
|---|---|---|---|---|
| 3.1 | 同 thread 二次 acquire | 第二个 False（互斥）；release 后可再 acquire | PASS | `_resuming_threads` set 互斥 |
| 3.2 | 对未 acquire 的 thread release / 重复 release | no-op 幂等 | PASS | discard |
| 3.3 | acquire 后异常路径（模拟 astream 爆炸） | finally 释放后线程可再次 resume | PASS | gen finally 必经释放（ASKBI-7 契约） |
| 3.4 | **409 拒绝路径孤儿任务**：并发第二个 resume → 409 | **修复前：create 先于 acquire，409 后任务永久滞留 running**（running 永不被 _evict_if_over 淘汰 → 泄漏）；**修复后：409 路径先 finish_if_running 落 aborted 再拒绝** | **真缺陷 RT2-1 → 已修** | 任务兜底原语 finish_if_running（ASKBI-1）复用到 409 路径 |
| 3.5 | 409 清理后重试 resume | 在途 resume 完成后重试正常恢复/无挂起时干净 409（无孤儿） | PASS | 清理不触碰 thread 状态与 resume 标记 |
| 3.6 | running 孤儿经 finish_if_running | 落 aborted；已终态再调 no-op | PASS | 单元钉死兜底原语 |
| 3.7 | 4200 次属主绑定风暴 | 注册表 ≤4096（FIFO 淘汰） | PASS | _MAX_THREAD_OWNERS 上限 |
| 3.8 | 无挂起可恢复的 resume（快照检查 409） | 不创建任务（校验先于 create），无孤儿 | PASS | ASKBI-1 校验 raise 先于 create |

## 四、攻击面 4：深度参数滥用计费逃逸（9 项）

| # | payload | 实际行为 | 判定 | 机制 |
|---|---|---|---|---|
| 4.1 | question 501 字符 | 422（500 内 200） | PASS | schema Field max_length=500 |
| 4.2 | **thread_id 300 字符** | **修复前：接受**（直达 checkpointer 键/属主注册表/JSONL 落盘面）；**修复后：422** | **真缺陷 RT2-2 → 已修** | AskRequest.thread_id max_length=200 |
| 4.3 | **session_id 300 字符** | 同上 | **真缺陷 RT2-2 → 已修** | max_length=200 |
| 4.4 | **feedback task_id 300 字符** | 同上 | **真缺陷 RT2-2 → 已修** | FeedbackRequest.task_id max_length=200 |
| 4.5 | **export task_id 300 字符** | 同上 | **真缺陷 RT2-2 → 已修** | Query(min_length=1, max_length=200) |
| 4.6 | depth=full 强制滥用 | 节点集有界（clarifier/sql_agent/insight/qa/finalizer），用量照记 | PASS | 图结构有限 + 显式 depth 只影响路由不产生循环 |
| 4.7 | depth=light 显式降级 | 跳过 insight/qa → 计费节点减少（升账单/省账单两向都有界且如实记账） | PASS | 预算路由 |
| 4.8 | SQL 连败滥用（沙箱恒失败） | 修复循环精确受 max_sql_repairs 约束（1+上限次执行）后拒答，**无无限烧 token** | PASS | repair_count 计数 + 耗尽拒答 |
| 4.9 | 拒答路径计费逃逸探测 | 枚举外取值拒答链路 clarifier/sql_agent 用量照记（不逃逸） | PASS | usage 随节点累积，与成败无关 |

## 五、攻击面 5：prompt 注入二阶（3 项 + 复用锚）

| # | payload | 实际行为 | 判定 | 机制 |
|---|---|---|---|---|
| 5.1 | 审计回显端点预埋 5 类敌意载荷（HTML/JS/换行/RLO/JSON 串） | 200 + application/json；载荷作为**数据**原样往返（body[0] 即最新记录原文）；损坏行跳过 | PASS | JSON API 面 + list() 容错 |
| 5.2 | 敌意问题（`<img onerror>` + SQL 堆叠）入库 → 任务明细回显 | content-type application/json、问题字段原样数据往返；SSE 流面 text/event-stream | PASS | API 层全 JSON（二阶 XSS 只可能在前端渲染层，esc() 由 round 1 静态钉死——本批复验锚保持在场） |
| 5.3 | 多行注入问题落 tasks.jsonl | 每任务恒两次快照（create/finish）行，逐行合法 JSON，无伪造行 | PASS | json.dumps 转义 |
| 5.4 | 管理配置面自由文本（额外字段 actor/detail） | 额外字段被忽略（白名单三布尔）；审计 detail 只含布尔 from→to | PASS | AdminConfigRequest exclude_unset + 结构化 detail——自由文本在结构上进不了审计面 |
| 5.5 | 属主/管理 token 明文探测 | 审计文件不含 token 明文（owner 只可能是 64 hex 派生值；admin action actor 恒 "admin"） | PASS | 哈希派生 + 审计字段白名单 |
| 5.6 | 头注入 token（换行/脚本/10KB）×4 | 落盘 owner 字段全部 re.fullmatch([0-9a-f]{64})；tasks.jsonl 无伪造行 | PASS | SHA-256 派生在结构上中和头注入 |
| 5.7 | web esc() 复验锚 | esc 五元字符映射、关键 sink 断言保持在场 | PASS（回归锚） | round 1 纵深不因新面松动 |

## 六、发现并当日修复（2 真缺陷，与本测试同批提交）

- **RT2-1 resume 409 孤儿 running 任务**：`POST /api/ask`（resume=true，挂起拓扑开）在
  并发第二个 resume 时按设计 409 拒绝，但 `task = create(...)` 先于 `acquire_thread_resume`
  （保持 ASKBI-7 的"占用放响应返回前最后一步"无 await 窗口），拒绝路径从不执行 gen 的
  finally 兜底 → 该任务**永久滞留 running**；而 `_evict_if_over` 只淘汰 finished 任务，
  running 孤儿永不可回收 → 任务列表与内存索引泄漏（实测探针复现：409 后 list 出
  status=running 的孤儿）。修复：409 路径 raise 前调 `finish_if_running(task_id, aborted)`
  落终态（复用 ASKBI-1 兜底原语，幂等）；回归 3.4/3.5 先红后绿。
- **RT2-2 请求 id 无长度上限**：`AskRequest.thread_id/session_id`、`FeedbackRequest.task_id`、
  `/api/export?task_id=` 均为自由长度字符串 → 超长 id 直达 checkpointer 键（LRU 按条目数
  不按字节）、属主注册表、tasks.jsonl 落盘面（实测 300 字符全被接受）。修复：四处统一
  `max_length=200`（与 question 上限同哲学，合法会话标识远短于此）；回归 4.2~4.5 先红后绿。

## 七、口径与纪律

- 全程 `LLM_PROVIDER=mock`（conftest 显式），零 API 花费；41 项测试全部通过
  （含 2 缺陷修复后的回归），全量 pytest 366 绿 + ruff 全清。
- 观察项（如实记录，不修）：①审计 list() 对"直写文件的伪造完整事件"按容错口径
  原样读出——追加式日志无鉴权写面（文件系统权限即边界），伪造行不可执行；②
  task_detail 的 path 参数（/api/tasks/{task_id}）未加长度约束——路径参数天然受
  URL 长度约束且只作 dict 键查 O(1)，未构成与 4.2~4.5 同级的落盘面。
- 红线自查：冻结评测集旧文件零字节改动（md5 与批次前一致）；路径限定 add；未 push。
