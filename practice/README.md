# AskBI 白板重写练习场（practice/）

> **先自己写，跑不过再看 `solutions/`。** 参考实现是判卷后的对照答案，不是起点——先看答案的"练习"只会制造已经学会的错觉。

这是 `docs/STUDY_GUIDE.md` 第 6 节"手写重造计划"的可判卷版本：把 A 级模块蒙住源码重写，pytest 当裁判。对应关系：

| 练习 | 对应 STUDY_GUIDE | 讲义 | 重写对象 | 判卷测试 |
|---|---|---|---|---|
| `e1_compile_sql/` | E1（6 节表格） | `docs/lectures/L1_dsl编译器.md` | `app/data/dsl.py` 的 DSL 模型 + `compile_sql` | `tests/test_compiler.py` 核心用例改编 |
| `e2_check_numbers/` | E2（6 节表格） | `docs/lectures/L3_数值质检.md` | `app/agents/nodes.py` 的 `check_numbers` 三件套 | `tests/test_qa_check.py` 核心用例改编 + 豁免规则扩展 |
| `e3_minigraph/` | E3（6 节表格） | `docs/lectures/L4_状态机.md` | LangGraph 最小图（router/clarifier/sql_agent/executor/finalizer + 拒答捷径） | `tests/test_graph.py` 核心用例改编 |

## 做题流程（每个练习都一样）

1. **读讲义**：先读对应 lecture，合上讲义能说出"这模块防什么/为什么这么写"再动笔。
2. **看签名**：打开 `skeleton.py`，只读函数签名、docstring 和 TODO 注释——docstring 就是行为规格，写的代码必须恰好满足它。
3. **自己写**：**不要**打开 `app/` 源码对照着抄；卡住了回讲义找线索，讲义没有的（比如某列名）允许查 schema。
4. **跑测试**：`../.venv/Scripts/python.exe -m pytest test_practice.py -v`（在练习目录内）。
5. **对照 solution**：全绿后打开 `solutions/solution.py`，逐行 diff 你的实现，重点看：你漏了哪个防御点、哪个分支、哪个注释里的"为什么"。
6. **写一句"我理解了什么"**：在下面"完成记录"表格里补一行。说不出来就说明还没懂，回到第 1 步。

## 验证记录（判卷有效性已被验证）

每个练习在交付前都跑过两轮：

| 练习 | solution 放入 skeleton | 还原 NotImplementedError 骨架 | 结论 |
|---|---|---|---|
| e1_compile_sql | 10 passed | 10 failed | 测试真的能判卷 |
| e2_check_numbers | 7 passed | 7 failed | 测试真的能判卷 |
| e3_minigraph | 6 passed | 6 failed | 测试真的能判卷 |

（具体 pytest 输出见各练习 README。）

## 环境说明

- 统一用项目自带虚拟环境：`askbi/.venv/Scripts/python.exe -m pytest`。
- `practice/conftest.py` 负责把 `app/` 包路径加进 `sys.path`、强制 mock LLM、并按需生成练习专用测试库（`practice/_db/`，独立于 `tests/_db/`，不碰既有文件）。
- 测试文件 `test_practice.py` 的 import 全部指向 `skeleton.py`——你写的代码就是被测对象；`app/` 源码只读不写。

## 完成记录

| 日期 | 练习 | 一句"我理解了什么" |
|---|---|---|
| | | |
