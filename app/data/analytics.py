"""归因下钻：纯代码计算"为什么变了"，LLM 只负责解读。

确定性主张的落点：贡献分解全部由 SQL 完成（同比环比、按维度分解 delta），
Insight Agent 拿到的是"已计算好的事实"，不存在模型心算的空间。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_settings
from .dsl import QueryDSL, TimeRange, compile_sql
from .metrics import METRICS
from .sandbox import Sandbox


@dataclass
class DrillDownFacts:
    metric_name: str
    current_window: str
    previous_window: str
    current_value: float | None
    previous_value: float | None
    delta: float | None
    delta_pct: float | None
    breakdowns: list[dict] = field(default_factory=list)   # 每个维度的 top 贡献
    month_series: list[tuple[str, float]] = field(default_factory=list)

    def as_context(self) -> dict:
        return {
            "metric": self.metric_name,
            "对比窗口": f"{self.previous_window} → {self.current_window}",
            "当前值": self.current_value,
            "上期值": self.previous_value,
            "变化": self.delta,
            "变化率": self.delta_pct,
            "维度分解": self.breakdowns,
            "月度序列_近12个月": [
                {"month": m, "value": round(v, 4)} for m, v in self.month_series
            ],
        }


def _parse_window(tr: TimeRange) -> tuple[str, str]:
    """把 DSL 时间条件解析为 (当前月, 上一个自然月)，仅支持按月粒度。"""
    if tr.mode == "last_n_months" and tr.n == 1:
        cur = get_settings().generator_end_month
    elif tr.mode == "between" and tr.start and tr.end and tr.start == tr.end:
        cur = tr.start
    else:
        # 兜底：无法定位单月时，取数据集最后一个月（并在事实中标注）
        cur = get_settings().generator_end_month
    y, m = int(cur[:4]), int(cur[5:7])
    pm, py = (m - 1, y) if m > 1 else (12, y - 1)
    return cur, f"{py:04d}-{pm:02d}"


async def drill_down(dsl: QueryDSL, role: str) -> DrillDownFacts:
    """计算主指标的环比变化与维度贡献分解。

    贡献定义（加法型指标）：该维度成员的指标值变化量；比率型指标（毛利率等）
    用成员毛利额变化与收入结构近似解释——这里统一采用"成员值变化量"排序，
    并在 breakdown 中同时给出成员当前值与变化，避免过度建模。
    """
    metric = METRICS[dsl.metric]
    cur_m, prev_m = _parse_window(dsl.time)

    async def _one(month: str) -> float | None:
        q = dsl.model_copy(deep=True)
        q.dimensions = []
        q.time = TimeRange(mode="between", start=month, end=month)
        q.limit = 1
        sql, _ = compile_sql(q, role=role)
        res = await Sandbox().execute(sql)
        return float(res.rows[0][0]) if res.rows and res.rows[0][0] is not None else None

    cur_v, prev_v = await _one(cur_m), await _one(prev_m)
    delta = None if (cur_v is None or prev_v is None) else cur_v - prev_v
    delta_pct = (
        None if (delta is None or not prev_v) else delta / abs(prev_v)
    )

    facts = DrillDownFacts(
        metric_name=metric.name, current_window=cur_m, previous_window=prev_m,
        current_value=cur_v, previous_value=prev_v, delta=delta, delta_pct=delta_pct,
    )

    # 维度分解：在 DSL 既有过滤条件内，按维度看成员级变化
    for dim in ("category", "channel", "region"):
        if dsl.filters and any(f.field == dim for f in dsl.filters):
            continue
        rows: list[dict] = []
        for month in (prev_m, cur_m):
            q = dsl.model_copy(deep=True)
            q.dimensions = [dim]
            q.time = TimeRange(mode="between", start=month, end=month)
            q.limit = 50
            sql, _ = compile_sql(q, role=role)
            res = await Sandbox().execute(sql)
            key = res.columns.index(dim)
            for r in res.rows:
                member = r[key]
                if month == prev_m:
                    rows.append({"member": member, "prev": float(r[-1]), "cur": None})
                else:
                    hit = [x for x in rows if x["member"] == member]
                    if hit:
                        hit[0]["cur"] = float(r[-1])
                    else:
                        rows.append({"member": member, "prev": None, "cur": float(r[-1])})
        for x in rows:
            p, c = x.get("prev"), x.get("cur")
            x["delta"] = None if (p is None or c is None) else round(c - p, 4)
        rows = [x for x in rows if x.get("delta") is not None]
        rows.sort(key=lambda x: abs(x["delta"]), reverse=True)
        if rows:
            facts.breakdowns.append({
                "dimension": dim,
                "top_movers": [
                    {"member": x["member"], "上期": x["prev"], "本期": x["cur"], "变化": x["delta"]}
                    for x in rows[:5]
                ],
            })

    # 近 12 个月主指标月度序列（趋势图数据，按月升序）
    q = dsl.model_copy(deep=True)
    q.dimensions = ["month"]
    q.time = TimeRange(mode="last_n_months", n=12)
    q.limit = 12
    sql, _ = compile_sql(q, role=role)
    res = await Sandbox().execute(sql)
    for r in res.rows:
        facts.month_series.append((str(r[0]), float(r[1])))
    facts.month_series.sort(key=lambda x: x[0])

    return facts
