# E1 · 白板重写 compile_sql（DSL 编译器）

> 讲义：`docs/lectures/L1_dsl编译器.md` ｜ 对应 STUDY_GUIDE 第 6 节 E1 ｜ 判卷改编自 `tests/test_compiler.py`

## 任务

蒙住 `app/data/dsl.py`，在 `skeleton.py` 里重写 DSL 编译器核心：

1. 三个 pydantic 模型的白名单校验器（`Filter._check_field` / `QueryDSL._check_metric` / `_check_dims`）；
2. `QueryDSL._check_limit`——**钳制而非报错**（想想为什么）；
3. `TimeRange.to_sql()`——last_n_months 以 `_END_MONTH` 为时间锚；
4. `_q()` 值转义——先排除 bool，字符串 `'`→`''`；
5. `compile_sql()` 五段骨架：取指标与角色 → selects/group_by → conds（时间/权限 IN/filter）→ 组装+LIMIT 钳制 → caliber；
6. `validate_filter_values()` 取值校验（返回问题列表，不抛异常）。

词表（METRICS/DIMENSIONS/BASE_FROM 等）允许直接 import——考的是编译器层，不是背词表。

## 提示

- 先在纸上默写五段骨架，再动键盘（讲义 L1 第 3 节开头就是骨架）。
- conds 的拼接顺序：时间 → 权限 → filters；`mode="all"` 时时间是 `"TRUE"` 占位，保证 AND 拼接语法成立。
- in/not in 与标量 op 对 value 类型的要求**相反**，别写成一个分支。
- caliber 的拼接格式看 skeleton 顶部"行为规格"第 4 条，测试会检查口径里不含"毛利"（revenue 指标）。

## 验收标准（哪些测试跑绿）

`pytest test_practice.py` 全部 10 条：

- 8 条改编自 `tests/test_compiler.py`：未知指标/维度/过滤字段拒绝、注入值转义、south 角色权限注入、limit 钳到 200、上月时间锚点 2026-09、GROUP BY+ORDER BY+口径；
- 2 条按行为规格扩展：标量 op 收到列表值抛 `CompileError`；"火星"区域被 `validate_filter_values` 标记、合法区域返回空列表。

## 验证记录（判卷有效性）

- 参考实现（`solutions/solution.py`）复制进 `skeleton.py` 后：**10 passed in 0.05s**（全绿）；
- 还原 NotImplementedError 版骨架后：**10 failed in 0.37s**（全红，证明测试真的在判卷）。

## 做完后

打开 `solutions/solution.py` 与 `app/data/dsl.py` 对照 diff，回答：你的实现漏了哪个防御点？`_q` 里 bool 排除、limit 双层钳制、未知角色 raise 这三处你都写对了吗？然后在 `practice/README.md` 的完成记录表里写一句"我理解了什么"。
