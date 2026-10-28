# ADR：数据源注册表——语义层从硬编码到可注册（AB-3）

> 状态：已实现（2026-10-29）｜实现：app/data/registry.py + dataset_b.py + generator_b.py + tests/test_registry.py（15 项）
> 全量测试 110 项全绿（95 项既有**零改动** + 15 新增）；回归裁判：v1 smoke 33 条 mock 100%、v2 full 116 条 mock 100%；B 集独立评测 26 条 mock 100%

## 1. 我为什么做注册表

AskBI 的核心卖点是"语义层受控"：LLM 只产 DSL，SQL 由编译器从指标字典/维度白名单渲染。但改造前，这个语义层**硬编码在 schema.py/metrics.py 的模块级常量里**——DSL 校验器、编译器、提示词、mock 词表全部直接 import 这些全局量。"支持多数据源接入"这行简历字写不出口，因为换一套数据要改散落在 7 个文件里的白名单。

这个 ADR 的目标是最硬的实证形式：**新增一套物理表结构完全不同的数据集（线下连锁咖啡：tickets/ticket_lines/stores/skus，而电商是 orders/order_items/products/customers），上层代码零改动**——图结构、节点、QA 闭环、评测 runner、MCP 接口一行 if 都不加。做不到就说明"语义层解耦"是假的。

## 2. 我考虑过的三个方案

| 方案 | 结论 | 放弃/采用原因 |
|---|---|---|
| DatasetSpec dataclass + 注册表（**采用**） | 语义层变数据，消费方加默认参数 | 增量式：DEFAULT_SPEC 直接引用原常量对象，默认路径逐位不变；可回滚性最好 |
| DSL 模型多态化（每数据集一个 QueryDSL 子类） | 放弃 | pydantic 校验器绑定类定义期，动态子类生成复杂；且"DSL 结构全数据集同构、白名单不同"是事实，多态是过度设计 |
| 配置文件（JSON/YAML）声明语义层 | 放弃（本期） | 引入 schema 校验与加载失败模式；spec 里有 SQL 表达式，代码形态比字符串配置更可测试。将来接非程序员维护的语义层时再外置 |

## 3. 逐位不变是怎么保证的（回归裁判）

红线是"DEFAULT 行为逐位不变"。四层证据：

1. **对象同一性**：DEFAULT_SPEC 不复制——`dimensions is DIMENSIONS`、`metrics is METRICS`（test_default_spec_shares_original_objects）。改了 registry 不可能悄悄改变默认行为。
2. **prompt 逐字节**：schema_prompt() 与硬编码时期的输出逐字节相等（test_default_schema_prompt_bit_identical 内嵌原文对照）。
3. **物理标识符**：默认编译输出仍含 `FROM orders o JOIN order_items oi`、`strftime('%Y-%m', o.order_date)`、权限注入 `o.region IN ('华南')`（test_default_compile_uses_original_identifiers）。
4. **存量全绿**：95 项既有测试零改动通过 + v1/v2 双评测集 mock 100% 重跑。

实现手法：所有新参数一律**默认参数**（`compile_sql(dsl, role, spec=None)`），None 时解析到 DEFAULT_SPEC；pydantic 校验器经 `ValidationInfo.context` 取 spec，**无 context 时回退模块全局**——历史调用点 `QueryDSL(**raw)` 的行为没有任何路径可以改变。

## 4. spec 边界画在哪里（什么进 spec、什么保持全局）

**进 spec（随数据集走）**：物理层（base_from/db_path/版本探测 SQL）、白名单（dimensions/filter_fields/dimension_values/metrics）、时间语义（time_field/start_month/end_month）、NL 词表（mock 解析的指标别名序/维度关键词/取值提取关键词）、归因分解维度（drill_dims）。

**保持全局（有意为之）**：
- **行级权限角色表 ROLE_SCOPES**：角色是组织级概念，不是数据集属性。作用域维度全局约定为 `region`，两个数据集共享同一七大区划分（全国连锁的咖啡店与全国发货的电商，大区语义一致）。若未来接入无 region 维的数据集，编译期显式 CompileError 拒绝（不静默放行全量数据）。
- **掩码列 MASKED_FIELDS**：纵深防御不随数据集放松——新数据集引入敏感列时应收紧而不是依赖每套 spec 自己声明。
- **mock 拒答词表**（满意度/复购率/投诉等）与澄清候选：跨业务域通用的"未采集指标"，枚举在 spec 里只会重复。
- **DSL 结构本身**：intent/metric/dimensions/filters/time 的形状全数据集同构——这正是"换源不改上层"的前提。

## 5. 三个踩过的失败模式（诚实记录）

1. **权重和不为 1 的渠道迁移**：外卖占比 15%→45% 的故事信号，首版堂食常数写错导致三项权重和恒为 0.85，归一化后首月外卖就 17.4%、末月 54.2%——方向对但幅度全错。教训：**合成数据的故事信号要和设计值对账**（修复后 14.8%→29.6%→47.3%，与设计权重逐点核对）。
2. **惰性注册撞上 Python 模块缓存**：reset_specs() 清空注册表后想靠"重新 import dataset_b"恢复内置 spec——但模块导入一次后是缓存的，自注册代码不会重跑，coffee 从此"消失"。修复：reset 直接重注册（这解释了为什么第一批注册表测试 7 连炸且顺序敏感）。
3. **裸 Sandbox() 的第三个调用点**：drill_down 里月度序列查询漏传 db_path，B 数据集的归因查询落到 A 库直接 Catalog Error。教训：**贯穿参数要 grep 收尾**（`grep -n "Sandbox(" analytics.py`）——漏一个就是运行期才炸的错源。

## 6. 缓存的跨源隔离

缓存键（规范化 DSL 指纹）新增 `ds`（数据集名）字段：同 DSL 不同源 SQL 完全不同，**绝不跨源命中**。数据版本探测 SQL 也随 spec 走（orders/order_items vs tickets/ticket_lines，探测错表直接报错而不是返回假版本号）。编译语义版本（semver）按数据集分键——A 的指标口径变更不应误伤 B 的缓存。注意：指纹哈希值相对旧版全变（键是不可透明哈希，命中/未命中语义不变，且缓存仅在进程内存活，无跨版本持久化）。

## 7. 已知边界（下一步）

- **RAG 样例库未按数据集隔离**：B 数据集的问题在真实 provider 下会检索到电商的 few-shot 样例（mock 不受影响）。样例库加 dataset 维度是下一步。
- **多轮指代消解词表仍取默认数据集**：context.py 的实体词表在模块导入时从全局 DIMENSION_VALUES 构建，B 数据集的多轮消解（"那外卖呢"）暂不支持——followup 本身默认关闭，边界如实声明。
- **评测集 B 全部 main split**：26 条用于多数据源回归验证，无调参流程故不设 holdout；扩大到 v2 同等规模时应引入分层。
