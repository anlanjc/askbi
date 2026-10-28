# E2 · 白板重写 check_numbers（数值质检）

> 讲义：`docs/lectures/L3_数值质检.md` ｜ 对应 STUDY_GUIDE 第 6 节 E2 ｜ 判卷改编自 `tests/test_qa_check.py`

## 任务

蒙住 `app/agents/nodes.py` 的数值核验段（约 nodes.py:364-417），在 `skeleton.py` 里重写三件套：

1. `_collect_numbers`：递归收集 dict/list/tuple 里的数字，**先排除 bool**，每个数同时以 4 位与 2 位精度入集合；
2. `_match_allowed`：**分域**匹配——`is_pct=True` 时候选是 `(a*100, -a*100)`，否则 `(a, -a)`；容差 `max(floor, |c|×1.1%)`，floor 百分比域 0.061、原值域 0.005；
3. `check_numbers` 主流程：建值域（rows + facts + row_count）→ **先剥时长再剥日期** → 抽数（千分位剥离）→ 分域匹配 → 出 issues 与 checked 计数。

三个正则（`_NUM_RE` / `_DATE_RE` / `_DUR_RE`）已在 skeleton 给定——考的是流程与分域思想，不是背正则。

## 提示

- 两个踩坑点就是两个考点：跨域误配（编造的 5.23 撞上真实的 0.0523）和日期/时长误报（"近12个月"的 12）。讲义 L3 第 2、3 节各有完整因果链。
- 为什么先 `_DUR_RE` 再 `_DATE_RE`？想一个日期和时长共存的句子，试试两种顺序。
- `checked` 要统计**所有被抽取**的 token（不管过没过），测试会断言 `checked >= 3` 和 `checked == 1`。

## 验收标准（哪些测试跑绿）

`pytest test_practice.py` 全部 7 条：

- 4 条改编自 `tests/test_qa_check.py`：真数字通过、编造数字拦截（999999）、百分比换算放行（-5.23%）、千分位放行（12,345.67）；
- 3 条按讲义行为扩展：日期+时长豁免（2026-09 / 近12个月 不误报且 checked==1）、编造百分比 8.88% 不能借原值域蒙混、百分比域内编造 42.7% 被拦。

## 验证记录（判卷有效性）

- 参考实现（`solutions/solution.py`）复制进 `skeleton.py` 后：**7 passed in 0.03s**（全绿）；
- 还原 NotImplementedError 版骨架后：**7 failed in 0.35s**（全红，证明测试真的在判卷）。

## 做完后

对照 `solutions/solution.py`，自问三题（讲义 L3 第 8 节有答案）：
1. 每个数为什么存两个精度？
2. floor=0.061 与 0.005 各放过什么、拦住什么？
3. 顺序为什么是 `_DUR_RE.sub(" ", _DATE_RE.sub(" ", narrative))`？
然后在 `practice/README.md` 的完成记录表里写一句"我理解了什么"。
