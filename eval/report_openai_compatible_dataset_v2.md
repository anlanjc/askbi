# AskBI v2 评测集真实模型汇总（deepseek-v4-flash-0731 @ DashScope 兼容接口）

> 日期：2026-10-29 ｜ 评测集：eval/dataset_v2.json（116 条分层集：80 query / 12 drill / 12 clarify / 12 refusal，13 条 holdout）
> 判分：query=执行准确率（结果集与 gold 同库比对）· drill=叙述+QA 通过 · clarify/refusal=答案 kind。
> 成本：全量 560,550 token ≈ ¥1.7；另有中断重试与子集复跑合计 ≈ ¥1.5（诚实计入，一次后台中断未产出报告）。

## 1. 全量基线（clarifier prompt 校准前）

- 总准确率 **94.8%（110/116）**，叙述数值一致率 **100%**
- 延迟 avg 41.0s / p95 113.2s（heavy 链路含 QA 修订循环）
- 分类别：query **79/80** ｜ drill **12/12** ｜ clarify **12/12** ｜ refusal **7/12**
- 角色分层：boss 101/106 · south 5/5 · north 4/5（行级权限端到端可用）

## 2. 失败归因与修复（只用 main split 失败信号，holdout 不参与调参）

| 失败 | 归因 | 处置 |
|---|---|---|
| v2-078（role=north 渠道趋势） | 结果 30/30 行不一致；单条复跑 DSL 正确且 match gold=True → **真实模型单次 DSL 漂移**（与 agenteval judge 校准记录的"运行间波动"同构） | 记录为已知现象，不修 |
| v2-108/110/114/116（refusal→clarify 漂移） | 真实模型 refuse 边界不一致：预测词（预计 vs 预测）、未采集指标（满意度）、非查询动作（写/推荐/评价）被判成"指标不明"反问 | clarifier prompt 增补 4 类 refuse 规则+判定顺序+边界示例（仅用 main split 的 4 条失败信号） |
| v2-115（refusal，**holdout**） | 同上模式（调参时未看此条） | 盲测通过 |

**校准后回归**：refusal 子集 **12/12=100%**、clarify 子集 **12/12=100%**（无回归），pytest 94 全绿。

## 3. Holdout 盲测（prompt 校准后，两轮）

- run1：11/13=84.6%（两条 drill 失败——主跑时均为 ✓ 的同题，属链路运行间波动）
- run2：**13/13=100%**
- 结论：holdout 真实口径 ≈ **92.3%±波动**，失败集中于 drill 类；无系统性新问题。

## 4. 诚实声明

- mock 116/116 是**词表可判定集**的上限证明；真实模型 94.8%（基线）→ 校准后子集全绿 + holdout 92.3%±波动。
- 运行间波动是真实模型评测的固有属性（本集与 agenteval judge 校准两处独立复现）；单次全量数字不应过度解读。
- 明细报告：`report_mock_dataset_v2.md`（mock 全量）· `report_openai_compatible_dataset_v2_refusal_calibrated.md` · `..._clarify_recheck.md` · `..._holdout_run1.md` / `..._holdout.md`（run2）。
