# E3 · 白板重写最小图（LangGraph 状态机）

> 讲义：`docs/lectures/L4_状态机.md` ｜ 对应 STUDY_GUIDE 第 6 节 E3 ｜ 判卷改编自 `tests/test_graph.py`

## 任务

蒙住 `app/agents/graph.py` 与 `app/agents/nodes.py`，在 `skeleton.py` 里重搭最小图：

```
START → router → clarifier ─clarify/refuse→ finalizer → END（捷径）
                    └clear→ sql_agent ─refuse→ finalizer（捷径）
                              └ok→ executor ─ok→ finalizer
                                              └error→ 修复回路（加分项，mock 下测不到）
```

- 纯代码节点：`router`（预算路由正则）、`executor`（沙箱执行）、`finalizer`（三形态组装）；
- LLM 节点：`clarifier` / `sql_agent`——**走 mock provider**（CI 确定性），调用必须用 `_call_llm(node, payload)`，node 名为 `"clarifier"` / `"sql_agent"`（mock 按 node 名分发规则处理器，见 `app/llm/mock.py` 的 `_HANDLERS`）；
- 三条条件边函数 `_after_clarifier` / `_after_sql_agent` / `_after_executor`；
- `build_minigraph()` 装配：StateGraph + add_node + 固定边 + add_conditional_edges。

状态、`_event`/`_merge_events`/`_loose_json`/`_call_llm`/`run_ask` 已给定（skeleton 顶部"必须遵守的契约"逐条列了各节点要写的 state 键）。

## 提示

- **events 是追加不是覆盖**：LangGraph 对非 reducer 字段做整体替换，节点必须 `_merge_events(state, ...)` 带回旧事件——两个事件类测试都靠它判卷。
- 条件边的判定信号是 state 里的**量化字段**（clarify/refusal/sql_error），不是模型自由发挥。
- mock 的 `_sql_agent` 返回**已校验的 DSL dict** 或 `{"error": ...}`；你的 sql_agent 节点仍要自己跑 `QueryDSL(**raw)` + `compile_sql`——mock 只替代 LLM，不替代编译器。
- executor 失败时 repair_count 递增、超限置 refusal（原版 nodes.py:242-251 的简化版）。

## 验收标准（哪些测试跑绿）

`pytest test_practice.py` 全部 6 条：

- 3 条改编自 `tests/test_graph.py`：简单查询（answer+rows+select 开头+口径）、澄清流（clarify+options）、拒答流（refusal）；
- 3 条按契约扩展：拒答捷径（events 中不得出现 sql_agent/executor）、answer 路径全节点覆盖（router 开头、finalizer 结尾）、router 纯代码预算路由（full/light 直判）。

## 验证记录（判卷有效性）

- 参考实现（`solutions/solution.py`）复制进 `skeleton.py` 后：**6 passed in 50.56s**（全绿；首跑含练习库生成，二次运行约 2s）；
- 还原 NotImplementedError 版骨架后：**6 failed in 0.85s**（全红，证明测试真的在判卷）。

## 做完后

对照 `solutions/solution.py` 与 `app/agents/graph.py`，自问（讲义 L4 第 8 节有答案）：
1. 为什么条件边先判 refusal 再判 sql_error？
2. 最小图砍掉了 insight/qa，真实图里 light/full 的 LLM 调用次数差多少？
3. 你的 clarifier 在 JSON 解析失败时默认放行还是拦截？为什么？
然后在 `practice/README.md` 的完成记录表里写一句"我理解了什么"。
