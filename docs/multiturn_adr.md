# ADR：多轮评测集 v2 与演示层（AB-2——指代追问 / 时间追问 / 澄清挂起恢复 / 缓存命中复问）

> 状态：已实现（2026-10-28）｜实现：eval/dataset_v2_followup.json（30 条 × 64 轮）+ scripts/run_multiturn_eval.py + scripts/demo_full.py + web/index.html（缓存徽章 / session_id / 挂起恢复流）
> 验证：多轮评测 30/30 条目、64/64 轮全过（4 个缓存命中轮全部按预期命中）；askbi 全量 pytest 81 绿（零新增失败）；demo 四场景 mock 全通、零 API 花费

## 1. 为什么要一个独立的多轮评测集

v1 评测集（eval/dataset.json，33 条）是**单轮**的：每条用例独立成问，判分只看"这一问的答案对不对"。但 AskBI 的三个核心扩展恰恰活在**轮与轮之间**——指代消解（app/context.py）依赖上轮问题，查询缓存（app/cache.py）依赖同指纹复现，澄清挂起（E4②）依赖跨请求的 thread 恢复。单轮集对它们是盲的：把"那华北呢"当独立问题跑，判分器只会说"答案错了"，说不出是消解没触发、消解错了骨架、还是缓存串了角色。多轮集把"轮次序列 + 期望行为"作为一等公民，让这三个扩展第一次有了端到端的回归网。

## 2. 设计决策

### 2.1 判分器复用 v1 的 judge_query，多轮接线镜像 API 层
- **决策**：runner 只 import `app.eval.runner.judge_query`（同一把尺子，排序后 4 位小数比对），不改 runner.py 一行；指代消解在脚本层按 `app/api/main.py /api/ask` 的**同序**接线：`resolve_followup → ctx.set(改写后问题) → run_ask`。
- **理由**：两套判分口径是评测平台最常见的腐烂源；接线顺序不一致则"评测跑的不是线上行为"——API 存的是改写后的问题（下轮"那华南呢"才能消解回第一问原句），这个细节抄错，缓存组合用例（fu12）必挂。
- **代价**：脚本层有一段与 API 相似的 10 行接线逻辑。曾考虑把接线下沉进 run_ask——但那要改 app/（本任务只读），且会改变 run_ask 的纯函数签名，不做。

### 2.2 评测驱动方式：run_ask 组合 vs TestClient vs 真端口
- **三个候选**：① 真端口 uvicorn+httpx；② fastapi TestClient 进程内走完整 API 层（任务卡推荐项）；③ run_ask + app.context 镜像组合（本选）。
- **选 ③ 的理由**：API 层的多轮接线只有 10 行纯逻辑（`resolve_followup → ctx.set(改写后问题) → run_ask`，挂起再加 `Command(resume)`），镜像成本极低；而 TestClient 要拆 SSE 分帧才能断言 from_cache / 消解说明——判定逻辑会埋进流解析器。更硬的约束是副作用：API 路径每问都会经 task_store **追加写 data/tasks.jsonl**（task_store.py:19,66），评测跑 64 轮就写 64 条任务记录，评测输入输出不再纯净；run_ask 组合零写副作用。真端口的 SSE 契约已有 tests/test_clarify_suspend.py::test_api_clarify_pending_and_resume 端到端覆盖，评测不必重复承担进程与端口管理。
- **腐化风险与对冲**：镜像可能与 API 接线漂移——runner docstring 标明镜像源（app/api/main.py /api/ask 的 65-70 行顺序），且 fu12/fu30 这类"消解×缓存"组合用例对接线顺序敏感（ctx 存的是改写后问题），API 改了而镜像没改时这两条会立刻红，漂移可观测。

### 2.3 PROFILE=full 之下，run_ask 必须显式关掉挂起开关
- **发现**：`CLARIFY_SUSPEND_ENABLED` 在 full 档默认开 → `get_graph()` 带上 InMemorySaver → `run_ask` 的一次性 astream（无 thread_id）直接抛 `Checkpointer requires ... thread_id`。这不是 bug，是挂起语义的必然要求。
- **决策**：评测与 demo 脚本都在导入 app 前设 `CLARIFY_SUSPEND_ENABLED=0`（配置约定"显式配置优先于档位默认"的正用），挂起语义改由 suspend 条目经 `build_graph(clarify_suspend=True) + Command(resume)` 独立图验证——与 API 的挂起拓扑同构。
- **教训**：给全局图加"需要请求上下文"的能力时，必须同时确认所有无上下文的既有入口（run_ask/评测脚本）不会静默变红。

### 2.4 mock 可判定：每条预期先经解析器实测，再写进数据集
30 条 × 64 轮的期望值（kind / gold SQL / from_cache / note_contains）没有一条是猜的，全部先在 `app.llm.mock._parse_question` + `app.context` 上探测过。探测换来四个关键设计修正：
1. **词表边界**：context 实体词表与 schema 取值词表只有部分交集（channel 交集仅"直播"，category 交集"美妆/食品"）。于是"换成电商呢"这类用例的期望被诚实地定为**拒答**（消解成功 → 取值校验拦截 → 宁缺毋滥），而不是硬凑一个"正确切换"。
2. **裸时间追问的兜底语义**："那上个月呢"不含指标词、时间不在实体词表 → 按新问题透传 → clarifier 反问指标。期望定为 **clarify**（保守反问，不猜指标）而非硬造一个时间改写；带指标词的时间追问（"那本月销售额呢"）则期望独立出数。两类都入集，边界两边都有回归。
3. **"换成X渠道"要带裸实体**：带后缀（"换成电商渠道呢"）会消解出"电商渠道渠道"重复后缀——期望仍可判定但数据集统一用裸实体，避免把解析噪音写进黄金答案。
4. **歧义守卫（评审 D2）与无上下文兜底（评审 D3）首次有端到端用例**：上轮含两个同类型实体 → 保守回退 + 可见说明；首问即"那华北呢" → "无上轮上下文"说明 + 反问。

### 2.5 缓存判定的确定性：条目间 clear，条目内自由组合
- **决策**：每条用例开始时 `cache.clear()`（清条目不清计数器），from_cache 断言只依赖**条目内**的轮次关系。
- **理由**：缓存是进程级单例，若不隔离，"第 N 条的第一轮是否命中"就依赖条目执行顺序——评测结果随排列漂移。隔离后 fu30（boss→south→boss 第三问命中第一问）这类跨角色隔离用例的判定是纯局部的。
- **注意**：cache.stats() 计数器跨条目累计，报告里的命中数用"从_cache 徽章"统计而非计数器差值。

### 2.6 演示层（demo_full.py + web）与评测同源
demo 四场景（指代消解 / 缓存回放 / 澄清挂起恢复 / 三角色行级权限）与评测集的场景一一对应，共用同一套环境硬前置（PROFILE=full + LLM_PROVIDER=mock 写死在导入前，.env 的真实 key 不可达）。web/index.html 的改动收敛在三件事：**缓存命中徽章**（data_badge.from_cache → "⚡ 缓存命中"）、**session_id 接线**（多轮消解在 UI 生效的前提；切换角色即新会话，与"role 参与缓存指纹"的隔离语义一致）、**clarify_pending 最小恢复流**（挂起帧 → 存 thread_id → 下一问自动带 resume）——挂起拓扑下不加这条，UI 会静默无响应。

## 3. 边界与已知的诚实局限

- **时间追问不做骨架继承**：v1 的 context 词表是实体（地区/渠道/品类），时间维度不在其中——"那上个月呢"永远按新问题处理。这是**设计出来的保守**（误判方向的取舍，followup_adr 已论证），本评测集把边界两边都钉进回归，防止未来加时间词表时无感知地改变既有行为。
- **channel 词表漂移未修**：context 的 channel 词表（直播/门店/电商/线下/线上）与数据集实际取值（直播/搜索/推荐/社交/官网）交集仅"直播"，任何渠道切换消解在取值校验处必拒答。属 TAKEOVER 级词表统一问题（两处词表应收敛到 DIMENSION_VALUES 单一来源），本任务不改 app/，用 fu05 把现状钉死。
- **挂起图是 InMemorySaver**：评测与 demo 的挂起条目进程内即焚，跨重启恢复依赖换持久 checkpointer（clarify_suspend_adr 已论证，接口不变）。
- **mock 评测验证的是流水线正确性**，不是 LLM 质量——切真模型后 gold SQL 判定仍适用（判的是执行结果），但 kind 类预期（clarify/refusal）依赖模型行为，需按 v1 的真模型评测口径另跑。
