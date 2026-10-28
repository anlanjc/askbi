"""E2 参考实现 —— 与 app/agents/nodes.py 的 check_numbers 三件套逐行为对应（先自己写，跑不过再看）。"""

from __future__ import annotations

import re
from typing import Any

_NUM_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(%?)")
# 日期（2026-09 / 2026年9月）与时长（近12个月 / 6个月）不参与数值核验：
# 它们是时间坐标/量词，不是业务数值
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?")
_DUR_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:个?月|年|天|日|小时|分钟|周|季度)")


def _collect_numbers(value: Any, out: set[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.add(round(float(value), 4))
        out.add(round(float(value), 2))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_numbers(v, out)


def _match_allowed(val: float, allowed: set[float], is_pct: bool) -> bool:
    """数值匹配：带 % 的 token 只对百分比域，否则只对原值域，杜绝跨域误配。

    - 百分比域容差 max(0.061, |c|×1.1%)：放过 0.3%↔0.35% 这类 1 位小数舍入；
    - 原值域容差 max(0.005, |c|×1.1%)：足以放过千分位/4 位小数表达，
      拦得住 8% 量级的数值篡改（如 0.5006 → 0.5406）。
    """
    floor = 0.061 if is_pct else 0.005
    for a in allowed:
        cands = (a * 100, -a * 100) if is_pct else (a, -a)
        for c in cands:
            if abs(val - c) <= max(floor, abs(c) * 0.011):
                return True
    return False


def check_numbers(narrative: str, result: dict, facts: dict) -> tuple[list[str], int]:
    """确定性核验：叙述中的每个数字必须能在结果集/事实中找到（含百分比换算）。"""
    allowed: set[float] = set()
    _collect_numbers(result.get("rows", []), allowed)
    _collect_numbers(facts, allowed)
    if result.get("row_count") is not None:
        _collect_numbers(result["row_count"], allowed)

    issues: list[str] = []
    checked = 0
    clean = _DUR_RE.sub(" ", _DATE_RE.sub(" ", narrative))
    for tok, pct in _NUM_RE.findall(clean):
        val = float(tok.replace(",", ""))
        checked += 1
        if not _match_allowed(val, allowed, bool(pct)):
            issues.append(f"叙述中的数字 {tok}{'%' if pct else ''} 未出现在查询结果或计算事实中（疑似编造）")
    return issues, checked
