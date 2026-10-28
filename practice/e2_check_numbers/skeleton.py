"""E2 · 白板重写 check_numbers 数值质检（对应 app/agents/nodes.py，讲义 docs/lectures/L3_数值质检.md）。

给定（不许改）：
- 三个正则 _NUM_RE / _DATE_RE / _DUR_RE（抽数与豁免规则已定，考的是用它们的流程）；
- _collect_numbers / _match_allowed / check_numbers 的函数签名。

行为规格（务必逐条满足）：
1. _collect_numbers(value, out)：递归 dict/list/tuple；int/float（先排除 bool）同时以
   round(v, 4) 和 round(v, 2) 两个精度加入 out；
2. _match_allowed(val, allowed, is_pct)：
   - 分域！is_pct=True 时候选是 (a*100, -a*100)，否则 (a, -a)，两域绝不互通；
   - 容差 = max(floor, |c| * 0.011)，其中 floor = 0.061（is_pct）或 0.005（原值域）；
   - 任一候选落在容差内即 True；
3. check_numbers(narrative, result, facts) -> (issues, checked)：
   - 值域三个来源：result["rows"]、facts、result["row_count"]（非 None 时）；
   - 先 _DUR_RE.sub 再 _DATE_RE.sub 清洗叙述，顺序不能反（日期可能是时长的前缀场景）；
   - 逐 token：float(tok.replace(",", ""))；checked 计数；未命中 -> issues 追加
     "叙述中的数字 {tok}{% if pct} 未出现在查询结果或计算事实中（疑似编造）"；
   - 返回 (issues 列表, checked 总数)。
"""

from __future__ import annotations

import re
from typing import Any

# ---- 给定：三个正则（与 app/agents/nodes.py:364-368 一致） ----

_NUM_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(%?)")
# 日期（2026-09 / 2026年9月）与时长（近12个月 / 6个月）不参与数值核验：
# 它们是时间坐标/量词，不是业务数值
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?")
_DUR_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:个?月|年|天|日|小时|分钟|周|季度)")


# ---------------- TODO 1: 值域收集 ----------------


def _collect_numbers(value: Any, out: set[float]) -> None:
    """递归收集数字。注意：bool 是 int 子类，要先排除；每个数存 4 位与 2 位两个精度。"""
    raise NotImplementedError


# ---------------- TODO 2: 分域容差匹配 ----------------


def _match_allowed(val: float, allowed: set[float], is_pct: bool) -> bool:
    """数值匹配：带 % 的 token 只对百分比域，否则只对原值域，杜绝跨域误配。

    - 百分比域容差 max(0.061, |c|×1.1%)：放过 0.3%↔0.35% 这类 1 位小数舍入；
    - 原值域容差 max(0.005, |c|×1.1%)：足以放过千分位/4 位小数表达，
      拦得住 8% 量级的数值篡改（如 0.5006 → 0.5406）。
    """
    raise NotImplementedError


# ---------------- TODO 3: 主函数 ----------------


def check_numbers(narrative: str, result: dict, facts: dict) -> tuple[list[str], int]:
    """确定性核验：叙述中的每个数字必须能在结果集/事实中找到（含百分比换算）。

    流程（讲义 L3 第 1-4 节）：建值域 → 剥日期/时长 → 抽数 → 分域匹配 → 出 issues。
    返回 (issues, checked)。
    """
    raise NotImplementedError
