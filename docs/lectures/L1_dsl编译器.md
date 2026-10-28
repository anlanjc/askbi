# L1 · DSL 编译器精读：为什么 NL2SQL 之前要先过一道 DSL

> 精读对象：`app/data/dsl.py`（约 180 行，全项目最核心的 A 级模块）。
> 配套源码：`app/data/schema.py`（维度白名单）、`app/data/metrics.py`（指标字典）、`app/agents/nodes.py`（调用方）。
> 配套练习：`askbi/practice/e1_compile_sql/`（蒙住源码重写 `compile_sql`，pytest 当裁判）。

---

## 0. 这一讲要回答的三个问题

1. 为什么不让 LLM 直接产 SQL，而是 NL → DSL → SQL 两跳？
2. "白名单校验"到底拦在哪几个位置？每个位置防的是什么攻击/什么故障？
3. `compile_sql` 五段式骨架（selects / group_by / conds / 排序 / limit）为什么这样排？

---

## 1. 先看设计主张（dsl.py 模块注释，dsl.py:1-8）

模块 docstring 写了三条理由，逐条拆开：

| 主张 | 含义 | 如果不这样做会怎样 |
|---|---|---|
| LLM 只产受白名单约束的结构化意图 | LLM 输出的是 `{metric, dimensions, filters, time}` 这样的 JSON，metric/dimension/filter 字段都是**枚举校验**，物理标识符（表名/列名）由编译器渲染 | LLM 幻觉一个表名 `users` 或被提示注入诱导输出 `DROP TABLE`，SQL 直接进库——错误在执行期才暴露，且不可复算 |
| DSL 可解释、可 diff、可单测 | 编译器是纯函数（dsl.py:114 `compile_sql(dsl, role)`，输入两个值、输出 `(sql, caliber)`），没有任何 IO | 直接 NL2SQL 的系统只能端到端评测，无法像 `tests/test_compiler.py` 那样对每个校验点写单元测试 |
| 行级权限、口径、LIMIT 在编译期统一注入 | 权限 WHERE（dsl.py:131-133）、口径说明（dsl.py:154-156）、LIMIT 钳制（dsl.py:151）都发生在 LLM 输出**之后**、SQL 生成**之时** | 若做"SQL 改写层"（先生成任意 SQL 再用解析器补权限），要面对子查询/CTE 绕过的著名难题——改写任意 SQL 是出了名的不可靠 |

一句话：**LLM 永远碰不到物理表名**（STUDY_GUIDE 7.4 的钩子句）。物理标识符只存在于两个地方——`DIMENSIONS`（schema.py:18-27，field→SQL 表达式的硬编码映射）和 `METRICS`（metrics.py:28-77，指标字典里的 `expr` 字段）。

---

## 2. 第一层转换：pydantic 模型即白名单（dsl.py:19-107）

### 2.1 `Ops` 与 `Filter`（dsl.py:19-32）

```python
Ops = Literal["=", "!=", "in", "not in", ">", "<", ">=", "<="]
```

`Filter.field` 的校验器 `_check_field`（dsl.py:27-32）检查 `v not in FILTER_FIELDS`。`FILTER_FIELDS` 就是 `DIMENSIONS` 的别名（schema.py:30：`FILTER_FIELDS: dict[str, str] = {**DIMENSIONS}`）。

防的是什么：测试 `tests/test_compiler.py:28` 用 `Filter(field="1=1; DROP TABLE orders", op="=", value="x")` 验证——字段名不是白名单内的标识符，pydantic 直接抛 `ValidationError`，**这个 filter 根本活不到拼 SQL 那一步**。

### 2.2 `TimeRange` 与时间锚点（dsl.py:35-59）

三种模式：`last_n_months` / `between` / `all`（dsl.py:36）。`to_sql()`（dsl.py:41-55）把时间条件渲染为 DuckDB 的 `strftime('%Y-%m', o.order_date)` 比较。

关键常量：

```python
# 数据截止月：与生成器一致，保证 last_n_months 语义稳定
_END_MONTH = "2026-09"          # dsl.py:59
```

`last_n_months` 的 SQL 是 `>= strftime(..., DATE '2026-09-01' - INTERVAL (n-1) MONTH)`（dsl.py:44-47）。也就是说"上月"永远以数据截止月 2026-09 为锚，**不用 `CURRENT_DATE`**。为什么？因为数据是固定种子生成的 24 个月（截止 2026-09），如果按真实系统时间算"上月"，评测永远查到空集。这是评测迭代修出来的设计（STUDY_GUIDE 4.1 提到 schema_prompt 里的时间锚点是"评测迭代修出来的"）。同一个锚点还写在 `schema_prompt()` 里喂给 LLM（schema.py:55-57："相对时间一律以 2026-09 为基准（上月=本月=2026-09）"）。

### 2.3 `QueryDSL` 的三个校验器（dsl.py:69-99）

| 校验器 | 位置 | 行为 | 为什么 |
|---|---|---|---|
| `_check_metric` | dsl.py:78-83 | `v not in METRICS` → 抛 ValueError | 未知指标=数据集不支持，宁可拒答不可硬查 |
| `_check_dims` | dsl.py:85-91 | 逐个检查 `d not in DIMENSIONS` → 抛 | 同上，维度也是白名单 |
| `_check_limit` | dsl.py:93-99 | `max(1, min(v, MAX_LIMIT))`，**钳制而非报错** | 注释原话（dsl.py:98）："LLM 输出的越界 limit 不值得一次修复循环"——limit=99999 语义仍然明确（要很多行），钳到 `MAX_LIMIT=200`（dsl.py:61）即可；而未知指标是语义错误，必须走拒答/重试 |

这是本项目"错误分型"思想的最小样本：**语义错误（枚举外）→ 报错触发修复循环；程度错误（值越界）→ 静默钳制**。`max(1, ...)` 还顺带挡了 limit=0 / 负数。

### 2.4 行级权限词表 `ROLE_SCOPES`（dsl.py:102-107）

```python
ROLE_SCOPES: dict[str, list[str] | None] = {
    "boss": None,          # 不限
    "south": ["华南"],
    "north": ["华北", "东北"],
}
```

权限=角色→可见区域列表。注意它只在这一个文件定义，编译器是唯一消费点——权限没有散落在 prompt 或 API 层。

---

## 3. 第二层转换：`compile_sql` 逐段精读（dsl.py:114-157）

先给骨架（白板重写就默写这个）：

```python
def compile_sql(dsl: QueryDSL, role: str = "boss") -> tuple[str, str]:
    metric = METRICS[dsl.metric]                       # ① 取指标定义
    scope = ROLE_SCOPES.get(role)                      # ② 取角色范围
    if scope is None and role != "boss":               #    未知角色 → CompileError
        raise CompileError(f"未知角色: {role}")         #      dsl.py:122-123
    selects = [维度列...] + [指标表达式 AS value]        # ③ dsl.py:126
    group_by = [维度表达式...]                          # ④ dsl.py:127
    conds = [时间条件] + [权限 IN] + [filter 条件...]    # ⑤ dsl.py:129-144
    sql = SELECT ... FROM {BASE_FROM} WHERE ...         # ⑥ dsl.py:146-148
        [+ GROUP BY] [+ ORDER BY value] [+ LIMIT]      #    dsl.py:149-152
    caliber = 口径说明 + 权限范围                        # ⑦ dsl.py:154-156
    return sql, caliber
```

### 3.1 ①② 指标与角色（dsl.py:120-123）

`METRICS[dsl.metric]` 拿到 `MetricDef`，其 `.expr` 是权威口径 SQL（如 revenue = `SUM(oi.quantity * oi.unit_price * (1 - oi.discount))`，metrics.py:12-13, 33）。"毛利率怎么算"在这个系统里有唯一答案，因为表达式只有一份、存在指标字典里（metrics.py 模块注释：借鉴 WrenAI MDL 语义层）。

未知角色的处理是 **raise** 而不是降级为 boss：角色是调用方（API/评测）传的系统参数，传错属于程序 bug，静默放宽权限反而危险。对比 limit 的钳制—— again 错误分型。

### 3.2 ③④ selects / group_by（dsl.py:126-127）

```python
selects = [f"{DIMENSIONS[d]} AS {d}" for d in dsl.dimensions] + [f"{metric.expr} AS value"]
group_by = [DIMENSIONS[d] for d in dsl.dimensions]
```

两个细节：
- 列别名用 DSL 字段名（`AS region`），指标列固定叫 `value`——下游（`_build_chart`，nodes.py:333-346）按 `rows[: ][: -1]` 是维度、`r[-1]` 是指标的约定取数，不必解析列名。
- group_by 用的是**表达式本身**（`p.category`），不是别名。DuckDB 允许 GROUP BY 别名，但写成表达式与 MySQL 等方言兼容性更好。

### 3.3 ⑤ conds：三个注入点（dsl.py:129-144）

**注入点 A：时间**（dsl.py:130）——`conds.append(dsl.time.to_sql())`，永远第一个条件，`mode="all"` 时是 `TRUE` 占位，保证后面 `AND` 拼接不会语法错。

**注入点 B：行级权限**（dsl.py:131-133）：

```python
if scope is not None:
    vals = ", ".join(_q(v) for v in scope)
    conds.append(f"o.region IN ({vals})")
```

权限 WHERE 在**编译期**拼进主查询。测试 `tests/test_compiler.py:39-41` 验证 south 角色产出 `o.region IN ('华南')`，且口径说明（caliber）里带"华南"。为什么不做在 SQL 改写层？——改写要解析任意 SQL 结构（子查询、CTE 都可能改写行语义），编译期注入则从源头保证**任何**由本编译器产出的 SQL 都带权限。LLM 输出里根本没有"权限"这个概念的字段，想绕都没有入口。

**注入点 C：filter 渲染**（dsl.py:134-144），三分支：

```python
for f in dsl.filters:
    col = FILTER_FIELDS[f.field]                    # 白名单列名
    if f.op in ("in", "not in"):
        if not isinstance(f.value, list) or not f.value:
            raise CompileError(...)                 # in 需要非空列表  dsl.py:137-138
        vals = ", ".join(_q(v) for v in f.value)
        conds.append(f"{col} {f.op.upper()} ({vals})")
    elif isinstance(f.value, list):
        raise CompileError(...)                     # 标量 op 不接受列表  dsl.py:141-142
    else:
        conds.append(f"{col} {f.op} {_q(f.value)}") # 标量走 _q 转义  dsl.py:144
```

### 3.4 值转义 `_q()`：为什么"参数化不现实"还要双保险（dsl.py:160-165）

```python
def _q(v: Any) -> str:
    """白名单校验 + 转义。数字直出；字符串校验枚举/格式后加引号。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"
```

docstring 里那句"纵深防御"要能复述（dsl.py:117-118）：

1. **第一道：枚举校验**。region/category/channel/member_level/status 这些枚举字段的取值，在节点层被 `validate_filter_values()`（dsl.py:168-179）比对 `DIMENSION_VALUES`（schema.py:38-44），不在取值内的直接拒答（nodes.py:203-210："宁缺毋滥，不用编造数据回答"）。所以正常链路里传进 `_q` 的字符串值都来自白名单枚举。
2. **第二道：引号转义**。`'` → `''`。测试 `tests/test_compiler.py:31-35`：`value="华南' OR '1'='1"` 产出条件 `'华南'' OR ''1''=''1'`——注入串退化成一个不存在的区域名，查询返回空集而非全表。

为什么不用参数化查询（`?` 占位符）？源码注释给出真实约束：DuckDB 的线程限制（dsl.py:117"值参数化不现实（DuckDB线程限制）"）——本项目在 `asyncio.to_thread` 里执行查询、每次新建连接，参数绑定与连接生命周期在这套并发模型下收益为负。同时注意 `isinstance(v, bool)` 的排除（dsl.py:162）：Python 里 `bool` 是 `int` 子类，`True` 会被 `str()` 成 `"True"` 造成语法错误，必须先排除。

### 3.5 ⑥⑦ 组装与 LIMIT 钳制（dsl.py:146-157）

```python
sql = f"SELECT {', '.join(selects)}\nFROM {BASE_FROM}\nWHERE {' AND '.join(conds)}"
if group_by:
    sql += "\nGROUP BY " + ...
ob = dsl.order_by or OrderBy(metric=dsl.metric, direction="desc")   # 默认排序  dsl.py:149
sql += f"\nORDER BY value {ob.direction.upper()}"
limit = min(dsl.limit or 100, MAX_LIMIT)                            # 默认100，上限200  dsl.py:151
sql += f"\nLIMIT {limit}"
```

- `BASE_FROM` 是硬编码的四表 join（schema.py:11-15）——**FROM 子句是常量**，LLM 没有任何途径换表。
- LIMIT 有两层钳制：pydantic 校验器钳一次（dsl.py:99），组装时 `min(dsl.limit or 100, MAX_LIMIT)` 再兜一次（dsl.py:151）——即使绕过 pydantic（例如内部代码直接构造 dict），上限仍生效。防御不信任单点。
- 返回值第二个元素 `caliber` 是口径说明（dsl.py:154-156），最终随答案输出给用户（finalizer 把它放进 `answer["caliber"]`，nodes.py:483）。**口径治理不是文档，是每条回答自带的字段。**

### 3.6 `validate_filter_values`：独立于编译的取值校验（dsl.py:168-179）

它**不**在 `compile_sql` 内部调用，而是由 `sql_agent_node`（nodes.py:203-210）在编译前调用，返回问题列表而非抛异常。为什么分离？——取值错误（如"火星区域"）业务上要的是**带候选项的拒答**（"可选：华南/华北/…"），编译错误要的是异常路径；返回 list 让调用方能拼接出人类可读的拒答理由。这也让校验规则可以被 mock 解析器复用（mock.py:148-150 同样调用它）。

---

## 4. 常见错误实现对比（面试"换种写法会怎样"素材）

| 错误写法 | 后果 | 正确做法（源码位置） |
|---|---|---|
| 让 LLM 直接生成列名/表名，正则事后过滤 | 过滤器永远追不上表达方式（别名/注释/Unicode 变体）；漏一个就漏到执行期 | 标识符只来自 `DIMENSIONS`/`METRICS` 硬编码映射（schema.py:18-27、metrics.py:28-77） |
| limit 越界抛 ValidationError | 触发一次昂贵的 LLM 修复循环，而语义上钳到 200 没有信息损失 | 钳制 `max(1, min(v, MAX_LIMIT))`（dsl.py:93-99） |
| 权限做成"生成后正则删除敏感列" | 子查询/CTE/列别名即可绕过；改写任意 SQL 不可靠 | 编译期拼 `o.region IN (...)`（dsl.py:131-133） |
| 只在 `_q` 转义、不做取值白名单 | `'华南'' OR ''1''=''1'` 虽然安全，但业务上会返回"空数据"让用户误以为没数据 | `validate_filter_values` 前置拒答（dsl.py:168-179 + nodes.py:203-210） |
| GROUP BY 用位置 `GROUP BY 1,2` | DSL 维度顺序变化时 SQL 语义漂移，且可读性差 | 显式表达式（dsl.py:127） |
| 口径说明写在 prompt 里让 LLM 自己说 | 同一问题两种说法，无法审计 | `caliber` 由编译器从指标字典渲染（dsl.py:154-156） |

---

## 5. 面试讲法

**30 秒版**：
> 我做的是 NL2DSL2SQL 三段式。LLM 只输出一个受 pydantic 白名单约束的查询意图 JSON——指标、维度、过滤字段全是枚举校验；物理表名列名只在两个硬编码映射里，SQL 由一个纯函数编译器渲染。行级权限、口径说明、LIMIT 上限都在编译期统一注入，所以提示注入在编译期就不可达。附带的好处是编译器可单测——我的 8 条编译器测试全是毫秒级纯函数测试。

**3 分钟版**（在 30 秒版基础上展开）：
- 第一分钟：讲三段式动机。直接 NL2SQL 的三个痛点：幻觉表名不可控、SQL 写法多样无法单测、权限只能事后改写（改写任意 SQL 是著名难题）。DSL 把"LLM 擅长的语义理解"和"必须确定性的标识符/权限/口径"切开。
- 第二分钟：讲错误分型。未知指标/维度/字段=语义错误，pydantic 抛错→节点层区分"数据集不支持→拒答"和"其余校验错→带错误重试一次"（nodes.py:181-201）；limit 越界=程度错误，钳制不报错（dsl.py:98 注释：不值得一次修复循环）。取值校验 `validate_filter_values` 独立于编译，产出"可选值列表"式的友好拒答。
- 第三分钟：讲纵深防御的真实取舍。参数化查询在 DuckDB+asyncio.to_thread 的并发模型下不现实，所以值走"枚举白名单+引号转义"双保险（`_q`，dsl.py:160-165），并解释 `' OR '1'='1` 实际会退化成查不存在的区域名。收尾：连接层还有物理只读兜底（引出 L2）。

---

## 6. 自测五题（答案见文末折叠区）

1. `QueryDSL` 的 `limit` 校验器和 `compile_sql` 末尾的 `min(dsl.limit or 100, MAX_LIMIT)` 是重复防御吗？分别防什么？
2. 把行级权限从编译期注入改为"执行前用 sqlglot 解析 SQL 并插入 WHERE"，会出现哪类绕过？
3. `_q()` 里为什么先判断 `isinstance(v, bool)` 再判断数字？
4. `validate_filter_values` 为什么返回 `list[str]` 而不是抛异常？它和 pydantic 的枚举校验职责有何不同？
5. `TimeRange.to_sql()` 为什么用固定常量 `_END_MONTH` 而不是 `CURRENT_DATE`？在什么前提下这个选择是对的？

<details>
<summary><strong>答案（先自己答再看）</strong></summary>

1. 不是重复。pydantic 校验器（dsl.py:93-99）防的是**LLM 输出**的越界 limit——在 DSL 构造时钳制，越界值根本进不了系统；`min(...)`（dsl.py:151）防的是**所有其他来源**（内部代码、未来新增的构造路径），是组装时刻的兜底。纵深防御不信任单一校验点。两处的语义也不同：校验器钳到 `[1, 200]`（含下限 1），组装处的 `or 100` 还负责"未设 limit 时默认 100 行"。
2. 三类：(a) 子查询/CTE 内的行语义——外层 WHERE 加了权限不代表内层派生表没泄数据；(b) 别名/注释/编码变体让解析器认不出该插哪；(c) 解析器版本差异导致的解析失败路径——失败后是放行还是拒绝？编译期注入没有这个问题：LLM 根本没有"写 SQL"的能力，权限是拼进去的常量（dsl.py:131-133）。
3. Python 中 `bool` 是 `int` 的子类，`isinstance(True, int)` 为 True。若不先排除，`str(True)` 产出 `"True"` 拼进 SQL 是语法错误（DuckDB 布尔字面量是小写 true），而且 `True/False` 本来就不该作为查询值出现。
4. 取值校验失败的**业务动作**是"带候选项拒答"，需要把所有问题收集齐一次性反馈给用户（"过滤值 region=火星 不在数据集取值内（可选：华南/华北/…）"，dsl.py:178）；抛异常只能拿到第一条。pydantic 校验的是**字段标识符**（有没有这个字段），它校验的是**字段取值**（这个字段的值在不在数据集里）——前者是语法错误，后者是业务数据错误。
5. 因为数据集是固定种子生成的 24 个月模拟数据，截止 2026-09（dsl.py:59 注释："与生成器一致"）。前提是"查询的库内容固定、有明确截止月"。接生产数仓时这里应换成 T-1 之类的动态锚点——这正是 STUDY_GUIDE 7.3 说的"接生产数仓只需 DSL 编译器换 target"的一部分。
</details>

---

## 7. 动手环节

打开 `askbi/practice/e1_compile_sql/`，按 README 流程蒙住本讲义与源码，重写 `compile_sql` 骨架，用 `test_practice.py` 判卷。
